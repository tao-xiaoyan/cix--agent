import hashlib
import json
import os
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.db import AnalysisResult, Base, EventRow, TaskRow, now
from app.main import create_app
from app.security import encode_cursor, scope
from app.worker import MockWorker, run_one

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture
def env(tmp_path):
    identities = [{'subject': 'mock-device-001', 'role': 'device', 'devices': ['mock-device-001'],
                   'token_sha256': hashlib.sha256(b'device-token').hexdigest()},
                  {'subject': 'operator', 'role': 'operator', 'devices': ['mock-device-001', 'second'],
                   'token_sha256': hashlib.sha256(b'operator-token').hexdigest()}]
    url = os.getenv('TEST_DATABASE_URL', 'sqlite:///' + (tmp_path / 'test.sqlite').as_posix())
    # PostgreSQL tests only ever touch a temporary schema, not application tables.
    schema = None
    if url.startswith('postgresql'):
        from sqlalchemy import create_engine, text
        engine = create_engine(url)
        schema = 'test_' + uuid4().hex
        with engine.begin() as conn:
            conn.execute(text('CREATE SCHEMA ' + schema))
        url += ('&' if '?' in url else '?') + 'options=-csearch_path%3D' + schema
    settings = Settings(url, 'test-cursor-secret-' * 3, identities, worker_enabled=False)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer device-token'
        yield client, app, settings
    if schema:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
        engine.dispose()


def examples():
    return json.loads((ROOT.parent / 'shared/examples/events.json').read_text(encoding='utf-8'))


def upload(client, events):
    response = client.post('/api/v1/events/batch', json={'events': events})
    assert response.status_code == 200, response.text
    return response.json()['results']


