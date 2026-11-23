"""Authorized MCSO direct-booking scanner; private R2 store, durable checkpoint.

Modes:
  range: finite inclusive ID range, resumes until every candidate is checked;
         five misses do NOT truncate a specifically requested historical range.
  daily: starts after the provided seed ID; stops after five confirmed missing
         booking pages. The forward cursor advances between runs, while a
         separate pending-missing queue is rechecked for late-appearing records.

This program does not access the search form, solve CAPTCHAs, hide identity,
rotate IPs, or continue after rate limits / blocked responses. Use only within
MCSO's permission for direct URL collection.

No data are automatically published. Full records go only into private R2.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from full_record_parser import parse_full_record_html

BASE = "https://apps.mcso.us/PAID/Home/Booking?id="
RANGE_PREFIX = "multco/sequential-test"
DAILY_PREFIX = "multco/sequential-daily"
NOT_FOUND_PHRASES = (
    "booking not found",
    "booking was not found",
    "no booking found",
    "no booking information found",
)
CHALLENGE_PHRASES = (
    "captcha", "verify you are human", "verify you're human",
    "human verification", "security challenge", "checking your browser",
    "access denied", "request blocked", "too many requests",
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def charges_absent(record: dict) -> bool:
    """Only a validated, explicitly empty simple charge list needs a retry.

    Avoid classifying synthetic records that omit the field entirely as empty.
    The full parser always supplies a list, even when MCSO has not posted charges.
    """
    return isinstance(record, dict) and isinstance(record.get("charges"), list) and not record["charges"]


def candidate_url(booking_id: int) -> str:
    if not 1 <= booking_id <= 999999999999:
        raise ValueError("Invalid booking ID")
    return f"{BASE}{booking_id}"


class ScanError(RuntimeError):
    pass


class AccessStopped(ScanError):
    pass


class TransientServerError(ScanError):
    """Temporary server failure; retry a few times without advancing checkpoint."""
    pass


@dataclass(frozen=True)
class PageResult:
    kind: str  # FOUND / MISSING
    record: dict | None = None


def classify_page(status: int | None, html: str, booking_id: int, final_url: str) -> PageResult:
    """Accept only a valid detail or an explicit missing-booking response.

    HTTP errors and unexpected content must NEVER count as five missing IDs.
    """
    expected = candidate_url(booking_id)
    got = urlsplit(final_url)
    if got.scheme != "https" or got.hostname != "apps.mcso.us" or (got.path + ("?" + got.query if got.query else "")) != urlsplit(expected).path + "?" + urlsplit(expected).query:
        raise AccessStopped("Unexpected redirect or URL change")
    if status in (403, 429):
        raise AccessStopped("MCSO denied or rate-limited the request")
    if status is None:
        raise TransientServerError("MCSO server response unavailable (no HTTP status)")

    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#right .page") or soup.select_one("#right") or soup.body or soup
    visible = " ".join(main.stripped_strings).casefold()
    if (soup.select_one('[class*="captcha" i], [id*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], iframe[src*="turnstile" i]')
            or any(p in visible for p in CHALLENGE_PHRASES)):
        raise AccessStopped("MCSO returned a verification or access-block page")
    if status >= 500:
        raise TransientServerError(f"MCSO server returned HTTP {status}")
    if any(phrase in visible for phrase in NOT_FOUND_PHRASES):
        if status not in (200, 404):
            raise ScanError("Unexpected status on missing-booking response")
        return PageResult("MISSING")
    if status != 200:
        raise ScanError("HTTP response was not a verified detail or missing page")
    hidden = soup.select_one("input#BookingNumber")
    if hidden is None or hidden.get("value") != str(booking_id):
        raise ScanError("Missing or mismatched booking number field")
    try:
        full = parse_full_record_html(html, expected)
    except (ValueError, TypeError, KeyError) as exc:
        raise ScanError("Booking content could not be validated") from exc
    if full.get("booking_id") != str(booking_id):
        raise ScanError("Validated booking ID did not match")
    return PageResult("FOUND", full)


class PrivateStore:
    """R2-compatible store. Separate keys from multco/archive.json."""
    def __init__(self, mode: str):
        if mode not in ("range", "daily"):
            raise ValueError("Invalid mode")
        required = ("ARCHIVE_BUCKET", "ARCHIVE_ENDPOINT_URL", "ARCHIVE_ACCESS_KEY_ID", "ARCHIVE_SECRET_ACCESS_KEY")
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise ValueError("Missing GitHub R2 secrets: " + ", ".join(missing))
        import boto3
        self.client = boto3.client(
            "s3", endpoint_url=os.environ["ARCHIVE_ENDPOINT_URL"],
            aws_access_key_id=os.environ["ARCHIVE_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["ARCHIVE_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        self.bucket = os.environ["ARCHIVE_BUCKET"]
        self.prefix = RANGE_PREFIX if mode == "range" else DAILY_PREFIX

    def _write(self, key: str, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.client.put_object(Bucket=self.bucket, Key=key, Body=payload,
                               ContentType="application/json", CacheControl="no-store")

    def load(self) -> dict | None:
        from botocore.exceptions import ClientError
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=f"{self.prefix}/checkpoint.json")
            return json.loads(obj["Body"].read().decode("utf-8"))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def save(self, state: dict) -> None:
        self._write(f"{self.prefix}/checkpoint.json", state)

    def save_record(self, record: dict) -> None:
        bid = str(record["booking_id"])
        if not re.fullmatch(r"\d{1,12}", bid):
            raise ValueError("Invalid booking ID in parsed record")
        self._write(f"{self.prefix}/records/{bid}.json", {"private_full_record": True, "record": record})

    def discover_records_without_charges(self) -> list[int]:
        """One-time migration for already saved empty-charge records.

        Scan both historical range and daily folders; a populated copy wins
        if the same ID exists under both prefixes.
        """
        by_id: dict[int, bool] = {}
        for folder in (RANGE_PREFIX, DAILY_PREFIX):
            prefix = f"{folder}/records/"
            for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix
            ):
                for item in page.get("Contents", []):
                    key = item.get("Key", "")
                    if not re.fullmatch(re.escape(prefix) + r"\d{1,12}\.json", key):
                        continue
                    envelope = json.loads(
                        self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
                    )
                    if not isinstance(envelope, dict) or envelope.get("private_full_record") is not True:
                        raise ValueError("Unrecognized stored booking record envelope")
                    record = envelope.get("record")
                    if not isinstance(record, dict) or not re.fullmatch(r"\d{1,12}", str(record.get("booking_id", ""))):
                        raise ValueError("Invalid saved booking ID")
                    bid = int(record["booking_id"])
                    # Never queue records that already contain charges in either store.
                    by_id[bid] = by_id.get(bid, False) or not charges_absent(record)
        return sorted(bid for bid, has_charges in by_id.items() if not has_charges)


class LocalStore:
    """For repeatable tests; never uses the public website or live archive."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records = self.path.with_name(self.path.stem + "_records")

    def load(self) -> dict | None:
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def save_record(self, record: dict) -> None:
        self.records.mkdir(parents=True, exist_ok=True)
        bid = str(record["booking_id"])
        p = self.records / (bid + ".json")
        p.write_text(json.dumps({"private_full_record": True, "record": record}, indent=2), encoding="utf-8")

    def discover_records_without_charges(self) -> list[int]:
        ids = []
        for p in self.records.glob("*.json") if self.records.exists() else []:
            envelope = json.loads(p.read_text(encoding="utf-8"))
            if envelope.get("private_full_record") is not True:
                raise ValueError("Unexpected local booking record envelope")
            record = envelope.get("record")
            if charges_absent(record):
                ids.append(int(record["booking_id"]))
        return sorted(set(ids))


