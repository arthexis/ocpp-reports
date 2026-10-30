"""Tenant restrictions cover both warehouse formats, pages and the JSON API."""
import json

import pytest
from fastapi.testclient import TestClient

from ocpp_reports.collector import create_collector
from ocpp_reports.follow import sync_page
from ocpp_reports.web import create_app
from ocpp_reports.warehouse_queries import snapshot
from test_web import empty_page


def populate(database):
    for source in ('gway-002', 'gway-005', 'porsche-abb-1'):
        page = empty_page()
        page['data']['source_id'] = source
        page['data']['status']['chargers'][0]['id'] = 'cp-' + source
        sync_page(database, page)


def test_scopes_enforced_for_all_sections_and_query_filters(tmp_path):
    database = tmp_path / 'reports.sqlite'
    populate(database)
    import sqlite3
    with sqlite3.connect(database) as db:
        for source in ('gway-002', 'gway-005', 'porsche-abb-1'):
            payload = json.dumps({'id': 1, 'charger_id': 'cp-' + source})
            db.execute('INSERT INTO events VALUES (?,?,?)', (source, 1, payload))
            db.execute('INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)',
                       (source, 'cp-' + source, 1, 0, 0, '1', payload, '2026-01-01'))
    for allowed, forbidden in [(('gway-002',), 'gway-005'),
                               (('gway-005', 'porsche-abb-1'), 'gway-002')]:
        client = TestClient(create_app(database, allowed_source_ids=allowed, title='Customer'))
        page = client.get('/')
        assert page.status_code == 200
        assert '<h1>Customer</h1>' in page.text
        assert 'cp-' + forbidden not in page.text
        data = client.get('/api/snapshot').json()
        assert {r['source_id'] for r in data['sources']} == set(allowed)
        for key in ('connectors', 'transactions', 'events'):
            assert {r['source_id'] for r in data[key]} == set(allowed)
        for path in ('/', '/api/snapshot'):
            assert client.get(path, params={'source_id': forbidden}).status_code == 403
            assert client.get(path, params={'source_id': forbidden, 'charger_id': 'cp-' + forbidden}).status_code == 403
        assert client.get('/api/snapshot', params={'source_id': allowed[0]}).json()['sources'][0]['source_id'] == allowed[0]
        assert client.get('/api/snapshot', params={'charger_id': 'cp-' + forbidden}).json()['connectors'] == []


def test_empty_scope_fails_closed(tmp_path):
    with pytest.raises(ValueError, match='allowlist'):
        create_app(tmp_path / 'missing', allowed_source_ids=())
    with pytest.raises(ValueError, match='allowlist'):
        snapshot(tmp_path / 'missing', allowed_source_ids=())


def test_replication_scopes(tmp_path):
    from test_collector import batch
    database = tmp_path / 'reports.sqlite'
    for source in ('gway-002', 'gway-005'):
        document = batch()
        document['source_id'] = source
        document['changes'][0]['payload']['connector_id'] = 1
        document['changes'].extend([
            {'sequence': 2, 'kind': 'transaction', 'record_key': 'cp-1:1',
             'payload': {'id': 1, 'charger_id': 'cp-1'}},
            {'sequence': 3, 'kind': 'event', 'record_key': '1',
             'payload': {'id': 1, 'charger_id': 'cp-1'}}])
        document['next'] = 3
        collector = TestClient(create_collector(database, {source: 'test-token'}))
        assert collector.post('/api/replication', json=document,
                              headers={'Authorization': 'Bearer test-token'}).status_code == 200
    client = TestClient(create_app(database, allowed_source_ids=('gway-002',)))
    data = client.get('/api/snapshot').json()
    assert {r['source_id'] for r in data['sources']} == {'gway-002'}
    assert all(data.values())
    for rows in data.values():
        assert all(r['source_id'] == 'gway-002' for r in rows)
