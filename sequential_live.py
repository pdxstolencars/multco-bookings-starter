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
         sleep: Callable[[float], None] = time.sleep) -> dict:
    if not (1 <= max_requests <= 500) or not (1 <= missing_threshold <= 50) or delay < 0:
        raise ValueError("Invalid scan safety limits")
    state = state_store.load()
    if state is None:
        state = new_state(mode, start, end)
        state_store.save(state)
        print(f"New PRIVATE {mode} checkpoint: starts at {start}")
    else:
        validate_state(state, mode, start, end)
        if repair_legacy_daily_checkpoint(state):
            state_store.save(state)
        print(f"Resuming PRIVATE {mode} checkpoint from ID {state['next_candidate']}")
    if state["complete"]:
        print("Requested finite range already complete; no new requests")
        return state
    state["run_count"] += 1
    state["last_run_utc"] = now_utc()
    state_store.save(state)
    processed = 0
    # Snapshot only prior-run gaps. Never fetch a just-checked missing ID twice
    # in the same run, and always prioritize new forward bookings.
    previous_missing = list(state.get("pending_missing_ids", [])) if mode == "daily" else []
    for idx in range(max_requests):
        candidate = state["next_candidate"]
        if end is not None and candidate > end:
            state["complete"] = True
            state_store.save(state)
            break
        try:
            result = fetch(candidate)
        except (ScanError, AccessStopped):
            # Leave next_candidate untouched on all unexpected responses.
            print(f"STOP: page {candidate} was not verifiably FOUND or MISSING; checkpoint unchanged", file=sys.stderr)
            raise
        if result.kind == "FOUND":
            if not result.record or str(result.record.get("booking_id")) != str(candidate):
                raise ScanError("Invalid validated record; checkpoint unchanged")
            # Write the full record BEFORE advancing checkpoint; replay is safe.
            state_store.save_record(result.record)
            state = apply_found(state, candidate)
        elif result.kind == "MISSING":
            state, stop = apply_missing(state, candidate, missing_threshold)
        else:
            raise ScanError("Unknown result; checkpoint unchanged")
        state_store.save(state)
        processed += 1
        print(f"ID {candidate}: {result.kind}; checkpoint -> {state['next_candidate']}")
        if end is not None and state["next_candidate"] > end:
            state["complete"] = True
            state_store.save(state)
            break
        if result.kind == "MISSING" and stop:
            print(f"Daily stopping rule met ({missing_threshold} consecutive confirmed missing IDs)")
            break
        if idx < max_requests - 1 and delay:
            sleep(delay)
    # Use at most five of the remaining request budget to revisit old gaps.
    # These checks are independent of the forward stop-after-five rule.
    if previous_missing and processed < max_requests:
        cursor = state.get("recheck_cursor", 0) % len(previous_missing)
        limit = min(5, max_requests - processed, len(previous_missing))
        for i in range(limit):
            missing_id = previous_missing[(cursor + i) % len(previous_missing)]
            if missing_id not in state.get("pending_missing_ids", []):
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
    print("RUN SUMMARY:",
          f"mode={mode} checked={processed} total_found={state['found_count']}",
          f"unresolved_missing={state['missing_count']} next={state['next_candidate']}",
          f"pending_rechecks={len(state.get('pending_missing_ids', []))}",
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
            scan(store, fetch, args.mode, start, end, args.max_requests, args.missing_threshold, args.delay)
        finally:
            close()
        return 0
    except Exception as exc:
        # No personal data or credentials in GitHub logs.
        print(f"SCANNER FAILED SAFELY: {type(exc).__name__}: {str(exc) if isinstance(exc, (ScanError, AccessStopped, ValueError)) else 'inspect private configuration'}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