def test_health_auth_and_scope(env):
    client, app, _ = env
    assert client.get('/health').json()['status'] == 'ok'
    assert client.get('/api/v1/events', headers={'Authorization': ''}).status_code == 401
    assert client.get('/api/v1/events', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    assert client.get('/api/v1/events?device_id=second').status_code == 403
    assert upload(client, [{**examples()[0], 'device_id': 'second'}])[0]['error']['code'] == 'device_mismatch'
    assert client.get('/api/v1/dashboard').status_code == 403


def test_partial_upload_dedup_and_persistence(env):
    client, app, _ = env
    events = examples()
    results = upload(client, events + [{'device_id': 'mock-device-001', 'event_type': 'bad'}])
    assert [r['status'] for r in results] == ['accepted'] * 8 + ['rejected']
    assert all(r['status'] == 'duplicate' for r in upload(client, events))
    changed = deepcopy(events[0]); changed['confidence'] = 0.5
    assert upload(client, [changed])[0]['error']['code'] == 'event_id_conflict'
    with app.state.factory() as session:
        assert session.scalar(select(func.count()).select_from(EventRow)) == 8
    assert client.get('/api/v1/events/' + events[0]['event_id']).json()['event_id'] == events[0]['event_id']
    assert client.get('/api/v1/events/' + str(uuid4())).status_code == 404


def test_utc_normalized_duplicate(env):
    client, _, _ = env
    event = examples()[0]
    assert upload(client, [event])[0]['status'] == 'accepted'
    event['timestamp'] = '2026-10-30T08:00:00+08:00'
    assert upload(client, [event])[0]['status'] == 'duplicate'


def test_validation_limits_and_request_id(env):
    client, _, _ = env
    for body, status in [('{', 400), ('{}', 422), ('{"events":[]}', 422)]:
        response = client.post('/api/v1/events/batch', content=body, headers={'Content-Type': 'application/json'})
        assert response.status_code == status
        assert response.headers['x-request-id'] == response.json()['request_id']
    assert client.post('/api/v1/events/batch', content='x' * (1024 * 1024 + 1)).status_code == 413
    assert client.post('/api/v1/events/batch', json={'events': [examples()[0]] * 101}).status_code == 422
    event = examples()[0]; event['confidence'] = 2
    assert upload(client, [event])[0]['error']['code'] == 'validation_error'
    event = examples()[0]; event['schema_version'] = '9'
    assert upload(client, [event])[0]['error']['code'] == 'unsupported_schema'


def test_filters_pagination_snapshot_and_cursor_binding(env):
    client, _, _ = env
    upload(client, examples())
    first = client.get('/api/v1/events?limit=3').json()
    assert len(first['items']) == 3
    late = deepcopy(examples()[0]); late['event_id'] = str(uuid4())
    upload(client, [late])
    cursor = first['next_cursor']
    second = client.get('/api/v1/events', params={'limit': 10, 'cursor': cursor}).json()
    assert len(second['items']) == 5
    assert late['event_id'] not in [e['event_id'] for e in second['items']]
    assert client.get('/api/v1/events', params={'cursor': cursor, 'event_type': 'audio_event'}).status_code == 400
    assert client.get('/api/v1/events', params={'cursor': cursor + 'x'}).status_code == 400
    params = {'start': '2026-10-30T00:00:00Z', 'end': '2026-10-30T00:00:01Z', 'event_type': 'audio_event'}
    assert len(client.get('/api/v1/events', params=params).json()['items']) == 1
    params['end'] = params['start']
    assert client.get('/api/v1/events', params=params).status_code == 422
    assert client.get('/api/v1/events', params={'end': '2026-10-30T00:00:00Z'}).json()['items'] == []


def task_body():
    return {'device_id': 'mock-device-001', 'start': '2026-10-29T00:00:00Z', 'end': '2026-10-31T00:00:00Z'}


def test_task_lifecycle_idempotency_and_result(env):
    client, app, _ = env
    upload(client, examples())
    body = task_body()
    headers = {'Idempotency-Key': 'once'}
    created = client.post('/api/v1/analysis/tasks', json=body, headers=headers)
    assert created.status_code == 202, created.text
    task = created.json()
    assert task['status'] == 'queued'
    assert client.post('/api/v1/analysis/tasks', json=body, headers=headers).json()['task_id'] == task['task_id']
    assert client.post('/api/v1/analysis/tasks', json={**body, 'end': '2026-11-01T00:00:00Z'}, headers=headers).status_code == 409
    assert run_one(app.state.factory)
    final = client.get('/api/v1/analysis/tasks/' + task['task_id']).json()
    assert final['status'] == 'succeeded'
    result = client.get('/api/v1/events/' + final['result_event_id']).json()
    assert result['source'] == 'mock' and 'MOCK' in result['payload']['summary']
    with app.state.factory() as session:
        assert session.scalar(select(func.count()).select_from(AnalysisResult)) == 1
    assert not run_one(app.state.factory)
    assert client.get('/api/v1/analysis/tasks').json()['items'][0]['task_id'] == task['task_id']


def test_restart_marks_running_failed(env):
    client, app, _ = env
    task = client.post('/api/v1/analysis/tasks', json=task_body(), headers={'Idempotency-Key': 'restart'}).json()
    with app.state.factory.begin() as session:
        session.get(TaskRow, task['task_id']).status = 'running'
    worker = MockWorker(app.state.factory)
    worker.start(); worker.stop()
    assert client.get('/api/v1/analysis/tasks/' + task['task_id']).json()['status'] == 'failed'


def test_dashboard_and_devices(env):
    client, _, _ = env
    upload(client, examples())
    devices = client.get('/api/v1/devices').json()['items']
    assert devices[0]['connectivity'] == 'online'
    response = client.get('/api/v1/dashboard', headers={'Authorization': 'Bearer operator-token'})
    assert response.json()['event_count'] == 8
    assert response.json()['anomaly_count'] == 1


def test_migration_roundtrip(tmp_path, monkeypatch):
    url = 'sqlite:///' + (tmp_path / 'migration.sqlite').as_posix()
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.setenv('CURSOR_SECRET', 'test-' * 10)
    monkeypatch.setenv('CLOUD_IDENTITIES', '[]')
    cfg = Config(str(ROOT / 'backend/alembic.ini'))
    command.upgrade(cfg, 'head')
    from sqlalchemy import create_engine, inspect
    engine = create_engine(url)
    assert {'devices', 'events', 'analysis_tasks', 'analysis_results'} <= set(inspect(engine).get_table_names())
    command.downgrade(cfg, 'base')
    assert 'events' not in inspect(engine).get_table_names()
    engine.dispose()

