"""Private smoke test for ONE user-supplied known public booking detail page.

No enumeration, no CAPTCHA interaction, no writes to the production archive.
"""
from __future__ import annotations

import json
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from archive_store import ArchiveStore
from collector import canonical_booking_url
from full_record_parser import parse_full_record_html
from live_record_smoke import classify_page

KNOWN_URL = "https://apps.mcso.us/PAID/Home/Booking?id=1658164"
PRIVATE_KEY = "multco/full-detail-smoke-1658164.json"


def main() -> int:
    try:
        store = ArchiveStore()
        if store.backend != "s3":
            raise RuntimeError("Private R2 storage required")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(timezone_id="America/Los_Angeles", locale="en-US")
                page = ctx.new_page()
                response = page.goto(KNOWN_URL, wait_until="domcontentloaded", timeout=30000)
                if response is None or response.status != 200:
                    raise RuntimeError("Known page did not return HTTP 200")
                if urlsplit(page.url).hostname != "apps.mcso.us" or canonical_booking_url(page.url) != KNOWN_URL:
                    raise RuntimeError("Unexpected redirect")
                html = page.content()
                if classify_page(html) != "DETAIL_OR_OTHER":
                    raise RuntimeError("Human verification or not-found page; stopped")
                record = parse_full_record_html(html, KNOWN_URL)
                if record["booking_id"] != "1658164":
                    raise RuntimeError("Booking ID mismatch")
            finally:
                browser.close()

        store.client.put_object(
            Bucket=store.bucket, Key=PRIVATE_KEY,
            Body=json.dumps({"purpose": "PRIVATE_SINGLE_KNOWN_RECORD_TEST_ONLY", "record": record}, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json", CacheControl="no-store",
        )
        # Never log raw names, sensitive fields or charges in public GitHub logs.
        print("PRIVATE FULL-FIELD TEST: SUCCESS")
        print("Verified one known booking ID: 1658164")
        print(f"Detail fields captured: {sum(value is not None for value in record['booking_detail'].values())}")
        print(f"Case groups: {len(record['case_groups'])}")
        print(f"Charge entries: {sum(len(g['charges']) for g in record['case_groups'])}")
        print(f"Stored privately at: {PRIVATE_KEY}")
        print("Production archive and public website were NOT modified")
        return 0
    except Exception as exc:
        # Error details might contain personal data or URLs; print class only.
        print(f"PRIVATE FULL-FIELD TEST FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
