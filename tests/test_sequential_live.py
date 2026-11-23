from pathlib import Path

import pytest
from sequential_live import (
    AccessStopped, LocalStore, PageResult, ScanError,
    apply_found, apply_missing, candidate_url, classify_page, new_state, scan,
)


def synthetic_detail(bid: int) -> str:
    return f'''<!DOCTYPE html><html><head><title>Booking</title></head><body><div id="right"><div class="page">
        <h2>Booking Information</h2><input id="BookingNumber" value="{bid}"/>
        <div id="booking-detail"><table>
        <tr><td><label>Name</label></td><td>Example, Casey</td></tr>
        <tr><td><label>Booking Date</label></td><td>11/22/2026 08:47 PM</td></tr>
        <tr><td><label>Assigned Facility</label></td><td>MCDC</td></tr>
        <tr><td><label>Arresting Agency</label></td><td>Test Agency</td></tr>
        <tr><td><label>Age</label></td><td>25</td></tr></table></div>
        <div id="charge-info"><h3><span class="court-case-number"><b>None</b></span>
        <span class="da-case-number"><b>None</b></span><span class="citation-number"><b>None</b></span></h3>
        <div><ol><li><span class="charge-description-display">SAMPLE CHARGE</span>
        <span class="charge-bail-value">$0</span><span class="charge-status-value">Unsentenced</span>
        </li></ol></div></div></div></div></body></html>'''


def fake_found(bid):
    return PageResult('FOUND', {'booking_id': str(bid), 'name': 'Example, Casey', 'case_groups': []})


def test_full_details_on_confirmed_page():
    url = candidate_url(1658164)
    found = classify_page(200, synthetic_detail(1658164), 1658164, url)
    assert found.kind == 'FOUND'
    assert found.record['booking_detail']['age_displayed'] == '25'
    assert found.record['booking_detail']['arresting_agency'] == 'Test Agency'
    assert found.record['case_groups'][0]['charges'][0]['description'] == 'SAMPLE CHARGE'


def test_missing_requires_explicit_message():
    assert classify_page(200, '<html><div id="right"><p>Booking not found</p></div></html>', 1658150, candidate_url(1658150)).kind == 'MISSING'
    assert classify_page(404, '<html><p>Booking not found</p></html>', 1658150, candidate_url(1658150)).kind == 'MISSING'
    with pytest.raises(ScanError):
        classify_page(404, '<html><p>Not Found</p></html>', 1658150, candidate_url(1658150))


def test_blocked_and_redirect_cannot_be_counted_missing():
    with pytest.raises(AccessStopped):
        classify_page(200, '<html><p>Verify you are human</p></html>', 1658165, candidate_url(1658165))
    with pytest.raises(AccessStopped):
        classify_page(200, synthetic_detail(1658165), 1658165, 'https://other.site/x')
    with pytest.raises(AccessStopped):
        classify_page(429, '<html/>', 1658165, candidate_url(1658165))


def test_mismatched_id_fails_closed():
    with pytest.raises(ScanError):
        classify_page(200, synthetic_detail(1658164), 1658165, candidate_url(1658165))


def test_range_resumes_through_misses_and_finishes(tmp_path):
    store = LocalStore(tmp_path / 'range.json')
    fetch = lambda x: fake_found(x) if x in (1658100, 1658103, 1658107) else PageResult('MISSING')
    a = scan(store, fetch, 'range', 1658100, 1658107, max_requests=4, delay=0)
    assert a['next_candidate'] == 1658104 and not a['complete']
    assert (store.records / '1658100.json').exists()
    b = scan(store, fetch, 'range', 1658100, 1658107, max_requests=4, delay=0)
    assert b['complete'] and b['next_candidate'] == 1658108
    assert b['found_count'] == 3 and b['missing_count'] == 5
    c = scan(store, fetch, 'range', 1658100, 1658107, max_requests=20, delay=0)
    assert c['found_count'] == 3 and c['run_count'] == 2


def test_daily_stops_at_five_and_rechecks_tail(tmp_path):
    store = LocalStore(tmp_path / 'daily.json')
    calls = []
    def fetch(bid):
        calls.append(bid)
        return fake_found(bid) if bid in (1658165, 1658167) else PageResult('MISSING')
    a = scan(store, fetch, 'daily', 1658165, None, max_requests=50, missing_threshold=5, delay=0)
    assert calls == list(range(1658165, 1658173))
    assert a['next_candidate'] == 1658168
    assert a['found_count'] == 2
    calls.clear()
    b = scan(store, fetch, 'daily', 1658165, None, max_requests=20, missing_threshold=5, delay=0)
    assert calls == list(range(1658168, 1658173))
    assert b['next_candidate'] == 1658168


def test_interrupted_request_does_not_advance(tmp_path):
    store = LocalStore(tmp_path / 'checkpoint.json')
    with pytest.raises(ScanError):
        scan(store, lambda x: (_ for _ in ()).throw(ScanError('timeout')),
             'range', 1658100, 1658101, delay=0)
    assert store.load()['next_candidate'] == 1658100


def test_wrong_range_does_not_overwrite_existing_checkpoint(tmp_path):
    store = LocalStore(tmp_path / 'checkpoint.json')
    scan(store, lambda x: PageResult('MISSING'), 'range', 1658100, 1658102, max_requests=1, delay=0)
    with pytest.raises(ValueError):
        scan(store, lambda x: PageResult('MISSING'), 'range', 1658101, 1658102, delay=0)
    assert store.load()['next_candidate'] == 1658101


def test_save_record_before_checkpoint(tmp_path):
    class FailWrite(LocalStore):
        def save_record(self, record):
            raise OSError('simulated R2 failure')
    store = FailWrite(tmp_path / 'checkpoint.json')
    with pytest.raises(OSError):
        scan(store, lambda x: fake_found(x), 'range', 1658100, 1658100, delay=0)
    assert store.load()['next_candidate'] == 1658100
