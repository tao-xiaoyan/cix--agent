"""Workbench API plumbing tests. These do not execute a model or a board."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from shared.interfaces import ToolResult

from . import agent_workbench, app as app_module
from .sleep_analysis.agent import RunResult
from .sleep_analysis.depth_posture import DEPTH_SKILL, DepthPostureOutput

RUNS = '/api/v1/agent/runs'
QUERY = '请分析受试者 101 的第 8 个深度姿态样本，告诉我是什么睡姿，并提供推理依据。'


def test_session_origin_validation_and_unconfigured_runner(tmp_path, monkeypatch):
    monkeypatch.setenv('EDGE_OPERATOR_TOKEN', 'test-operator')
    monkeypatch.delenv('EDGE_DEPTH_RUN_DIR', raising=False)
    app = app_module.create_app(tmp_path / 'events.sqlite3', mock=False)
    with TestClient(app) as client:
        key = {'Idempotency-Key': str(uuid4())}
        assert client.post(RUNS, json={'query': QUERY}, headers=key).status_code == 401
        assert client.post('/api/v1/session', headers={'Authorization': 'Bearer test-operator'}).status_code == 200
        assert client.post(RUNS, json={'query': QUERY}, headers={**key, 'Origin': 'https://other.invalid'}).status_code == 403
        assert client.post(RUNS, json={'query': QUERY, 'path': '/tmp/input'}, headers=key).status_code == 422
        assert client.post(RUNS, json={'query': QUERY}, headers=key).status_code == 503
        assert client.get(f'{RUNS}/{key["Idempotency-Key"]}').status_code == 404


def test_idempotency_status_retrieval_and_cancel_cleanup(tmp_path, monkeypatch):
    started = []
    cleaned_up = []

    async def wait_for_cancel(snapshot):
        started.append(snapshot.run_id)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned_up.append(snapshot.run_id)

    monkeypatch.setattr(agent_workbench, 'configured_runner', lambda _: wait_for_cancel)
    app = app_module.create_app(tmp_path / 'events.sqlite3', mock=False)
    run_id = uuid4()
    key = {'Idempotency-Key': str(run_id)}
    path = f'{RUNS}/{run_id}'
    with TestClient(app) as client:
        assert client.post('/api/v1/session', headers={'Authorization': 'Bearer local-demo-token'}).status_code == 200
        response = client.post(RUNS, json={'query': QUERY}, headers=key)
        assert response.status_code == 202
        assert response.json()['run_id'] == str(run_id)
        assert response.json()['status'] in ('submitted', 'running')
        assert client.get(path).json()['status'] == 'running'
        assert client.post(RUNS, json={'query': QUERY}, headers=key).json()['run_id'] == str(run_id)
        assert client.post(RUNS, json={'query': 'other'}, headers=key).status_code == 409
        assert client.post(RUNS, json={'query': QUERY}, headers={'Idempotency-Key': str(uuid4())}).status_code == 429
        assert client.delete(path).status_code == 200
        deadline = time.monotonic() + 2
        while (snapshot := client.get(path).json())['status'] != 'cancelled' and time.monotonic() < deadline:
            time.sleep(.01)
        assert snapshot['status'] == 'cancelled'
        assert snapshot['finished_at']
        assert snapshot['evidence'] is None
        assert started == cleaned_up == [run_id]


def test_depth_evidence_projection_and_unverified_termination():
    # A synthetic result exercises the same projection without any private board logs.
    target = agent_workbench.TARGET
    execution_id = uuid4()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=1)
    output = DepthPostureOutput(
        **target.model_dump(),
        status='success',
        predicted_posture='supine',
        dequantized_logits=(2.0, 1.0, 0.0),
        inference_device='CIX P1 NPU on synthetic-board',
        input_sha256='a' * 64,
        evidence_reference=f'{execution_id}/result.json',
        execution_id=execution_id,
        runtime_ms=1.0,
    )
    tool_result = ToolResult(call_id=execution_id, status='succeeded', data=output.model_dump(mode='json'))
    result = RunResult.model_validate({
        'run_id': uuid4(),
        'request': {
            'dataset_id': target.dataset_id, 'session_id': 'synthetic',
            'window': {'start': start, 'end': end},
        },
        'termination': 'finished',
        'finish': {
            'reason': 'verified',
            'report': {
                'answer': 'Synthetic supine result',
                'findings': [{'skill': DEPTH_SKILL, 'result': tool_result}],
                'uncertainty_reasons': [],
            },
        },
        'trace': [{
            'step': 1,
            'started_at': start,
            'finished_at': end,
            'call': {'call_id': execution_id, 'name': DEPTH_SKILL, 'arguments': target.model_dump(mode='json')},
            'result': tool_result,
        }],
    })
    snapshot = agent_workbench.RunSnapshot(run_id=uuid4(), query=QUERY)
    agent_workbench.publish_result(snapshot, result)
    assert snapshot.status == 'completed'
    assert snapshot.agent_run_id == result.run_id
    assert snapshot.selected_skill == 'depth_posture_analysis'
    evidence = snapshot.evidence
    assert evidence and evidence.predicted_posture == 'supine'
    assert evidence.execution_id == result.trace[0].result.call_id
    assert evidence.input_sha256 == result.trace[0].result.data['input_sha256']
    assert evidence.model_sha256 == result.trace[0].result.data['model_sha256']
    assert 'dequantized_logits' not in evidence.model_dump()

    unverified = result.model_copy(update={'termination': 'incomplete_evidence'})
    snapshot = agent_workbench.RunSnapshot(run_id=uuid4(), query=QUERY)
    agent_workbench.publish_result(snapshot, unverified)
    assert snapshot.status == 'failed'
    assert snapshot.evidence is snapshot.answer is None
