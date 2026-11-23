from pathlib import Path

import pytest
from sequential_live import (
    AccessStopped, LocalStore, PageResult, ScanError,
    TransientServerError, retry_transient_server_error,
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


def test_daily_stops_at_five_and_advances_beyond_gap(tmp_path):
    store = LocalStore(tmp_path / 'daily.json')
    calls = []
    def fetch(bid):
        calls.append(bid)
        return fake_found(bid) if bid in (1658165, 1658167) else PageResult('MISSING')
    a = scan(store, fetch, 'daily', 1658165, None, max_requests=50, missing_threshold=5, delay=0)
    assert calls == list(range(1658165, 1658173))
    assert a['next_candidate'] == 1658173
    assert a['found_count'] == 2
    assert a['pending_missing_ids'] == [1658166] + list(range(1658168, 1658173))
    calls.clear()
    b = scan(store, fetch, 'daily', 1658165, None, max_requests=20, missing_threshold=5, delay=0)
    assert calls[:5] == list(range(1658173, 1658178))
    assert calls[5:] == [1658166, 1658168, 1658169, 1658170, 1658171]
    assert b['next_candidate'] == 1658178


def test_migrates_real_previous_stopping_checkpoint_and_recovers_late_id(tmp_path):
    store = LocalStore(tmp_path / 'daily.json')
    old = {
        'schema': 1, 'mode':'daily', 'start_id': 1658165, 'end_id':None,
        'next_candidate':1658175, 'highest_found_id':1658174,
        'missing_streak':0, 'found_count':10, 'missing_count':5,
        'run_count':1, 'last_checked_id':1658179, 'last_run_utc':None,
        'complete':False,
    }
    store.save(old)
    calls = []
    def fetch(bid):
        calls.append(bid)
        return fake_found(bid) if bid == 1658176 else PageResult('MISSING')
    result = scan(store, fetch, 'daily', 1658165, None,
                  max_requests=10, missing_threshold=5, delay=0)
    assert calls[:5] == list(range(1658180, 1658185))
    assert calls[5:] == list(range(1658175, 1658180))
    assert result['next_candidate'] == 1658185
    assert 1658176 not in result['pending_missing_ids']
    assert result['found_count'] == 11 and result['missing_count'] == 9
    assert (store.records / '1658176.json').exists()
    assert store.load()['next_candidate'] == 1658185


def test_daily_range_has_no_double_count_for_missing_recheck(tmp_path):
    store = LocalStore(tmp_path / 'daily.json')
    f = lambda bid: PageResult('MISSING')
    a = scan(store, f, 'daily', 1658165, None, max_requests=10, delay=0)
    assert a['found_count'] == 0 and a['missing_count'] == 5
    b = scan(store, f, 'daily', 1658165, None, max_requests=10, delay=0)
    assert b['next_candidate'] == 1658175
    assert b['missing_count'] == 10
    assert b['pending_missing_ids'] == list(range(1658165,1658175))



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


def test_server_error_is_retriable_but_challenge_is_not():
    bid = 1658109
    url = candidate_url(bid)
    with pytest.raises(TransientServerError, match="503"):
        classify_page(503, "<html><body>Service unavailable</body></html>", bid, url)
    with pytest.raises(AccessStopped):
        classify_page(503, "<html><body>Verify you are human: CAPTCHA</body></html>", bid, url)


def test_retry_only_temporary_server_error():
    attempted = []
    waits = []
    def flaky():
        attempted.append(1)
        if len(attempted) < 3:
            raise TransientServerError("HTTP 503")
        return PageResult("MISSING")
    outcome = retry_transient_server_error(flaky, 1658109, sleep=waits.append)
    assert outcome.kind == "MISSING"
    assert len(attempted) == 3
    assert waits == [10.0, 30.0]


def test_retry_exhausted_stops_without_advancing_checkpoint(tmp_path):
    store = LocalStore(tmp_path / "range.json")
    calls = []
    def always_503():
        calls.append(1)
        raise TransientServerError("HTTP 503")
    def fetch(bid):
        return retry_transient_server_error(always_503, bid, backoffs=(0, 0), sleep=lambda _: None)
    with pytest.raises(TransientServerError):
        scan(store, fetch, "range", 1658100, 1658163, delay=0)
    assert len(calls) == 3
    assert store.load()["next_candidate"] == 1658100


def test_access_block_does_not_retry():
    calls = []
    def blocked():
        calls.append(1)
        raise AccessStopped("access denied")
    with pytest.raises(AccessStopped):
        retry_transient_server_error(blocked, 1658109, sleep=lambda _: None)
    assert len(calls) == 1
