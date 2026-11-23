from pathlib import Path
from collector import parse_booking_html, normalize_date

URL = 'https://apps.mcso.us/PAID/Home/Booking/9999999'

def test_fake_fixture():
    record = parse_booking_html(Path('fixtures/fake_booking.html').read_text(), URL)
    assert record['name'] == 'EXAMPLE, JANE'
    assert record['booking_id'] == '9999999'
    assert record['booking_date'] == '2026-11-21'
    assert record['charges'] == ['DEMONSTRATION ONLY']

def test_reject_unlabeled_or_invalid_page():
    assert parse_booking_html('<h1>Someone with unknown details</h1>', URL) is None
    assert parse_booking_html('<p>Name: Person</p><p>Booking Date: 11/21/2026</p>', 'https://example.com/not-official') is None

def test_parse_date():
    assert normalize_date('11/22/2026 1:15 PM') == '2026-11-22'
    assert normalize_date('unavailable') is None
