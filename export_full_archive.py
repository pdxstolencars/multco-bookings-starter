"""Build a public, full-detail GitHub Pages export from verified private R2 records.

Sources: multco/sequential-test/records and multco/sequential-daily/records.
Private checkpoints, credentials, and raw S3 metadata are never exported.
Use --suppress-booking-id N to hide an ID from all *future* public deployments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo

PREFIXES = ("multco/sequential-test/records/", "multco/sequential-daily/records/")
CHECKPOINTS = ("multco/sequential-test/checkpoint.json", "multco/sequential-daily/checkpoint.json")
SUPPRESSION_KEY = "multco/publication/suppressed.json"
LEGACY_ARCHIVE_KEY = "multco/archive.json"
VERIFIED_SMOKE_KEY = "multco/full-detail-smoke-1658164.json"
DETAIL_FIELDS = (
    "swis_id", "name", "age_displayed", "gender", "race", "height", "weight",
    "hair", "eyes", "arresting_agency", "booking_datetime_local", "facility",
    "projected_release_date_displayed",
)


def valid_source(url: str, bid: str) -> bool:
    try:
        s = urlsplit(url)
        q = parse_qs(s.query, strict_parsing=True)
    except (TypeError, ValueError):
        return False
    return (
        s.scheme == "https" and s.hostname == "apps.mcso.us" and s.port in (None, 443)
        and s.path.casefold() == "/paid/home/booking"
        and set(q) <= {"id", "releaseId"} and q.get("id") == [bid]
        and all(len(v) == 1 and re.fullmatch(r"\d{1,14}", v[0]) for v in q.values())
        and not s.fragment and not s.username and not s.password
    )


def _same_charge_text(detailed: str, short: str) -> bool:
    """Reconcile two extracts of the SAME page without changing charge meaning.

    The collector collapses all whitespace and truncates charge summaries at
    300 characters; the full-detail parser preserves original spacing and
    length. Neither difference should invalidate an otherwise identical charge.
    """
    detail_normalized = " ".join(detailed.split())
    short_normalized = " ".join(short.split())
    return detail_normalized == short_normalized or (
        len(short_normalized) == 300 and detail_normalized.startswith(short_normalized)
    )


def public_record(raw: dict) -> dict:
    """Select only the fields collected from the public MCSO booking detail page."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid record object")
    bid = str(raw.get("booking_id", ""))
    date = raw.get("booking_date")
    if not re.fullmatch(r"\d{1,12}", bid) or not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("Missing booking ID/date")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError("Invalid booking date") from e
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or not valid_source(raw.get("source_url"), bid):
        raise ValueError("Record lacks validated name/official booking URL")
    source_detail = raw.get("booking_detail")
    if not isinstance(source_detail, dict):
        raise ValueError("Missing full detail object")
    detail = {key: source_detail.get(key) if isinstance(source_detail.get(key), str) else None for key in DETAIL_FIELDS}
    cases = raw.get("case_groups")
    if not isinstance(cases, list):
        raise ValueError("Missing case group list")
    case_groups = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("charges"), list):
            raise ValueError("Invalid case")
        charges = []
        for c in case["charges"]:
            if not isinstance(c, dict) or not isinstance(c.get("description"), str):
                raise ValueError("Invalid charge")
            charges.append({
                "description": c["description"],
                "bail_displayed": c.get("bail_displayed") if isinstance(c.get("bail_displayed"), str) else None,
                "status_displayed": c.get("status_displayed") if isinstance(c.get("status_displayed"), str) else None,
            })
        case_groups.append({
            "court_case_number": case.get("court_case_number") if isinstance(case.get("court_case_number"), str) else None,
            "da_case_number": case.get("da_case_number") if isinstance(case.get("da_case_number"), str) else None,
            "citation_number": case.get("citation_number") if isinstance(case.get("citation_number"), str) else None,
            "charges": charges,
        })
    plain_charges = [c["description"] for case in case_groups for c in case["charges"]]
    raw_charges = raw.get("charges", [])
    if (not isinstance(raw_charges, list) or len(plain_charges) != len(raw_charges)
            or any(not isinstance(short, str) or not _same_charge_text(full, short)
                   for full, short in zip(plain_charges, raw_charges))):
        raise ValueError(f"Booking {bid}: Charge list does not match case details")
    sort_date = date + "T00:00:00"
    if detail.get("booking_datetime_local"):
        try:
            sort_date = datetime.strptime(detail["booking_datetime_local"], "%m/%d/%Y %I:%M %p").isoformat(timespec="seconds")
        except ValueError:
            raise ValueError("Malformed booking time") from None
    return {
        "booking_id": bid, "name": name, "booking_date": date,
        "booking_sort_local": sort_date,
        "charges": plain_charges,
        "facility": detail.get("facility") or raw.get("facility"),
        "source_url": raw["source_url"],
        "captured_utc": raw.get("captured_utc") if isinstance(raw.get("captured_utc"), str) else None,
        "booking_detail": detail,
        "case_groups": case_groups,
    }


def json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def write_json(p: Path, value) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix(p.suffix + ".tmp")
    temp.write_bytes(json_bytes(value))
    temp.replace(p)


def read_if_present(client, bucket: str, key: str):
    from botocore.exceptions import ClientError
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def suppression_ids(client, bucket: str) -> set[str]:
    ids = set()
    for key, field in ((SUPPRESSION_KEY, "ids"), (LEGACY_ARCHIVE_KEY, "suppressed_booking_ids")):
        value = read_if_present(client, bucket, key)
        if value:
            if not isinstance(value, dict) or not isinstance(value.get(field), list):
                raise ValueError("Malformed suppression archive")
            ids.update(str(n) for n in value[field])
    return ids


