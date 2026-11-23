import io
import json
from datetime import datetime
from pathlib import Path

import pytest
from export_full_archive import (
    public_record, export_records, valid_source, suppress, collect_public_records,
)


def record(bid='1658164', date='2026-11-22', name='Example, Person', captured='2026-11-23T05:00:00+00:00'):
    return {
        'booking_id': bid,
        'name': name,
        'booking_date': date,
        'source_url': f'https://apps.mcso.us/PAID/Home/Booking?id={bid}',
        'captured_utc': captured,
        'charges': ['SAMPLE (C Misdemeanor)'],
        'booking_detail': {
            'swis_id': '12345', 'name': name, 'age_displayed': '25',
            'gender': 'Female', 'race': 'Not listed', 'height': '5 ft 9 in',
            'weight': '130 lbs', 'hair': 'Brown', 'eyes': 'Blue',
            'arresting_agency': 'Test agency', 'booking_datetime_local': '11/22/2026 08:47 PM',
            'facility': 'MCDC', 'projected_release_date_displayed': 'Unknown',
        },
        'case_groups': [{
            'court_case_number': '26CR00000', 'da_case_number': '1', 'citation_number': 'None',
            'charges': [{'description': 'SAMPLE (C Misdemeanor)', 'bail_displayed': '$0', 'status_displayed': 'Unsentenced'}],
        }],
    }


def test_full_details_preserved_and_sorted_timestamp():
    r = public_record(record())
    assert r['booking_sort_local'] == '2026-11-22T20:47:00'
    assert r['booking_detail']['gender'] == 'Female'
    assert r['booking_detail']['race'] == 'Not listed'
    assert r['case_groups'][0]['court_case_number'] == '26CR00000'
    assert r['case_groups'][0]['charges'][0]['bail_displayed'] == '$0'
    assert r['case_groups'][0]['charges'][0]['status_displayed'] == 'Unsentenced'


def test_disallows_untrusted_official_link():
    assert valid_source('https://apps.mcso.us/PAID/Home/Booking?id=1658164', '1658164')
    assert valid_source('https://apps.mcso.us/PAID/Home/Booking?id=1658164&releaseId=99999', '1658164')
    for url in ('http://apps.mcso.us/PAID/Home/Booking?id=1658164',
                'https://evil.example/PAID/Home/Booking?id=1658164',
                'https://apps.mcso.us/PAID/Home/Booking?id=1658165',
                'https://apps.mcso.us/PAID/Home/Booking?id=1658164&bad=1'):
        assert not valid_source(url, '1658164')
    bad = record()
    bad['source_url'] = 'https://evil.example/PAID/Home/Booking?id=1658164'
    with pytest.raises(ValueError):
        public_record(bad)


def test_never_include_unverified_private_fields():
    x = record()
    x['private_api_key'] = 'DO NOT PUBLISH'
    x['personal_home_address'] = 'DO NOT PUBLISH'
    r = public_record(x)
    assert 'DO NOT PUBLISH' not in json.dumps(r)


def test_invalid_charge_structure_fails_closed():
    x = record()
    x['charges'] = ['Something different']
    with pytest.raises(ValueError):
        public_record(x)


def test_export_creates_recent_and_searchable_year(tmp_path):
    r = public_record(record())
    manifest = export_records([r], tmp_path, '2026-11-23T05:00:00+00:00', '2026-11-23T04:00:00+00:00')
    assert manifest['total'] == 1
    archive = json.loads((tmp_path / 'archive' / '2026.json').read_text())
    assert archive['bookings'][0]['booking_detail']['height'] == '5 ft 9 in'
    assert archive['bookings'][0]['case_groups'][0]['charges'][0]['status_displayed'] == 'Unsentenced'
    assert json.loads((tmp_path / 'archive' / 'manifest.json').read_text())['years'][0]['count'] == 1
    assert 'private_api_key' not in (tmp_path / 'archive' / '2026.json').read_text()
    assert (tmp_path / 'recent.json').exists()


def test_empty_export_wont_erase_live_site(tmp_path):
    with pytest.raises(ValueError):
        export_records([], tmp_path, '2026-11-23T05:00:00+00:00', None)
    assert not (tmp_path / 'recent.json').exists()


class FakePaginator:
    def __init__(self, keys): self.keys = keys
    def paginate(self, Bucket, Prefix):
        yield {'Contents': [{'Key': k} for k in self.keys if k.startswith(Prefix)]}

class FakeR2:
    def __init__(self, objects):
        self.objects = {k: (json.dumps(v).encode() if not isinstance(v, bytes) else v) for k, v in objects.items()}
    def get_paginator(self, name):
        assert name == 'list_objects_v2'
        return FakePaginator(self.objects.keys())
    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({'Error': {'Code': 'NoSuchKey', 'Message':'missing'}}, 'GetObject')
        return {'Body': io.BytesIO(self.objects[Key])}
    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body


def test_collect_both_prefixes_and_suppress():
    a = record('1658164', name='First, Person')
    b = record('1658165', name='Second, Person')
    client = FakeR2({
        'multco/sequential-test/records/1658164.json': {'private_full_record':True, 'record':a},
        'multco/sequential-daily/records/1658165.json': {'private_full_record':True, 'record':b},
    })
    records, ids = collect_public_records(client,'test-bucket')
    assert [r['booking_id'] for r in records] == ['1658165', '1658164']
    suppress(client,'test-bucket','1658164')
    records, ids = collect_public_records(client,'test-bucket')
    assert [r['booking_id'] for r in records] == ['1658165']
    assert '1658164' in ids


def test_export_also_includes_verified_single_full_record():
    live = record('1658163', name='Range, Record')
    smoke = record('1658164', name='Known, Record')
    client = FakeR2({
        'multco/sequential-test/records/1658163.json': {'private_full_record': True, 'record': live},
        'multco/full-detail-smoke-1658164.json': {'purpose':'PRIVATE_SINGLE_KNOWN_RECORD_TEST_ONLY', 'record': smoke},
    })
    records, _ = collect_public_records(client, 'test-bucket')
    assert [r['booking_id'] for r in records] == ['1658164', '1658163']
    suppress(client, 'test-bucket', '1658164')
    records, _ = collect_public_records(client, 'test-bucket')
    assert [r['booking_id'] for r in records] == ['1658163']


def test_bad_verified_smoke_fails_closed():
    client = FakeR2({'multco/full-detail-smoke-1658164.json': {'purpose': 'SOME_OTHER_TEST', 'record': record()}})
    with pytest.raises(ValueError, match='Unexpected verified booking'):
        collect_public_records(client, 'test-bucket')