def new_state(mode: str, start: int, end: int | None) -> dict:
    if start < 1 or end is not None and end < start:
        raise ValueError("Invalid range")
    if mode not in ("range", "daily") or (mode == "range" and end is None) or (mode == "daily" and end is not None):
        raise ValueError("Mode/end mismatch")
    return {
        "schema": 1, "mode": mode, "start_id": start, "end_id": end,
        "next_candidate": start, "highest_found_id": start - 1,
        "missing_streak": 0, "found_count": 0, "missing_count": 0,
        "run_count": 0, "last_checked_id": None, "last_run_utc": None,
        "complete": False, "pending_missing_ids": [], "recheck_cursor": 0,
        "pending_charge_ids": [], "charge_backfill_done": False,
    }


def validate_state(state: dict, mode: str, start: int, end: int | None) -> None:
    if not isinstance(state, dict) or any((
        state.get("schema") != 1, state.get("mode") != mode,
        state.get("start_id") != start, state.get("end_id") != end,
        not isinstance(state.get("next_candidate"), int),
        not isinstance(state.get("highest_found_id"), int),
        state.get("next_candidate", 0) < start,
    )):
        raise ValueError("Stored checkpoint differs from requested scan; refusing to overwrite")



def repair_legacy_daily_checkpoint(state: dict) -> bool:
    """Upgrade a prior rewind-on-five-misses checkpoint without losing IDs.

    The old implementation stored next_candidate at highest_found+1 while
    last_checked_id pointed to the last confirmed missing page. Preserve the
    five missed IDs in a separate durable queue and move forward from there.
    This is deliberately idempotent and requires no user-edited R2 objects.
    """
    if state.get("mode") != "daily":
        return False
    changed = "pending_missing_ids" not in state or "recheck_cursor" not in state
    state.setdefault("pending_missing_ids", [])
    state.setdefault("recheck_cursor", 0)
    pending = state["pending_missing_ids"]
    if not isinstance(pending, list) or any(not isinstance(x, int) or x < state["start_id"] for x in pending):
        raise ValueError("Invalid pending missing IDs in checkpoint")
    if not isinstance(state["recheck_cursor"], int) or state["recheck_cursor"] < 0:
        raise ValueError("Invalid pending recheck cursor")
    last_checked = state.get("last_checked_id")
    if isinstance(last_checked, int) and state["next_candidate"] <= last_checked:
        gap = last_checked - state["next_candidate"] + 1
        if gap > 50:
            raise ValueError("Old checkpoint gap too large to migrate safely")
        state["pending_missing_ids"] = list(dict.fromkeys(pending + list(range(state["next_candidate"], last_checked + 1))))
        state["next_candidate"] = last_checked + 1
        state["missing_streak"] = 0
        changed = True
        print(f"Migrated previous missing-ID tail; next forward candidate {state['next_candidate']}")
    return changed


