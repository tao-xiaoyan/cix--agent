import asyncio
import tempfile
from pathlib import Path
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app as app_module
from shared.interfaces import ModelInput
from shared.schemas.events import Event
from shared.schemas.api import BatchResponse, BatchItemResult


def test_mock_events_validate_and_change():
    generator = app_module.MockGenerator('test-device')
    first, second = generator.next_events(), generator.next_events()
    assert {event.event_type for event in first} >= {'physiological_window', 'pose_estimation', 'sleep_posture', 'audio_event', 'device_metrics'}
    assert first[0].event_id != second[0].event_id
    assert first[2].payload.label != second[2].payload.label
    for event in first + second:
        assert event.source == 'mock'
        TypeAdapter(Event).validate_json(event.model_dump_json())


def test_sqlite_outbox_and_idempotency():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'events.sqlite3'
        store = app_module.Store(path)
        event = app_module.MockGenerator('test-device').next_events()[0]
        assert store.add(event)
        assert not store.add(event)
        assert store.pending_count() == 1
        store.mark_flight([str(event.event_id)])
        recovered = app_module.Store(path)
        assert len(recovered.pending_batch()) == 1
        recovered.mark_result(str(event.event_id), 'acknowledged')
        assert recovered.pending_count() == 0
        assert recovered.recent(app_module.EventQuery(), 'test-device').items[0].event_id == event.event_id


def test_ws_stream_and_auth():
    with tempfile.TemporaryDirectory() as directory:
        application = app_module.create_app(Path(directory) / 'events.sqlite3', mock=True)
        with TestClient(application) as client:
            assert client.get('/health').json()['status'] == 'ok'
            assert client.get('/api/v1/device/status').status_code == 401
            assert client.post('/api/v1/session', headers={'Authorization': 'Bearer local-demo-token'}).status_code == 200
            with client.websocket_connect('/ws/realtime') as socket:
                message = socket.receive_json()
                assert message['kind'] == 'event'
                assert message['sequence'] == 0
                TypeAdapter(Event).validate_python(message['event'])
            assert client.get('/api/v1/events/recent').status_code == 200
            assert client.get('/api/v1/device/status').json()['pending_upload_count'] > 0


def test_rule_decision_and_offline_queue():
    with tempfile.TemporaryDirectory() as directory:
        application = app_module.create_app(Path(directory) / 'events.sqlite3', mock=False)
        runtime = application.state.runtime
        generator = app_module.MockGenerator(runtime.device_id)
        for _ in range(5):
            events = generator.next_events()
        anomaly = next(event for event in events if event.event_type == 'anomaly_event')
        asyncio.run(runtime.record(anomaly))
        recent = runtime.store.recent(app_module.EventQuery(limit=10), runtime.device_id).items
        decision = next(event for event in recent if event.event_type == 'agent_decision')
        assert anomaly.event_id in decision.payload.related_event_ids
        assert 'queue_cloud_analysis' in decision.payload.actions
        assert len(runtime.store.pending_tasks()) == 1
        assert runtime.store.pending_count() == 2


def test_missing_model_adapter():
    request = ModelInput(request_id=uuid4(), device_id='test-device', modality='llm', data={})
    result = asyncio.run(app_module.AdapterRegistry().infer(request))
    assert result.status == 'unavailable'


def test_cloud_batch_acknowledgement_and_offline_retry():
    class FakeCloud:
        def __init__(self):
            self.fail = True

        async def upload(self, events):
            if self.fail:
                raise app_module.httpx.ConnectError('offline')
            return BatchResponse(request_id='test', results=[
                BatchItemResult(index=index, event_id=event['event_id'], status='accepted')
                for index, event in enumerate(events)])

    async def exercise(runtime, fake):
        event = app_module.MockGenerator(runtime.device_id).next_events()[0]
        await runtime.record(event)
        task = asyncio.create_task(runtime.sync())
        await asyncio.sleep(0.1)
        assert runtime.cloud_state == 'offline'
        assert runtime.store.pending_count() == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        fake.fail = False
        with runtime.store.connect() as db:
            db.execute('UPDATE outbox SET retry_at=0')
        task = asyncio.create_task(runtime.sync())
        await asyncio.sleep(0.1)
        assert runtime.cloud_state == 'online'
        assert runtime.store.pending_count() == 0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with tempfile.TemporaryDirectory() as directory:
        fake = FakeCloud()
        runtime = app_module.Runtime(app_module.Store(Path(directory) / 'events.sqlite3'), 'test-device', fake, False)
        asyncio.run(exercise(runtime, fake))
