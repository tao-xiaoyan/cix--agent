import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared.interfaces import ToolCall
from .sleep_analysis.agent import Finish, GroundedReport, LocalToolExecutor, ReportFinding, SkillRegistry, create_agent
from .sleep_analysis.contracts import WindowRequest
from .sleep_analysis.depth_posture import (
    DATASET, DEPTH_SKILL, MODEL, AuthorizedDepthSample, DepthPostureOutput, DepthPostureRequest,
    DepthPostureRuntime, read,
)
from .sleep_analysis.skills import depth_posture_skill


REQUEST = DepthPostureRequest(dataset_id=DATASET, subject_id=101, pose_id=8, model_version=MODEL)
GRANT = AuthorizedDepthSample(REQUEST, 'a' * 64, 'b' * 64)


def scope():
    start = datetime.now(timezone.utc)
    return WindowRequest(dataset_id=DATASET, session_id='subject_101',
                         window={'start': start, 'end': start + timedelta(seconds=1)})


def call(**updates):
    return ToolCall(call_id=uuid4(), name=DEPTH_SKILL, arguments=REQUEST.model_dump() | updates)


def success(call_id):
    return DepthPostureOutput(
        **REQUEST.model_dump(), status='success', predicted_posture='supine',
        dequantized_logits=(2., 1., -1.), inference_device='CIX P1 NPU on orangepi6plus',
        evidence_reference=f'{call_id}/result.json', execution_id=call_id, runtime_ms=1, input_sha256=GRANT.input_sha256,
    )


@pytest.mark.parametrize('arguments', [{'pose_id': True}, {'path': '../secret'}, {'model_version': 'other'}])
def test_bad_arguments_do_not_invoke_runtime(arguments):
    class Never:
        async def infer(self, *args):
            pytest.fail('schema failure must stop before NPU')
    executor = LocalToolExecutor(SkillRegistry([depth_posture_skill(Never(), (GRANT,))]), scope(), 1)
    result = asyncio.run(executor.execute(call(**arguments)))
    assert (result.status, result.error, result.data['predicted_posture']) == ('denied', 'invalid_arguments', None)


def test_unregistered_input_denied_before_start(tmp_path):
    runtime = DepthPostureRuntime(python=Path('unused'), interface=Path('unused'), assets=tmp_path,
                                 evidence_root=tmp_path / 'evidence')
    executor = LocalToolExecutor(SkillRegistry([depth_posture_skill(runtime, (GRANT,))]), scope(), 1)
    result = asyncio.run(executor.execute(call(pose_id=1)))
    assert (result.status, result.error) == ('denied', 'depth_posture_not_authorized')
    assert result.data['predicted_posture'] is None and not runtime.evidence_root.exists()


@pytest.mark.parametrize('status', ['timeout', 'failed', 'unavailable'])
def test_worker_failure_stays_failed(status):
    class Failing:
        async def infer(self, request, authorized, call_id):
            return DepthPostureOutput(
                **request.model_dump(), status=status, reason='controlled_failure',
                execution_id=call_id, evidence_reference=f'{call_id}/result.json', runtime_ms=1,
            )
    executor = LocalToolExecutor(SkillRegistry([depth_posture_skill(Failing(), (GRANT,))]), scope(), 1)
    result = asyncio.run(executor.execute(call()))
    assert result.status == 'failed' and result.data['status'] == status
    assert result.data['predicted_posture'] is None


def test_success_schema_rejects_inconsistent_class_or_evidence_reference():
    output = success(uuid4()).model_dump()
    for item in ({'predicted_posture': 'right'}, {'evidence_reference': '../other/result.json'},
                 {'dequantized_logits': (1., 2., float('nan'))}):
        with pytest.raises(ValidationError):
            DepthPostureOutput.model_validate(output | item)


@pytest.mark.parametrize('valid', [True, False])
def test_agent_report_checks_authorized_evidence_again(valid):
    class Proof:
        async def infer(self, request, authorized, call_id):
            return success(call_id)

        def verifies(self, result, grant):
            assert grant == GRANT and result.input_sha256 == GRANT.input_sha256
            return valid

    class Planner:
        async def plan(self, request, skills, trace):
            if not trace:
                assert DEPTH_SKILL in {item['name'] for item in skills}
                return call()
            return Finish(reason='done', report=GroundedReport(
                answer='wrong generated right label', uncertainty_reasons=[],
                findings=[ReportFinding(skill=DEPTH_SKILL, result=trace[0].result)],
            ))

    target = scope()
    agent = create_agent(Planner(), depth_runtime=Proof())
    result = asyncio.run(agent.run_authorized(target_window=target, authorized_scope=target,
                                              depth_target=REQUEST, authorized_depth=(GRANT,),
                                              completion_goal='infer_depth_posture'))
    if valid:
        assert result.termination == 'finished'
        assert 'supine' in result.finish.report.answer and 'right' not in result.finish.report.answer
    else:
        assert result.termination == 'incomplete_evidence' and result.finish is None


def test_cancelled_inference_publishes_no_posture(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        runtime = DepthPostureRuntime(python=Path('unused'), interface=Path('unused'), assets=tmp_path,
                                      evidence_root=tmp_path / 'evidence')

        async def wait(*args):
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(runtime, '_invoke', wait)
        call_id = uuid4()
        task = asyncio.create_task(runtime.infer(REQUEST, {REQUEST.sample_id: GRANT}, call_id))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        output = read(runtime.evidence_root / str(call_id) / 'result.json')['result']
        assert output['status'] == 'cancelled' and output['predicted_posture'] is None

    asyncio.run(scenario())
