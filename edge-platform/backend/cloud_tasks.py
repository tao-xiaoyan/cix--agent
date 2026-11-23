"""Outbound worker for cloud requests backed by the existing sleep-stage Agent."""
import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from shared.schemas.api import AnalysisTask, ErrorDetail
from shared.schemas.skill_tasks import EdgeClaim, EdgeCompletion, EdgeOutcome, Evidence, STAGE_SKILL

if __package__:
    from .sleep_analysis import agent, contracts, qwen, sleep_stage
else:
    from sleep_analysis import agent, contracts, qwen, sleep_stage

log = logging.getLogger(__name__)
TASKS = '/api/v1/analysis/tasks'
QWEN_URL = 'http://127.0.0.1:18080'


def failure(code: str, message: str) -> EdgeOutcome:
    return EdgeOutcome(error=ErrorDetail(code=code, message=message))


async def run_ppg(claim: EdgeClaim, assets: Path) -> EdgeOutcome:
    epoch = claim.request.epoch
    try:
        grant = sleep_stage.load_reference_epoch(assets)
    except (OSError, ValueError, KeyError, TypeError):
        return failure('epoch_assets_unavailable', 'Board epoch assets are unavailable')
    if epoch != grant.request:
        return failure('epoch_not_authorized', 'Epoch is outside this board’s configured grant')

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), trust_env=False) as http:
            response = await http.get(f'{QWEN_URL}/v1/models', timeout=5)
            response.raise_for_status()
            model = response.json()['data'][0]['id']
            if not isinstance(model, str) or 'qwen3' not in model.lower():
                raise ValueError('Expected a local Qwen3 model')
            client = qwen.ChatCompletionsClient(http, f'{QWEN_URL}/v1/chat/completions', model, max_tokens=384)
            prompt = (
                f'Determine the model-predicted sleep stage for dataset_id {epoch.dataset_id}, '
                f'subject_id {epoch.subject_id}, epoch_id {epoch.epoch_id} using sleep_stage_inference. '
                'Cite the actual result and retain the model quality limitations.'
                ' The generic scope is an orchestration anchor, separate from the epoch acquisition time.'
            )
            anchor = datetime.now(timezone.utc)
            scope = contracts.WindowRequest(dataset_id=epoch.dataset_id, session_id=f'subject_{epoch.subject_id}',
                                            window={'start': anchor, 'end': anchor + timedelta(seconds=30)})
            runner = agent.create_agent(qwen.QwenPlanner(client, prompt),
                                        stage_runtime=sleep_stage.SleepStageRuntime(assets / 'sleep_cnn.cix'),
                                        planner_timeout=120, run_timeout=480)
            run = await runner.run_authorized(target_window=scope, authorized_scope=scope,
                                             completion_goal='infer_sleep_stage', authorized_epochs=(grant,))
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return failure('qwen_unavailable', 'Local Qwen3 service could not execute this request')

    return outcome_from_run(run, grant)


def outcome_from_run(run: agent.RunResult, grant: sleep_stage.AuthorizedEpoch) -> EdgeOutcome:
    if run.termination != 'finished' or not run.finish or not run.finish.report:
        return failure('agent_incomplete', f'Agent did not finish with verified NPU evidence ({run.termination})')
    for finding in run.finish.report.findings:
        result = finding.result
        if finding.skill != STAGE_SKILL or result.status != 'succeeded':
            continue
        trace = next((entry for entry in run.trace
                      if entry.call.name == STAGE_SKILL and entry.result == result), None)
        if trace and result.data.get('status') == 'ok':
            output = sleep_stage.SleepStageOutput.model_validate(result.data)
            if output.source.get('input_sha256') != grant.input_sha256:
                break
            evidence = Evidence(skill=STAGE_SKILL, agent_run_id=run.run_id, call_id=result.call_id,
                executed_at=trace.finished_at, epoch=grant.request, backend=output.backend, model_id=output.model_id,
                input_sha256=output.source['input_sha256'], model_sha256=output.source['model_sha256'],
                predicted_stage=output.predicted_stage, limitations=output.limitations)
            return EdgeOutcome(result=result, evidence=evidence)
    return failure('evidence_unavailable', 'Agent report lacks matching PPG inference evidence')


class CloudTaskWorker:
    def __init__(self, store, device_id: str, url: str, token: str, assets: Path):
        endpoint = urlsplit(url)
        if (endpoint.scheme != 'https'
                and not (endpoint.scheme == 'http' and endpoint.hostname in ('127.0.0.1', 'localhost', '::1'))):
            raise ValueError('Cloud task worker requires HTTPS or a loopback development URL')
        self.store, self.device_id, self.assets = store, device_id, assets
        self.url = url.rstrip('/')
        self.headers = {'Authorization': f'Bearer {token}'}
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS edge_task_results (
                task_id TEXT PRIMARY KEY, completion TEXT NOT NULL, state TEXT NOT NULL)""")

    async def tick(self, http: httpx.AsyncClient):
        with self.store.connect() as db:
            pending = db.execute("SELECT task_id,completion FROM edge_task_results WHERE state='pending' ORDER BY rowid").fetchall()
        for item in pending:
            response = await http.post(f"{self.url}{TASKS}/{item['task_id']}/edge-completion",
                                       content=item['completion'], headers={**self.headers, 'Content-Type': 'application/json'})
            if response.status_code in (404, 409, 422):
                state = 'awaiting_claim' if response.status_code == 409 else 'rejected'
                log.warning('Cloud task completion %s for task %s', state, item['task_id'])
            else:
                response.raise_for_status()
                task = AnalysisTask.model_validate(response.json())
                if (str(task.task_id) != item['task_id'] or task.device_id != self.device_id
                        or task.status not in ('succeeded', 'failed')):
                    raise ValueError('Invalid completion acknowledgement')
                state = 'acknowledged'
            with self.store.connect() as db:
                db.execute('UPDATE edge_task_results SET state=? WHERE task_id=?', (state, item['task_id']))

        response = await http.post(f'{self.url}{TASKS}/edge-claim', headers=self.headers)
        response.raise_for_status()
        if response.json() is None:
            return
        claim = EdgeClaim.model_validate(response.json())
        if claim.request.device_id != self.device_id:
            raise ValueError('Claim for a different device')
        key = str(claim.task_id)
        with self.store.connect() as db:
            cached = db.execute('SELECT completion FROM edge_task_results WHERE task_id=?', (key,)).fetchone()
        if cached:
            outcome = EdgeCompletion.model_validate_json(cached['completion']).outcome
        else:
            try:
                outcome = await run_ppg(claim, self.assets)
            except (ValidationError, KeyError):
                outcome = failure('invalid_evidence', 'Agent returned invalid inference evidence')
        completion = EdgeCompletion(lease=claim.lease, outcome=outcome)
        with self.store.connect() as db:
            db.execute("""INSERT INTO edge_task_results VALUES (?,?,'pending')
                ON CONFLICT(task_id) DO UPDATE SET completion=excluded.completion, state='pending'""",
                       (key, completion.model_dump_json()))

    async def loop(self):
        attempt = 0
        async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
            while True:
                try:
                    await self.tick(http)
                    attempt = 0
                except (httpx.HTTPError, ValueError, sqlite3.Error) as exc:
                    attempt = min(6, attempt + 1)
                    log.warning('Cloud task sync will retry (%s)', type(exc).__name__)
                await asyncio.sleep(2 if not attempt else min(60, 2 ** attempt + random.random()))