def prepare_charge_rechecks(state_store, state: dict) -> bool:
    """Import existing empty-charge records once, before handling new IDs.

    This does not reset or modify forward/missing booking checkpoints.
    """
    if state.get("mode") != "daily":
        return False
    changed = "pending_charge_ids" not in state or "charge_backfill_done" not in state
    state.setdefault("pending_charge_ids", [])
    state.setdefault("charge_backfill_done", False)
    pending = state["pending_charge_ids"]
    if not isinstance(pending, list) or any(not isinstance(i, int) or i < 1 for i in pending):
        raise ValueError("Invalid charge recheck queue")
    if not isinstance(state["charge_backfill_done"], bool):
        raise ValueError("Invalid charge migration state")
    if not state["charge_backfill_done"]:
        found = state_store.discover_records_without_charges()
        state["pending_charge_ids"] = list(dict.fromkeys(pending + found))
        state["charge_backfill_done"] = True
        print(f"Found {len(found)} archived booking IDs needing charge rechecks")
        changed = True
    return changed


def queue_unposted_charges(state: dict, bid: int, record: dict) -> None:
    """Keep only empty-charge bookings queued, without affecting found totals."""
    if state.get("mode") != "daily" or not isinstance(record.get("charges"), list):
        return
    queue = state.setdefault("pending_charge_ids", [])
    if charges_absent(record):
        if bid not in queue:
            queue.append(bid)
            print(f"ID {bid}: charges not listed yet; queued for next hourly recheck")
    elif bid in queue:
        queue.remove(bid)


