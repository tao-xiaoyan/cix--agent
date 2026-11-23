"""Asynchronous browser access to the trusted V0.4 depth run."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Awaitable, Callable, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request
from pydantic import Field, StringConstraints

from shared.schemas.events import ContractModel

if __package__:
    from .sleep_analysis import agent, contracts, depth_posture, qwen
else:
    from sleep_analysis import agent, contracts, depth_posture, qwen


log = logging.getLogger(__name__)
QWEN_BASE = 'http://127.0.0.1:18080'
TARGET = depth_posture.DepthPostureRequest(
    dataset_id=depth_posture.DATASET, subject_id=101, pose_id=8, model_version=depth_posture.MODEL,
)
RunStatus = Literal['submitted', 'running', 'completed', 'failed', 'cancelled']


class SubmitRun(ContractModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class VerifiedDepth(ContractModel):
    predicted_posture: Literal['supine', 'left', 'right']
    dataset_id: str
    subject_id: int
    pose_id: int
    model_version: str
    inference_device: str
    execution_id: UUID
    evidence_reference: str
    input_sha256: str
    model_sha256: str
    execution_status: Literal['succeeded']


class RunSnapshot(ContractModel):
    run_id: UUID
    query: str
    status: RunStatus = 'submitted'
    agent_run_id: UUID | None = None
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    selected_skill: Literal['depth_posture_analysis'] | None = None
    answer: str | None = None
    error: str | None = None
    evidence: VerifiedDepth | None = None


class RunnerUnavailable(Exception):
    pass


def configured_runner(operator_token: str):
    variables = ('EDGE_DEPTH_ASSETS', 'EDGE_DEPTH_INTERFACE', 'EDGE_DEPTH_NPU_PYTHON', 'EDGE_DEPTH_RUN_DIR')
    settings = tuple(os.getenv(name) for name in variables)
    if not all(settings) or os.name != 'posix' or operator_token == 'local-demo-token':
        return None
    assets, interface, python, run_dir = (Path(value) for value in settings if value)
    runtime = depth_posture.DepthPostureRuntime(python=python, interface=interface, assets=assets, evidence_root=run_dir / 'evidence')

    async def run(snapshot: RunSnapshot) -> agent.RunResult:
        audit = run_dir / str(snapshot.run_id) / 'agent.jsonl'
        audit.parent.mkdir(mode=0o700, parents=True)
        with audit.open('x', encoding='utf-8') as stream:
            def record(kind, **fields):
                print(json.dumps({'kind': kind, 'run_id': str(snapshot.run_id), **fields},
                                 ensure_ascii=False), file=stream, flush=True)

            record('submitted', query=snapshot.query, submitted_at=snapshot.submitted_at.isoformat())
            try:
                grants = await asyncio.to_thread(runtime.register, (TARGET,))
                async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), trust_env=False) as http:
                    try:
                        response = await http.get(f'{QWEN_BASE}/v1/models', timeout=5)
                        response.raise_for_status()
                        model = response.json()['data'][0]['id']
                        if 'qwen3' not in model.lower():
                            raise ValueError('local model is not Qwen3')
                    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                        raise RunnerUnavailable('qwen_unavailable') from exc
                    client = qwen.ChatCompletionsClient(http, f'{QWEN_BASE}/v1/chat/completions', model, max_tokens=384)
                    now = datetime.now(timezone.utc)
                    scope = contracts.WindowRequest(dataset_id=TARGET.dataset_id, session_id='subject_101',
                                          window={'start': now, 'end': now + timedelta(seconds=1)})
                    runner = agent.create_agent(qwen.QwenPlanner(client, snapshot.query), depth_runtime=runtime,
                                         planner_timeout=120, run_timeout=300)
                    result = await runner.run_authorized(
                        target_window=scope, authorized_scope=scope, completion_goal='infer_depth_posture',
                        depth_target=TARGET, authorized_depth=grants,
                    )
            except asyncio.CancelledError:
                record('cancelled')
                raise
            except Exception as exc:
                record('run_error', error_type=type(exc).__name__)
                raise
            record('agent_result', model=model, result=result.model_dump(mode='json'))
            return result

    return run


def publish_result(snapshot: RunSnapshot, result: agent.RunResult):
    snapshot.agent_run_id = result.run_id
    depth_trace = [entry for entry in result.trace if entry.call.name == depth_posture.DEPTH_SKILL]
    if depth_trace:
        snapshot.selected_skill = depth_posture.DEPTH_SKILL
    report = result.finish.report if result.finish else None
    if result.termination != 'finished' or report is None or len(report.findings) != 1:
        snapshot.error = result.termination
        snapshot.status = 'failed'
        return

    # For this completion goal, 'finished' with this finding is emitted only after
    # SleepAnalysisAgent checks the tool evidence against the frozen grant.
    finding = report.findings[0]
    if (finding.skill != depth_posture.DEPTH_SKILL or finding.result.status != 'succeeded'
            or not any(entry.result == finding.result for entry in depth_trace)):
        raise ValueError('report did not cite the successful depth trace')
    depth = depth_posture.DepthPostureOutput.model_validate(finding.result.data)
    if (depth.status != 'success' or depth.execution_id != finding.result.call_id
            or depth.model_dump(include=set(type(TARGET).model_fields)) != TARGET.model_dump()):
        raise ValueError('verified result did not match the authorized sample')
    snapshot.evidence = VerifiedDepth(
        **depth.model_dump(include=set(VerifiedDepth.model_fields) - {'execution_status'}),
        execution_status=finding.result.status,
    )
    snapshot.answer = report.answer
    snapshot.status = 'completed'


@dataclass
class Job:
    snapshot: RunSnapshot
    task: asyncio.Task | None = None
    cancelling: bool = False


class WorkbenchRuns:
    """One active job and bounded, process-local status snapshots."""

    def __init__(self, runner: Callable[[RunSnapshot], Awaitable[agent.RunResult]] | None):
        self.runner = runner
        self.jobs: dict[UUID, Job] = {}

    def get(self, run_id: UUID) -> Job:
        if run_id not in self.jobs:
            raise HTTPException(404, 'run_unavailable')
        return self.jobs[run_id]

    def submit(self, query: str, key: UUID) -> RunSnapshot:
        # The idempotency key is also the public run ID. A client can query it
        # even if the initial 202 response never reaches the browser.
        if key in self.jobs:
            snapshot = self.jobs[key].snapshot
            if snapshot.query != query:
                raise HTTPException(409, 'idempotency_key_conflict')
            return snapshot
        if self.runner is None:
            raise HTTPException(503, 'depth_runner_unavailable')
        if any(job.snapshot.status in ('submitted', 'running') for job in self.jobs.values()):
            raise HTTPException(429, 'depth_runner_busy')
        if len(self.jobs) >= 64:
            del self.jobs[next(iter(self.jobs))]
        job = Job(RunSnapshot(run_id=key, query=query))
        self.jobs[key] = job
        job.task = asyncio.create_task(self.execute(job, self.runner))
        return job.snapshot

    async def execute(self, job: Job, runner):
        snapshot = job.snapshot
        snapshot.status = 'running'
        try:
            publish_result(snapshot, await runner(snapshot))
        except asyncio.CancelledError:
            snapshot.status = 'cancelled'
            raise
        except RunnerUnavailable as exc:
            snapshot.error = str(exc)
            snapshot.status = 'failed'
        except Exception:
            log.exception('Depth run %s failed', snapshot.run_id)
            snapshot.error = 'depth_execution_unavailable'
            snapshot.status = 'failed'
        finally:
            snapshot.finished_at = datetime.now(timezone.utc)

    def cancel(self, run_id: UUID) -> RunSnapshot:
        job = self.get(run_id)
        if job.task and not job.cancelling and job.snapshot.status in ('submitted', 'running'):
            job.cancelling = True
            if job.snapshot.status == 'submitted':
                job.snapshot.status = 'cancelled'
                job.snapshot.finished_at = datetime.now(timezone.utc)
            job.task.cancel()
        return job.snapshot

    async def close(self):
        tasks = [job.task for job in self.jobs.values() if job.task and not job.task.done()]
        for run_id in self.jobs:
            self.cancel(run_id)
        await asyncio.gather(*tasks, return_exceptions=True)


def routes(runs: WorkbenchRuns, require_auth):
    def authorize(request: Request, authorization: str | None = Header(None), edge_session: str | None = Cookie(None)):
        require_auth(authorization, edge_session)
        origin = request.headers.get('origin')
        if origin and origin not in (
            os.getenv('EDGE_UI_ORIGIN', 'http://127.0.0.1:5173'),
            f"{request.url.scheme}://{request.headers.get('host')}",
        ):
            raise HTTPException(403, 'origin_not_allowed')

    router = APIRouter(prefix='/api/v1/agent/runs', dependencies=[Depends(authorize)])

    @router.post('', status_code=202)
    async def submit(body: SubmitRun, key: Annotated[UUID, Header(alias='Idempotency-Key')]) -> RunSnapshot:
        return runs.submit(body.query, key)

    @router.get('/{run_id}')
    async def get(run_id: UUID) -> RunSnapshot:
        return runs.get(run_id).snapshot

    @router.delete('/{run_id}')
    async def cancel(run_id: UUID) -> RunSnapshot:
        return runs.cancel(run_id)

    return router
