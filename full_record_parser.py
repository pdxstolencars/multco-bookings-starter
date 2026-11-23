"""Parse full detail fields from a single already-accessible MCSO booking page.

This module does not fetch pages or enumerate booking IDs. Full details are
intended for private review; do not automatically publish raw private records.
"""
from __future__ import annotations

from datetime import datetime, timezone
from bs4 import BeautifulSoup
from collector import parse_booking_html, booking_url_parts

LABEL_MAP = {
    "SWIS ID": "swis_id",
    "Name": "name",
    "Age": "age_displayed",
    "Gender": "gender",
    "Race": "race",
    "Height": "height",
    "Weight": "weight",
    "Hair": "hair",
    "Eyes": "eyes",
    "Arresting Agency": "arresting_agency",
    "Booking Date": "booking_datetime_local",
    "Assigned Facility": "facility",
    "Projected Release Date": "projected_release_date_displayed",
}


def _text(node) -> str | None:
    if node is None:
        return None
    result = " ".join(node.stripped_strings).strip()
    return result or None


def parse_full_record_html(html: str, source_url: str) -> dict:
    """Return validated main record plus all fields/structured charge groups.

    Missing details remain null; does not fabricate absent demographics or status.
    Bail and status are repeated for every charge, not collapsed by case.
    """
    minimal = parse_booking_html(html, source_url)
    if minimal is None:
        raise ValueError("Could not validate booking ID, name, and booking date")
    soup = BeautifulSoup(html, "html.parser")
    hidden = soup.select_one('input#BookingNumber')
    if hidden and hidden.get("value") and hidden["value"] != minimal["booking_id"]:
        raise ValueError("Booking number in HTML does not match source URL")

    detail = {v: None for v in LABEL_MAP.values()}
    for row in soup.select("#booking-detail tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) != 2:
            continue
        key = _text(cells[0])
        value = _text(cells[1])
        mapped = LABEL_MAP.get(key)
        if mapped:
            detail[mapped] = value

    cases = []
    parent = soup.select_one("#charge-info")
    if parent:
        for case_heading in parent.find_all("h3", recursive=False):
            court = _text(case_heading.select_one(".court-case-number b"))
            da = _text(case_heading.select_one(".da-case-number b"))
            citation = _text(case_heading.select_one(".citation-number b"))
            detail_div = case_heading.find_next_sibling("div")
            counts = []
            if detail_div:
                for li in detail_div.select("ol > li"):
                    description = _text(li.select_one(".charge-description-display"))
                    if not description:
                        continue
                    counts.append({
                        "description": description,
                        "bail_displayed": _text(li.select_one(".charge-bail-value")),
                        "status_displayed": _text(li.select_one(".charge-status-value")),
                    })
            if counts:
                cases.append({
                    "court_case_number": court,
                    "da_case_number": da,
                    "citation_number": citation,
                    "charges": counts,
                })

    result = {
        "schema": 2,
        "source": "MCSO PAID booking detail",
        "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **minimal,
        "booking_detail": detail,
        "case_groups": cases,
    }
    # Ensure underlying page agrees with values already independently validated.
    if detail["name"] and detail["name"] != minimal["name"]:
        raise ValueError("Inconsistent booking name")
    if detail["booking_datetime_local"]:
        try:
            full_date = datetime.strptime(detail["booking_datetime_local"], "%m/%d/%Y %I:%M %p").date().isoformat()
        except ValueError:
            raise ValueError("Unrecognized booking timestamp") from None
        if full_date != minimal["booking_date"]:
            raise ValueError("Inconsistent booking date")
    if sum(len(case["charges"]) for case in cases) != len(minimal["charges"]):
        raise ValueError("Structured charge count differs from validated simple list")
    return result
