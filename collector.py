"""A respectful, fail-closed collector for MCSO's public PAID bookings search.

The actual 2026 MCSO form could not be exercised from the build environment.
Inspect selectors and output at first deployment, and get the site's automation
policy confirmed before running it repeatedly. Never evade access controls.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qsl
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from archive_store import ArchiveStore, merge_state, export_site

BASE = "https://apps.mcso.us/PAID"
MAX_BOOKINGS_PER_RUN = 550
LOG = logging.getLogger("multco_bookings")
URL_RE = re.compile(r"^/PAID/Home/Booking/(\d+)/?$", re.I)


def booking_url_parts(url: str) -> tuple[str, str | None] | None:
    """Validate ONLY official MCSO detail URLs (including release-specific links)."""
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "apps.mcso.us"
            or parts.fragment or parts.username or parts.password):
        return None
    legacy = URL_RE.fullmatch(parts.path)
    if legacy and not parts.query:
        return legacy.group(1), None
    if parts.path != "/PAID/Home/Booking":
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    if len(query) not in (1, 2) or query[0][0] != "id":
        return None
    bid = query[0][1]
    if not re.fullmatch(r"\d{1,12}", bid):
        return None
    if len(query) == 1:
        return bid, None
    if query[1][0] != "releaseId" or not re.fullmatch(r"\d{1,12}", query[1][1]):
        return None
    return bid, query[1][1]


def canonical_booking_url(url: str) -> str | None:
    parts = booking_url_parts(url)
    if parts is None:
        return None
    bid, release_id = parts
    return f"{BASE}/Home/Booking?id={bid}" + (f"&releaseId={release_id}" if release_id else "")

LABELS = {
    "name": {"name", "full name", "person name", "person in custody", "inmate name", "booked name"},
    "booking_date": {"booking date", "date booked", "booked on", "booked date", "booking date/time", "booking datetime", "date/time booked"},
    "facility": {"assigned facility", "facility", "housing facility"},
    "charges": {"charge", "charges", "offense", "offenses", "charge description"},
}
FLAT_LABELS = {alias: field for field, aliases in LABELS.items() for alias in aliases}
DATE_FORMATS = (
    "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%b %d, %Y %I:%M %p", "%b %d, %Y", "%B %d, %Y",
)


def normalize_label(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().rstrip(":").casefold())


def clean(text: str | None, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def normalize_date(raw: str) -> str | None:
    raw = clean(raw, 100)
    raw = re.sub(r"\s+(PST|PDT)$", "", raw, flags=re.I)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})\b", raw)
    if match:
        return normalize_date(match.group(1))
    return None


def parse_booking_html(html: str, url: str) -> dict | None:
    """Conservative parser: suppress records without clearly labeled name and date.

    Intentionally excludes DOB, address, race, aliases, SWIS, and all photos.
    Does NOT use a page's unlabelled heading as proof of identity.
    """
    canonical = canonical_booking_url(url)
    if canonical is None:
        return None
    bid, _release_id = booking_url_parts(canonical)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()

    pairs: list[tuple[str, str]] = []
    # Most common government-detail HTML layouts: <dt>/<dd>, table rows.
    for dt in soup.find_all("dt"):
        sibling = dt.find_next_sibling("dd")
        if sibling:
            pairs.append((dt.get_text(" ", strip=True), sibling.get_text(" ", strip=True)))
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) == 2:
            pairs.append((cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True)))

    # Generic form fields such as <strong>Name:</strong> Jane Doe.
    for el in soup.find_all(["b", "strong", "label"]):
        label = clean(el.get_text(" ", strip=True), 100)
        if normalize_label(label) in FLAT_LABELS:
            value = el.next_sibling
            if isinstance(value, str):
                pairs.append((label, value))
            elif value is not None and getattr(value, "get_text", None):
                pairs.append((label, value.get_text(" ", strip=True)))

    # Flat text layout: Name\nJane Doe\nBooking Date\n11/22/2026 ...
    lines = [clean(s, 200) for s in soup.stripped_strings]
    for idx, line in enumerate(lines):
        if idx < len(lines)-1 and normalize_label(line) in FLAT_LABELS:
            pairs.append((line, lines[idx + 1]))
        if ":" in line:
            k, v = line.split(":", 1)
            if normalize_label(k) in FLAT_LABELS:
                pairs.append((k, v))

    fields: dict[str, str] = {}
    charges: list[str] = []
    for k, v in pairs:
        field = FLAT_LABELS.get(normalize_label(k))
        value = clean(v, 300)
        if not field or not value or normalize_label(value) in FLAT_LABELS:
            continue
        if field == "charges":
            if value not in charges:
                charges.append(value)
        elif field not in fields:
            fields[field] = value

    # MCSO's confirmed 2026 booking page uses one .charge-description-display
    # span per charge, inside #charge-info.  Don't mistake its instructional
    # "Click on each court case..." message for a charge.
    official_charges = [clean(tag.get_text(" ", strip=True), 300)
                        for tag in soup.select("#charge-info .charge-description-display")]
    if official_charges:
        charges = [value for value in official_charges if value]
    else:
        charges = [value for value in charges
                   if not value.casefold().startswith("click on each court case number")]

    name = fields.get("name", "")
    booking_date = normalize_date(fields.get("booking_date", ""))
    if not name or not booking_date or len(name) < 3 or len(name) > 110:
        return None
    # No need to retain data for juveniles even if a record accidentally appears.
    text = soup.get_text(" ", strip=True)
    if re.search(r"\bjuvenile\b|\byouth custody\b", text, flags=re.I):
        return None
    return {
        "booking_id": bid,
        "name": name,
        "booking_date": booking_date,
        "charges": charges[:20],
        "facility": clean(fields.get("facility", ""), 100),
        "source_url": canonical,
        "last_seen_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def allowed_to_scrape(user_agent: str, timeout: int = 20) -> bool:
    """Fail closed if robots.txt cannot be verified. A 404 means no robots file.

    robots permission is not legal permission; seek any required county approval.
    """
    url = "https://apps.mcso.us/robots.txt"
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        if response.status_code == 404:
            return True
        if response.status_code != 200:
            LOG.error("Cannot verify robots rules (HTTP %s)", response.status_code)
            return False
        robots = RobotFileParser()
        robots.parse(response.text.splitlines())
        return robots.can_fetch(user_agent, BASE) and robots.can_fetch(user_agent, BASE + "/Home/Booking?id=1")
    except requests.RequestException as exc:
        LOG.error("Cannot check robots rules: %s", exc)
        return False


def get_page_links(page) -> list[str]:
    """Extract only official detail links, including the observed ?id=123 form.

    The MCSO results page is authoritative; numeric IDs are not guessed,
    incremented, or scraped by sequential enumeration.
    """
    links = page.locator('a[href*="Booking"]')
    urls: set[str] = set()
    for i in range(links.count()):
        href = (links.nth(i).get_attribute("href") or "").strip()
        if href.startswith("/PAID/"):
            absolute = "https://apps.mcso.us" + href
        elif href.startswith("/Home/"):
            absolute = BASE + href
        elif href.startswith("Home/"):
            absolute = BASE + "/" + href
        else:
            absolute = urljoin(page.url, href)
        canonical = canonical_booking_url(absolute)
        if canonical:
            # Preserve releaseId: one booking number may have multiple occurrences.
            urls.add(canonical)
    return sorted(urls, key=lambda u: (int(booking_url_parts(u)[0]), int(booking_url_parts(u)[1] or 0)))


def gather_links(page) -> list[str]:
    """Use the site's documented 'Booked Last 7 Days' form filter.

    There is deliberately NO brute-force booking-ID enumeration.
    """
    page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
    select = page.locator("select").first
    if select.count() == 0:
        raise RuntimeError("MCSO search form changed: no search-type dropdown")
    options = select.locator("option")
    target_value = None
    for i in range(options.count()):
        option = options.nth(i)
        if normalize_label(option.inner_text()) == "booked last 7 days":
            target_value = option.get_attribute("value")
            break
    if target_value is None:
        raise RuntimeError("MCSO no longer lists the 'Booked Last 7 Days' filter")
    select.select_option(value=target_value)

    search = page.locator('button:has-text("Search"), input[type="submit"], input[value="Search"]').first
    if search.count() == 0:
        raise RuntimeError("Cannot identify MCSO public search button; selector needs review")
    search.click()
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(1250)
    if "captcha" in page.locator("body").inner_text().casefold():
        raise RuntimeError("MCSO requires a human verification; do not bypass it")

    all_links: set[str] = set()
    for n in range(15):
        all_links.update(get_page_links(page))
        if len(all_links) > MAX_BOOKINGS_PER_RUN:
            raise RuntimeError("Unexpectedly many results; stop rather than overload MCSO")
        nxt = page.get_by_role("link", name=re.compile(r"^(next|next page|›|»)$", re.I)).first
        if nxt.count() == 0 or not nxt.is_visible():
            break
        old = page.url
        nxt.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(600)
        if page.url == old and n > 0 and not get_page_links(page):
            break
    if not all_links:
        raise RuntimeError("No detail links found in MCSO results; verify page structure before publishing")
    return sorted(all_links)


def collect(contact: str = "", site: Path = Path("site")) -> int:
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact):
        raise ValueError("Set SCRAPER_CONTACT to a real email for respectful bot identification")
    store = ArchiveStore()
    previous = store.read()  # Must exist: never silently reset a missing private archive.
    ua = f"MultCoBookingMonitor/0.2 (public-interest transparency; contact: {contact})"
    if not allowed_to_scrape(ua):
        raise RuntimeError("Automation blocked or robots policy unverified. Request access from MCSO.")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=ua)
            page = context.new_page()
            links = gather_links(page)
            known = {item.get("source_url") for item in previous.get("bookings", [])}
            LOG.info("Official recent results: %d links (%d new record links, %d previously collected)",
                     len(links), sum(link not in known for link in links), sum(link in known for link in links))
            # We do not probe missing consecutive IDs: unpublished/invalid ID values
            # must never be assumed to represent accessible booking records.
            records: list[dict] = []
            failures = 0
            for n, link in enumerate(links, 1):
                try:
                    page.goto(link, wait_until="domcontentloaded", timeout=30000)
                    html = page.content()
                    parsed = parse_booking_html(html, link)
                    if parsed:
                        records.append(parsed)
                    else:
                        failures += 1
                except Exception as exc:
                    LOG.warning("Detail %s could not be read: %s", link, exc)
                    failures += 1
                page.wait_for_timeout(1200)  # <=1 detail request / 1.2 sec
                if n % 25 == 0:
                    LOG.info("Checked %d / %d", n, len(links))
        finally:
            browser.close()
    # An unverified parser must NOT replace a trusted snapshot with bad results.
    if len(records) < 1 or failures / max(1, len(links)) > 0.35:
        raise RuntimeError(f"Data quality gate failed: {len(records)} parsed / {len(links)} links; no file written")
    now = datetime.now(timezone.utc)
    current = merge_state(previous, records, now)
    store.write(current)
    manifest = export_site(current, site, now)
    LOG.info("Stored %d bookings; public archive spans %d years", manifest["total"], len(manifest["years"]))
    return manifest["total"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, default=Path("site"))
    parser.add_argument("--test-html", type=Path, help="Test only: parse saved, non-real HTML fixture")
    parser.add_argument("--test-url", default="https://apps.mcso.us/PAID/Home/Booking?id=9999999")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.test_html:
            print(json.dumps(parse_booking_html(args.test_html.read_text(), args.test_url), indent=2))
        else:
            collect(os.getenv("SCRAPER_CONTACT", ""), args.site)
    except Exception as exc:
        LOG.error("Collection stopped safely: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