def apply_late_found(state: dict, bid: int) -> dict:
    """Resolve a previously missing ID; no changes to the forward cursor."""
    if bid not in state.get("pending_missing_ids", []):
        raise ValueError("ID was not pending recheck")
    new = dict(state)
    new["pending_missing_ids"] = [x for x in state["pending_missing_ids"] if x != bid]
    new["highest_found_id"] = max(state["highest_found_id"], bid)
    new["found_count"] += 1
    new["missing_count"] = max(0, state["missing_count"] - 1)
    return new

def apply_found(state: dict, bid: int) -> dict:
    if bid != state["next_candidate"]:
        raise ValueError("Out-of-order record")
    new = dict(state)
    new["highest_found_id"] = max(state["highest_found_id"], bid)
    new["next_candidate"] = bid + 1
    new["missing_streak"] = 0
    new["found_count"] += 1
    new["last_checked_id"] = bid
    return new


def apply_missing(state: dict, bid: int, missing_threshold: int) -> tuple[dict, bool]:
    if bid != state["next_candidate"]:
        raise ValueError("Out-of-order record")
    new = dict(state)
    new["next_candidate"] = bid + 1
    new["missing_streak"] += 1
    new["missing_count"] += 1
    new["last_checked_id"] = bid
    if new["mode"] == "daily":
        new["pending_missing_ids"] = list(dict.fromkeys(new.get("pending_missing_ids", []) + [bid]))
    stop = new["mode"] == "daily" and new["missing_streak"] >= missing_threshold
    if stop:
        # Five misses end THIS run, not all future runs. Keep the durable
        # forward cursor beyond the gap; revisit misses in a separate pass.
        new["missing_streak"] = 0
    return new, stop


