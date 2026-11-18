import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from shared.interfaces import ToolCall

from .sleep_analysis.agent import create_agent
from .sleep_analysis.qwen import QwenPlanner
from .sleep_analysis.sleep_stage import AuthorizedEpoch, SleepStageRequest, SleepStageRuntime, STAGE_SKILL
from .test_sleep_analysis import TestModelClient, model_finish, scope


@pytest.fixture
def asset(tmp_path):
    path = tmp_path / 'epoch.npy'
    np.save(path, np.arange(1920, dtype=np.float32).reshape(1, 1, 1920))
    model = tmp_path / 'model.cix'
    model.write_bytes(b'unit-test-only')
    request = SleepStageRequest(dataset_id='test', subject_id=12, epoch_id=1)
    grant = AuthorizedEpoch(request, path, hashlib.sha256(path.read_bytes()).hexdigest(), 'a'*64, 0, 'Wake')
    return grant, SleepStageRuntime(model, hashlib.sha256(model.read_bytes()).hexdigest())


def test_stage_authorization_before_npu(asset, monkeypatch):
    from .sleep_analysis import sleep_stage
    grant, runtime = asset
    def unexpected(*args):
        pytest.fail('unauthorized request reached NPU')
    monkeypatch.setattr(sleep_stage, 'run_noe', unexpected)
    request = grant.request.model_copy(update={'epoch_id': 2})
    with pytest.raises(sleep_stage.StageAccessDenied):
        runtime.infer(request, {grant.key: grant})


@pytest.mark.parametrize('scenario', ['success', 'unavailable', 'hash_mismatch'])
def test_stage_result_completion_and_prediction_mapping(scope, asset, monkeypatch, scenario):
    from .sleep_analysis import sleep_stage
    grant, runtime = asset
    def infer(model, x):
        if scenario == 'unavailable':
            raise ImportError('unit-test missing NOE')
        assert x.shape == (1, 1, 1920)
        return [0.0, -1.0, 0.2, 0.7, 0.1], {'api_events': [{'api': 'unit_test', 'status': 'test_only'}]}
    monkeypatch.setattr(sleep_stage, 'run_noe', infer)
    if scenario == 'hash_mismatch':
        grant.input_path.write_bytes(b'changed')
    rounds = []
    def plan(prompt):
        rounds.append(prompt)
        if len(rounds) == 1 or prompt['completion']['pending'] is None:
            return model_finish(prompt)
        return {'action': 'call', 'call': {'call_id': prompt['next_call_id'], 'name': STAGE_SKILL,
                                          'arguments': grant.request.model_dump()}}
    agent = create_agent(QwenPlanner(TestModelClient(plan), 'test only'), stage_runtime=runtime)
    result = asyncio.run(agent.run_authorized(
        target_window=scope, authorized_scope=scope, completion_goal='infer_sleep_stage', authorized_epochs=(grant,),
    ))
    assert result.termination == 'finished'
    assert result.finish_rejections[0].reason == 'authorized_epoch_sleep_stage'
    data = result.trace[0].result.data
    if scenario == 'success':
        assert data['predicted_stage'] == 'N3' and data['human_annotation'] == 'Wake'
        assert data['backend'] == 'CIX NOE NPU'
    else:
        assert data['predicted_stage'] is None and data['status'] == 'unavailable'
    report = result.finish.report
    assert report.findings[0].result == result.trace[0].result and report.analysis_scope is None
    assert 'experimental_low_accuracy_model' in report.uncertainty_reasons
    assert '21.63%' in report.answer


def test_stage_runtime_is_opt_in(scope, asset):
    class Planner:
        async def plan(self, request, skills, trace):
            assert len(skills) == 4
            return ToolCall(call_id=uuid4(), name=STAGE_SKILL,
                            arguments=SleepStageRequest(dataset_id='test', subject_id=12, epoch_id=1).model_dump())
    agent = create_agent(Planner(), stage_runtime=asset[1])
    agent.max_calls = 1
    result = asyncio.run(agent.run_authorized(target_window=scope, authorized_scope=scope))
    assert result.trace[0].result.error == 'unknown_skill'
