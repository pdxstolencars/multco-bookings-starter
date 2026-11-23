"""One-record, read-only MCSO detail-page smoke test.

Visits one already-known public detail URL, parses a whitelist of public fields,
then stores exactly one test result in a distinct PRIVATE R2 key. This is not a
sequential ID crawler, a CAPTCHA workaround, or a public publication workflow.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from collector import parse_booking_html, canonical_booking_url
from archive_store import ArchiveStore, clean_record, retention_days

# Explicitly supplied by the repository owner; no guessing or incrementing IDs.
TEST_URL = "https://apps.mcso.us/PAID/Home/Booking?id=1658164"
PRIVATE_TEST_KEY = "multco/live-smoke-test-1658164.json"


def classify_page(html: str) -> str:
    """Fail closed on human-verification and missing-record indicators."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    indicators = soup.select('[class*="captcha" i], [id*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i]')
    body_text = soup.get_text(" ", strip=True).lower()
    if indicators or any(value in body_text for value in (
        "verify you are human", "verify you're human", "human verification",
        "checking your browser", "security challenge", "captcha", 
    )):
        return "HUMAN_VERIFICATION"
    if any(value in body_text for value in ("booking not found", "no booking found")):
        return "NOT_FOUND"
    return "DETAIL_OR_OTHER"


def retrieve_known_record() -> dict:
    """Retrieve only TEST_URL; do not follow navigation to unexpected destinations."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                locale="en-US", timezone_id="America/Los_Angeles",
                user_agent="MultcoBookingsSmokeTest/1.0 (single public URL; no crawling)",
            )
            page = context.new_page()
            response = page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)
            if response is None or response.status != 200:
                raise RuntimeError("MCSO returned a non-200 response; stopped without saving data")
            final_url = page.url
            if (urlsplit(final_url).hostname != "apps.mcso.us"
                    or canonical_booking_url(final_url) != TEST_URL):
                raise RuntimeError("MCSO redirected away from the expected booking URL; stopped")
            html = page.content()
            status = classify_page(html)
            if status != "DETAIL_OR_OTHER":
                raise RuntimeError(f"MCSO response was {status}; stopped without saving data")
            record = parse_booking_html(html, TEST_URL)
            if not record:
                raise RuntimeError("Could not verify name, booking date, and ID on the live page")
            if record["booking_id"] != "1658164":
                raise RuntimeError("Mismatched booking ID; stopped")
            return record
        finally:
            browser.close()


def save_private_smoke_record(record: dict) -> None:
    store = ArchiveStore()
    if store.backend != "s3":
        raise RuntimeError("Require private S3 backend; never write to public repository")
    now = datetime.now(timezone.utc)
    sanitized = clean_record(record, now, retention_days())
    if not sanitized:
        raise RuntimeError("Record did not pass archive validation; nothing saved")
    payload = {
        "schema": 1,
        "purpose": "SINGLE_RECORD_LIVE_SMOKE_TEST_ONLY",
        "tested_utc": now.isoformat(timespec="seconds"),
        "record": sanitized,
    }
    store.client.put_object(
        Bucket=store.bucket,
        Key=PRIVATE_TEST_KEY,
        Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        CacheControl="no-store",
    )
    # No personal details or credentials in public GitHub Actions logs.
    print("LIVE ONE-RECORD TEST: SUCCESS")
    print("Known booking ID: 1658164")
    print(f"Extracted charges: {len(sanitized['charges'])}")
    print(f"Saved one parsed record privately as: {PRIVATE_TEST_KEY}")
    print("Production archive and public website were NOT modified")


def main() -> int:
    print("Visiting one previously known booking URL (no enumeration or search)")
    try:
        record = retrieve_known_record()
        save_private_smoke_record(record)
        return 0
    except (RuntimeError, PlaywrightTimeout, OSError, ValueError) as exc:
        print(f"LIVE ONE-RECORD TEST FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Avoid accidentally exposing server response bodies or credentials.
        print(f"LIVE ONE-RECORD TEST FAILED: {type(exc).__name__} (inspect connection settings)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
