import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from archive_store import clean_record, empty_state, merge_state, suppress_record, export_site, ArchiveStore

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def record(booking_id: str, day: str, name='A REAL-LENGTH NAME', charges=None):
    return {
        'booking_id': booking_id, 'booking_date': day, 'name': name,
        'charges': charges or ['CHARGE TEXT'], 'facility': 'MCDC',
        'source_url': f'https://apps.mcso.us/PAID/Home/Booking/{booking_id}',
    }


def test_archive_grows_and_sorts_latest_first(monkeypatch):
    monkeypatch.setenv('ARCHIVE_RETENTION_DAYS', '730')
    original = merge_state(empty_state(NOW), [record('11', '2026-11-21')], NOW)
    later = merge_state(original, [record('12', '2026-11-22')], NOW)
    assert [r['booking_id'] for r in later['bookings']] == ['12', '11']
    assert len(later['bookings']) == 2
    assert later['first_collection_utc'] == original['first_collection_utc']
    updated = merge_state(later, [record('11', '2026-11-21', charges=['NEW CHARGE'])], NOW)
    assert len(updated['bookings']) == 2
    assert updated['bookings'][1]['charges'] == ['NEW CHARGE']


def test_retention_and_sensitive_fields(monkeypatch):
    monkeypatch.setenv('ARCHIVE_RETENTION_DAYS', '365')
    old = record('1', '2024-01-01')
    good = record('2', '2026-11-20')
    good.update({'date_of_birth':'01/01/1980', 'street_address': 'Private Street', 'race':'Unknown', 'social_security_number': '000'})
    state = merge_state(empty_state(NOW), [old, good], NOW)
    assert len(state['bookings']) == 1
    assert set(state['bookings'][0]) == {'booking_id','name','booking_date','charges','facility','source_url'}


def test_suppression_survives_new_collection_and_export(monkeypatch, tmp_path):
    monkeypatch.setenv('ARCHIVE_RETENTION_DAYS', '730')
    state = merge_state(empty_state(NOW), [record('22', '2026-11-21'), record('44', '2026-11-21')], NOW)
    suppressed = suppress_record(state, '22')
    updated = merge_state(suppressed, [record('22', '2026-11-21')], NOW)
    assert [r['booking_id'] for r in updated['bookings']] == ['44']
    assert updated['suppressed_booking_ids'] == ['22']
    manifest = export_site(updated, tmp_path, NOW)
    assert manifest['total'] == 1
    assert all(r['booking_id'] != '22' for r in json.loads((tmp_path/'recent.json').read_text())['bookings'])
    assert all(r['booking_id'] != '22' for r in json.loads((tmp_path/'archive/2026.json').read_text())['bookings'])
    assert manifest['total'] == 1
    assert updated['first_collection_utc'] == manifest['coverage_start_utc']


def test_no_silent_archive_reset(monkeypatch, tmp_path):
    monkeypatch.setenv('ARCHIVE_BACKEND', 'local')
    monkeypatch.setenv('ARCHIVE_FILE', str(tmp_path/'private/archive.json'))
    store = ArchiveStore()
    with pytest.raises(FileNotFoundError): store.read()
    store.initialize()
    with pytest.raises(FileExistsError): store.initialize()
    state = store.read()
    assert state['bookings'] == []


def test_reject_bad_or_injected_details(monkeypatch):
    monkeypatch.setenv('ARCHIVE_RETENTION_DAYS', '730')
    r = record('33', '2026-11-20')
    r['source_url'] = 'https://malicious.example/redirect'
    assert clean_record(r, NOW, 730) is None
    r['source_url'] = 'https://apps.mcso.us/PAID/Home/Booking/33'
    r['booking_date'] = '2026-33-33'
    assert clean_record(r, NOW, 730) is None


def test_archive_index_is_sharded_by_year(monkeypatch, tmp_path):
    monkeypatch.setenv('ARCHIVE_RETENTION_DAYS','0')
    now = NOW
    state = merge_state(empty_state(now), [record('2','2026-11-21'), record('3','2025-03-02')], now)
    manifest = export_site(state, tmp_path, now)
    assert [x['year'] for x in manifest['years']] == ['2026','2025']
    assert len(json.loads((tmp_path/'recent.json').read_text())['bookings']) == 1
    assert len(json.loads((tmp_path/'archive/2025.json').read_text())['bookings']) == 1