def suppress(client, bucket: str, bid: str):
    if not re.fullmatch(r"\d{1,12}", bid):
        raise ValueError("Invalid booking ID")
    current = read_if_present(client, bucket, SUPPRESSION_KEY)
    if current is not None and (not isinstance(current, dict) or not isinstance(current.get("ids"), list)):
        raise ValueError("Malformed publication suppression list")
    ids = set(str(i) for i in ((current or {}).get("ids", [])))
    ids.add(bid)
    client.put_object(Bucket=bucket, Key=SUPPRESSION_KEY,
                      Body=json_bytes({"schema": 1, "ids": sorted(ids, key=int)}),
                      ContentType="application/json", CacheControl="no-store")


def collect_public_records(client, bucket: str) -> tuple[list[dict], set[str]]:
    suppressed = suppression_ids(client, bucket)
    data = {}
    for prefix in PREFIXES:
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
        for page in pages:
            for item in page.get("Contents", []):
                key = item["Key"]
                if not re.fullmatch(re.escape(prefix) + r"\d{1,12}\.json", key):
                    continue
                envelope = read_if_present(client, bucket, key)
                if not isinstance(envelope, dict) or envelope.get("private_full_record") is not True:
                    raise ValueError("Unexpected record wrapper")
                try:
                    record = public_record(envelope.get("record"))
                except ValueError as exc:
                    raise ValueError(f"R2 record {key}: {exc}") from exc
                if record["booking_id"] in suppressed:
                    continue
                prev = data.get(record["booking_id"])
                if prev is None or str(record.get("captured_utc") or "") >= str(prev.get("captured_utc") or ""):
                    data[record["booking_id"]] = record
    # The separately verified full-detail test for ID 1658164 predates the
    # permanent sequential collector. Include it if it exists, without ever
    # accepting arbitrary smoke-test records or overwriting a fresher capture.
    smoke = read_if_present(client, bucket, VERIFIED_SMOKE_KEY)
    if smoke is not None:
        if not isinstance(smoke, dict) or smoke.get("purpose") != "PRIVATE_SINGLE_KNOWN_RECORD_TEST_ONLY":
            raise ValueError("Unexpected verified booking smoke-test format")
        verified = public_record(smoke.get("record"))
        if verified["booking_id"] != "1658164":
            raise ValueError("Verified smoke-test ID does not match")
        if verified["booking_id"] not in suppressed:
            previous = data.get(verified["booking_id"])
            if previous is None or str(verified.get("captured_utc") or "") >= str(previous.get("captured_utc") or ""):
                data[verified["booking_id"]] = verified

    records = sorted(data.values(), key=lambda r: (r["booking_sort_local"], int(r["booking_id"])), reverse=True)
    return records, suppressed


def scan_timestamp(client, bucket: str) -> str | None:
    times = []
    for key in CHECKPOINTS:
        state = read_if_present(client, bucket, key)
        if isinstance(state, dict) and isinstance(state.get("last_run_utc"), str):
            times.append(state["last_run_utc"])
    return max(times) if times else None


def export_records(records: list[dict], site: Path, published_utc: str, scan_utc: str | None):
    if not records:
        raise ValueError("Refusing to replace the public website with an empty archive")
    years = {}
    for record in records:
        years.setdefault(record["booking_date"][:4], []).append(record)
    seven_days_ago = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=6)
    recent = [r for r in records if r["booking_date"] >= seven_days_ago.isoformat()]
    for year, group in years.items():
        write_json(site / "archive" / f"{year}.json", {"schema": 2, "bookings": group})
    manifest = {
        "schema": 2, "total": len(records), "published_utc": published_utc,
        "last_scan_utc": scan_utc, "coverage_start_utc": min(r["booking_date"] for r in records) + "T00:00:00Z",
        "years": [{"year": year, "file": f"archive/{year}.json", "count": len(group)}
                  for year, group in sorted(years.items(), reverse=True)],
    }
    write_json(site / "archive" / "manifest.json", manifest)
    write_json(site / "recent.json", {"schema": 2, "last_updated_utc": published_utc,
                                      "last_scan_utc": scan_utc, "bookings": recent})
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="site")
    parser.add_argument("--suppress-booking-id")
    args = parser.parse_args()
    required = ("ARCHIVE_BUCKET", "ARCHIVE_ENDPOINT_URL", "ARCHIVE_ACCESS_KEY_ID", "ARCHIVE_SECRET_ACCESS_KEY")
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        parser.error("Missing Cloudflare R2 credentials: " + ", ".join(missing))
    import boto3
    client = boto3.client("s3", endpoint_url=os.environ["ARCHIVE_ENDPOINT_URL"],
                          aws_access_key_id=os.environ["ARCHIVE_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ["ARCHIVE_SECRET_ACCESS_KEY"], region_name="auto")
    bucket = os.environ["ARCHIVE_BUCKET"]
    if args.suppress_booking_id:
        suppress(client, bucket, args.suppress_booking_id)
        print("Suppression saved in private R2")
    records, suppressed = collect_public_records(client, bucket)
    utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = export_records(records, Path(args.site), utc, scan_timestamp(client, bucket))
    print(f"PUBLIC EXPORT: {manifest['total']} verified records, "
          f"{len(manifest['years'])} year group(s); {len(suppressed)} suppressed ID(s).")
    print("No passwords, secret keys, or private R2 checkpoints included in site files.")


if __name__ == "__main__":
    main()