def scan(state_store, fetch: Callable[[int], PageResult], mode: str, start: int, end: int | None,
         max_requests: int = 20, missing_threshold: int = 5, delay: float = 2.0,
         sleep: Callable[[float], None] = time.sleep, gap_lookahead: int = 0) -> dict:
    if not (1 <= max_requests <= 500) or not (1 <= missing_threshold <= 50) or delay < 0 or not (0 <= gap_lookahead <= 200):
        raise ValueError("Invalid scan safety limits")
    if gap_lookahead and (mode != "daily" or gap_lookahead <= missing_threshold):
        raise ValueError("Gap lookahead must exceed missing threshold and requires daily mode")
    state = state_store.load()
    if state is None:
        state = new_state(mode, start, end)
        if mode == "daily":
            # Recover orphaned saved records if a prior checkpoint write failed.
            prepare_charge_rechecks(state_store, state)
        state_store.save(state)
        print(f"New PRIVATE {mode} checkpoint: starts at {start}")
    else:
        validate_state(state, mode, start, end)
        checkpoint_repaired = repair_legacy_daily_checkpoint(state)
        charge_queue_prepared = prepare_charge_rechecks(state_store, state)
        if checkpoint_repaired or charge_queue_prepared:
            state_store.save(state)
        print(f"Resuming PRIVATE {mode} checkpoint from ID {state['next_candidate']}")
    # Previous hourly runs advanced past speculative not-found IDs. For robust
    # lookahead, those IDs were never confirmed as valid bookings, so restart
    # the forward scan immediately after the highest *confirmed* booking.
    # This is a migration of the cursor only, not a deletion of any records.
    if mode == "daily" and gap_lookahead:
        confirmed_frontier = max(start, state["highest_found_id"] + 1)
        if state["next_candidate"] > confirmed_frontier:
            distance = state["next_candidate"] - confirmed_frontier
            if distance > 10000:
                raise ValueError("Excessive speculative cursor drift; manual checkpoint review required")
            pending = set(state.get("pending_missing_ids", []))
            pending.update(range(confirmed_frontier, state["next_candidate"]))
            state["pending_missing_ids"] = sorted(pending)
            state["next_candidate"] = confirmed_frontier
            state["missing_streak"] = 0
            state_store.save(state)
            print(f"Corrected speculative cursor: revisiting IDs from {confirmed_frontier}")
    if state["complete"]:
        print("Requested finite range already complete; no new requests")
        return state
    state["run_count"] += 1
    state["last_run_utc"] = now_utc()
    state_store.save(state)
    processed = 0
    # Recheck bookings that were saved without charges in PREVIOUS runs.
    # Do it before the forward scan, so the next run always attempts a retry.
    # Rotate unresolved IDs to avoid starving later records if the queue grows.
    prior_charge_ids = list(state.get("pending_charge_ids", [])) if mode == "daily" else []
    for missing_charge_id in prior_charge_ids[:min(10, max_requests)]:
        try:
            result = fetch(missing_charge_id)
        except AccessStopped:
            # A real access block must stop all collection immediately.
            print(f"STOP: access restriction during charge recheck {missing_charge_id}", file=sys.stderr)
            raise
        except ScanError:
            # A single old booking that is temporarily unavailable must not
            # prevent the rest of the hourly NEW-booking collection.
            print(f"CHARGE RECHECK {missing_charge_id}: temporarily unavailable; will retry next run")
            result = None
        if result is None:
            state["pending_charge_ids"].remove(missing_charge_id)
            state["pending_charge_ids"].append(missing_charge_id)
            processed += 1
            state_store.save(state)
            if delay and processed < min(10, max_requests):
                sleep(delay)
            continue
        if result.kind == "FOUND":
            if not result.record or str(result.record.get("booking_id")) != str(missing_charge_id):
                raise ScanError("Invalid charge recheck record")
            # Original record is kept until a fully validated replacement exists.
            if charges_absent(result.record):
                print(f"CHARGE RECHECK {missing_charge_id}: charges still not posted")
            else:
                state_store.save_record(result.record)
                state["pending_charge_ids"].remove(missing_charge_id)
                print(f"CHARGE RECHECK {missing_charge_id}: now {len(result.record.get('charges', []))} charge(s); record updated")
        elif result.kind == "MISSING":
            print(f"CHARGE RECHECK {missing_charge_id}: page temporarily missing; saved record retained")
        else:
            raise ScanError("Unknown charge recheck status")
        # Requeue unresolved records at the back, so each gets a turn.
        if missing_charge_id in state["pending_charge_ids"]:
            state["pending_charge_ids"].remove(missing_charge_id)
            state["pending_charge_ids"].append(missing_charge_id)
        processed += 1
        state_store.save(state)
        if delay and processed < min(10, max_requests):
            sleep(delay)
    # Snapshot only prior-run gaps. Never fetch a just-checked missing ID twice
    # in the same run, and always prioritize new forward bookings.
    previous_missing = list(state.get("pending_missing_ids", [])) if mode == "daily" else []
    forward_seen: set[int] = set()
    for idx in range(max_requests - processed):
        candidate = state["next_candidate"]
        if mode == "daily" and gap_lookahead and candidate >= state["highest_found_id"] + 1 + gap_lookahead:
            print(f"Safety lookahead exhausted: checked {gap_lookahead} IDs past last confirmed booking {state['highest_found_id']}")
            break
        if end is not None and candidate > end:
            state["complete"] = True
            state_store.save(state)
            break
        try:
            result = fetch(candidate)
            forward_seen.add(candidate)
        except (ScanError, AccessStopped):
            # Leave next_candidate untouched on all unexpected responses.
            print(f"STOP: page {candidate} was not verifiably FOUND or MISSING; checkpoint unchanged", file=sys.stderr)
            raise
        if result.kind == "FOUND":
            if not result.record or str(result.record.get("booking_id")) != str(candidate):
                raise ScanError("Invalid validated record; checkpoint unchanged")
            # Write the full record BEFORE advancing checkpoint; replay is safe.
            state_store.save_record(result.record)
            already_missing = candidate in state.get("pending_missing_ids", [])
            state = apply_found(state, candidate)
            if already_missing:
                state["pending_missing_ids"] = [n for n in state["pending_missing_ids"] if n != candidate]
                state["missing_count"] = max(0, state["missing_count"] - 1)
            queue_unposted_charges(state, candidate, result.record)
        elif result.kind == "MISSING":
            already_missing = candidate in state.get("pending_missing_ids", [])
            state, stop = apply_missing(state, candidate, missing_threshold)
            if already_missing:
                # A repeated negative response is not a *new* missing record.
                state["missing_count"] -= 1
        else:
            raise ScanError("Unknown result; checkpoint unchanged")
        state_store.save(state)
        processed += 1
        print(f"ID {candidate}: {result.kind}; checkpoint -> {state['next_candidate']}")
        if end is not None and state["next_candidate"] > end:
            state["complete"] = True
            state_store.save(state)
            break
        if result.kind == "MISSING" and stop and not gap_lookahead:
            print(f"Daily stopping rule met ({missing_threshold} consecutive confirmed missing IDs)")
            break
        if result.kind == "MISSING" and stop and gap_lookahead:
            print(f"Five-missing warning at {candidate}; using {gap_lookahead}-ID safety lookahead", flush=True)
        if idx < max_requests - 1 and delay:
            sleep(delay)
    # Use at most five of the remaining request budget to revisit old gaps.
    # These checks are independent of the forward stop-after-five rule.
    if previous_missing and processed < max_requests:
        cursor = state.get("recheck_cursor", 0) % len(previous_missing)
        limit = min(5, max_requests - processed, len(previous_missing))
        for i in range(limit):
            missing_id = previous_missing[(cursor + i) % len(previous_missing)]
            if missing_id not in state.get("pending_missing_ids", []) or missing_id in forward_seen:
                continue
            try:
                result = fetch(missing_id)
            except (ScanError, AccessStopped):
                print(f"STOP: recheck {missing_id} was not verifiably FOUND or MISSING; saved progress retained", file=sys.stderr)
                raise
            if result.kind == "FOUND":
                if not result.record or str(result.record.get("booking_id")) != str(missing_id):
                    raise ScanError("Invalid late booking; checkpoint unchanged")
                state_store.save_record(result.record)
                state = apply_late_found(state, missing_id)
                queue_unposted_charges(state, missing_id, result.record)
                print(f"LATE ID {missing_id}: FOUND; recovered without rewinding forward cursor")
            elif result.kind == "MISSING":
                print(f"LATE ID {missing_id}: still MISSING")
            else:
                raise ScanError("Unknown missing-ID recheck response")
            processed += 1
            if delay and i < limit - 1:
                sleep(delay)
            state_store.save(state)
        state["recheck_cursor"] = (cursor + limit) % max(len(state.get("pending_missing_ids", [])), 1)
        state_store.save(state)
    if mode == "daily" and gap_lookahead:
        # Do not let a speculative negative tail become a permanent checkpoint.
        # Each run must test the uncertain end again, since MCSO can publish
        # those booking pages later.
        state["next_candidate"] = max(start, state["highest_found_id"] + 1)
        state["missing_streak"] = 0
        state_store.save(state)
        print(f"Unverified tail retained for next run; confirmed frontier {state['next_candidate']}")
    print("RUN SUMMARY:",
          f"mode={mode} checked={processed} total_found={state['found_count']}",
          f"unresolved_missing={state['missing_count']} next={state['next_candidate']}",
          f"pending_rechecks={len(state.get('pending_missing_ids', []))}",
          f"pending_charge_rechecks={len(state.get('pending_charge_ids', []))}",
          f"complete={state['complete']} runs={state['run_count']}")
    print("PRIVATE R2 ONLY. Production archive and public GitHub Pages site not changed.")
    return state


