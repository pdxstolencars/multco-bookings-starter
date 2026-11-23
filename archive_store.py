"""Private durable archive and public static index for verified MCSO bookings.

Public exports contain only a small whitelist. No historical records are ever
put in Git source control. Production requires an S3-compatible private store
(e.g. Cloudflare R2) and an explicit one-time init.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

PUBLIC_FIELDS = ("booking_id", "name", "booking_date", "charges", "facility", "source_url")
SOURCE_PREFIX = "https://apps.mcso.us/PAID/Home/Booking?id="
LEGACY_SOURCE_PREFIX = "https://apps.mcso.us/PAID/Home/Booking/"
DEFAULT_RETENTION_DAYS = 730  # Two years; 0 disables age-based expiration after legal review.


def retention_days() -> int:
    value = int(os.getenv("ARCHIVE_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    if not 0 <= value <= 36500:
        raise ValueError("ARCHIVE_RETENTION_DAYS must be 0..36500")
    return value


def clean_record(item: dict, now: datetime, max_age: int) -> dict | None:
    """Whitelist only public fields and reject invalid dates, IDs and URLs."""
    if not isinstance(item, dict):
        return None
    bid = str(item.get("booking_id", ""))
    day = item.get("booking_date", "")
    if not re.fullmatch(r"\d{1,12}", bid) or not isinstance(day, str):
        return None
    try:
        date = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return None
    if date > now.date() + timedelta(days=1):
        return None
    if max_age and date < now.date() - timedelta(days=max_age):
        return None
    name = str(item.get("name", "")).strip()
    if len(name) < 3 or len(name) > 110:
        return None
    # Do not trust arbitrary external URLs, even when a record is loaded from storage.
    expected_url = SOURCE_PREFIX + bid
    supplied_url = item.get("source_url", "")
    if not isinstance(supplied_url, str) or supplied_url.rstrip("/") not in {
        expected_url, LEGACY_SOURCE_PREFIX + bid
    }:
        return None
    charges = item.get("charges", [])
    if not isinstance(charges, list):
        charges = []
    return {
        "booking_id": bid,
        "name": name,
        "booking_date": day,
        "charges": [str(c).strip()[:300] for c in charges[:20] if str(c).strip()],
        "facility": str(item.get("facility", "")).strip()[:100],
        "source_url": expected_url,
    }


def empty_state(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    return {
        "schema": 1, "created_utc": stamp,
        "last_updated_utc": None,
        "first_collection_utc": None,
        "retention_days": retention_days(),
        "bookings": [], "suppressed_booking_ids": [],
    }


def merge_state(state: dict, incoming: list[dict], now: datetime) -> dict:
    """Update or add a booking, keep older bookings, obey suppression and retention."""
    days = retention_days()
    suppressed = {str(i) for i in state.get("suppressed_booking_ids", []) if str(i).isdigit()}
    values: dict[str, dict] = {}
    for item in list(state.get("bookings", [])) + list(incoming):
        rec = clean_record(item, now, days)
        if rec and rec["booking_id"] not in suppressed:
            values[rec["booking_id"]] = rec
    result = {**state}
    result["bookings"] = sorted(values.values(), key=lambda r: (r["booking_date"], int(r["booking_id"])), reverse=True)
    result["retention_days"] = days
    result["last_updated_utc"] = now.isoformat(timespec="seconds")
    result["first_collection_utc"] = state.get("first_collection_utc") or result["last_updated_utc"]
    result["suppressed_booking_ids"] = sorted(suppressed, key=int)
    return result


def suppress_record(state: dict, booking_id: str) -> dict:
    if not re.fullmatch(r"\d{1,12}", booking_id):
        raise ValueError("Suppression requires a numeric booking ID")
    result = {**state}
    ids = set(map(str, state.get("suppressed_booking_ids", [])))
    ids.add(booking_id)
    result["suppressed_booking_ids"] = sorted(ids, key=int)
    result["bookings"] = [r for r in state.get("bookings", []) if str(r.get("booking_id")) != booking_id]
    return result


def export_site(state: dict, site: Path = Path("site"), now: datetime | None = None) -> dict:
    """Produce a recent-first view and year-sharded historical search data."""
    now = now or datetime.now(timezone.utc)
    site.mkdir(parents=True, exist_ok=True)
    folder = site / "archive"
    folder.mkdir(parents=True, exist_ok=True)
    days = retention_days()
    excluded = set(map(str, state.get("suppressed_booking_ids", [])))
    records = {}
    for item in state.get("bookings", []):
        rec = clean_record(item, now, days)
        if rec and rec["booking_id"] not in excluded:
            records[rec["booking_id"]] = rec
    all_records = sorted(records.values(), key=lambda r: (r["booking_date"], int(r["booking_id"])), reverse=True)
    newest = (now.date() - timedelta(days=7)).isoformat()
    recent = [r for r in all_records if r["booking_date"] >= newest]
    years = {}
    for item in all_records:
        years.setdefault(item["booking_date"][:4], []).append(item)
    for path in folder.glob("[0-9][0-9][0-9][0-9].json"):
        if path.stem not in years:
            path.unlink()
    for year, records_for_year in years.items():
        _atomic_json(folder / f"{year}.json", {"bookings": records_for_year})
    manifest = {
        "schema": 1,
        "last_updated_utc": state.get("last_updated_utc"),
        "coverage_start_utc": state.get("first_collection_utc"),
        "retention_days": days,
        "total": len(all_records),
        "oldest_booking_date": all_records[-1]["booking_date"] if all_records else None,
        "newest_booking_date": all_records[0]["booking_date"] if all_records else None,
        "years": [{"year": y, "file": f"archive/{y}.json", "count": len(rows)} for y, rows in sorted(years.items(), reverse=True)],
    }
    _atomic_json(site / "recent.json", {"last_updated_utc": state.get("last_updated_utc"), "bookings": recent})
    _atomic_json(folder / "manifest.json", manifest)
    return manifest


def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temp.replace(path)


class ArchiveStore:
    """Choose local private file for development or required S3 for production."""
    def __init__(self):
        self.backend = os.getenv("ARCHIVE_BACKEND", "local")
        if self.backend not in {"local", "s3"}:
            raise ValueError("ARCHIVE_BACKEND must be local or s3")
        self.file = Path(os.getenv("ARCHIVE_FILE", ".private/archive.json"))
        self.bucket = os.getenv("ARCHIVE_BUCKET", "")
        self.key = os.getenv("ARCHIVE_KEY", "multco/archive.json")
        if self.backend == "s3":
            necessary = ["ARCHIVE_BUCKET", "ARCHIVE_ENDPOINT_URL", "ARCHIVE_ACCESS_KEY_ID", "ARCHIVE_SECRET_ACCESS_KEY"]
            missing = [key for key in necessary if not os.getenv(key)]
            if missing:
                raise ValueError(f"Missing private archive configuration: {', '.join(missing)}")
            import boto3
            self.client = boto3.client(
                "s3", endpoint_url=os.environ["ARCHIVE_ENDPOINT_URL"],
                aws_access_key_id=os.environ["ARCHIVE_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["ARCHIVE_SECRET_ACCESS_KEY"],
                region_name="auto",
            )

    def read(self) -> dict:
        if self.backend == "local":
            if not self.file.exists():
                raise FileNotFoundError(f"Private archive not initialized: {self.file}")
            data = self.file.read_text(encoding="utf-8")
        else:
            from botocore.exceptions import ClientError
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=self.key)
                data = response["Body"].read().decode("utf-8")
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                    raise FileNotFoundError("Private S3 archive not initialized") from error
                raise
        state = json.loads(data)
        if not isinstance(state, dict) or state.get("schema") != 1 or not isinstance(state.get("bookings"), list) or not isinstance(state.get("suppressed_booking_ids"), list):
            raise ValueError("Private archive invalid; aborting rather than replacing it")
        return state

    def write(self, state: dict):
        if self.backend == "local":
            _atomic_json(self.file, state)
        else:
            payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.client.put_object(Bucket=self.bucket, Key=self.key, Body=payload, ContentType="application/json")

    def initialize(self) -> dict:
        try:
            self.read()
        except FileNotFoundError:
            state = empty_state()
            self.write(state)
            return state
        raise FileExistsError("Archive already initialized; refusing to erase it")


def main():
    ap = argparse.ArgumentParser(description="Manage private MCSO archive")
    ap.add_argument("action", choices=["init", "export", "suppress", "status"])
    ap.add_argument("booking_id", nargs="?")
    ap.add_argument("--site", type=Path, default=Path("site"))
    args = ap.parse_args()
    try:
        store = ArchiveStore()
        if args.action == "init":
            store.initialize()
            print("Initialized private archive (empty; no example data)")
            return
        state = store.read()
        if args.action == "suppress":
            if not args.booking_id:
                raise ValueError("Provide booking ID to suppress")
            state = suppress_record(state, args.booking_id)
            store.write(state)
            print("Booking ID suppressed. Deploy refreshed public site immediately to apply.")
        if args.action in ("export", "suppress"):
            manifest = export_site(state, args.site)
            print(f"Published index contains {manifest['total']} bookings in {len(manifest['years'])} year(s)")
        else:
            print(f"Archive records: {len(state['bookings'])}; last scrape: {state.get('last_updated_utc')}")
    except Exception as error:
        print(f"Archive stopped safely: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