def retry_transient_server_error(operation: Callable[[], PageResult], booking_id: int,
                                 backoffs: tuple[float, ...] = (10.0, 30.0),
                                 sleep: Callable[[float], None] = time.sleep) -> PageResult:
    """Retry only temporary 5xx/absent-status responses. Never retry access blocks.

    If all attempts fail, the caller stops on this ID and preserves its checkpoint.
    """
    for attempt in range(len(backoffs) + 1):
        try:
            return operation()
        except TransientServerError as exc:
            if attempt == len(backoffs):
                raise
            delay = backoffs[attempt]
            print(f"Temporary server response for ID {booking_id}: {exc}; "
                  f"retry {attempt + 1}/{len(backoffs)} after {delay:g}s", flush=True)
            sleep(delay)
    raise AssertionError("unreachable")


def live_fetcher():
    from playwright.sync_api import sync_playwright
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    agent = os.environ.get("SCRAPER_CONTACT", "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", agent):
        browser.close(); playwright.stop()
        raise ValueError("SCRAPER_CONTACT repository secret must be a contact email")
    ctx = browser.new_context(locale="en-US", timezone_id="America/Los_Angeles",
                               user_agent=f"MultcoBookingArchive/1.0 (direct booking monitoring; contact: {agent})")
    page = ctx.new_page()
    def fetch(booking_id: int) -> PageResult:
        url = candidate_url(booking_id)
        def once() -> PageResult:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is None:
                raise TransientServerError("MCSO server returned no HTTP response")
            return classify_page(response.status, page.content(), booking_id, page.url)
        try:
            return retry_transient_server_error(once, booking_id)
        except (ScanError, AccessStopped):
            raise
        except Exception as exc:
            raise ScanError(f"Network/browser failure at booking ID {booking_id}") from exc
    def close():
        browser.close(); playwright.stop()
    return fetch, close


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", required=True, choices=("range", "daily"))
    ap.add_argument("--start", type=int, default=1658100, help="Range start, or first daily candidate")
    ap.add_argument("--end", type=int, default=None, help="Inclusive end, only with --mode range")
    ap.add_argument("--seed", type=int, default=1658164, help="Last previously known ID, only for daily")
    ap.add_argument("--max-requests", type=int, default=20)
    ap.add_argument("--missing-threshold", type=int, default=5)
    ap.add_argument("--gap-lookahead", type=int, default=0,
                    help="For daily mode: revisit an uncertain tail and check up to N IDs after the latest confirmed booking; use 30 for hourly collection")
    ap.add_argument("--delay", type=float, default=2.0)
    args = ap.parse_args()
    if args.mode == "range":
        if args.end is None:
            ap.error("--end required for range")
        start, end = args.start, args.end
    else:
        start, end = args.seed + 1, None
    try:
        store = PrivateStore(args.mode)
        fetch, close = live_fetcher()
        try:
            scan(store, fetch, args.mode, start, end, args.max_requests, args.missing_threshold, args.delay,
                 gap_lookahead=args.gap_lookahead)
        finally:
            close()
        return 0
    except Exception as exc:
        # No personal data or credentials in GitHub logs.
        print(f"SCANNER FAILED SAFELY: {type(exc).__name__}: {str(exc) if isinstance(exc, (ScanError, AccessStopped, ValueError)) else 'inspect private configuration'}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
