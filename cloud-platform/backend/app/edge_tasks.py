"""Device pull, fenced completion, and atomic result persistence."""
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from sqlalchemy import or_, select, update

from shared.schemas.api import AnalysisTask
from shared.schemas.events import CloudAnalysisEvent
from shared.schemas.skill_tasks import EdgeAnalysisRequest, EdgeClaim, EdgeCompletion, STAGE_SKILL

from .db import AnalysisResult, Device, EventRow, TaskRow, now
from .security import digest, fail

LEASE_DURATION = timedelta(minutes=10)
TASK_TYPE = TaskRow.request['analysis_type'].as_string()


def device_identity(request):
    user = request.state.user
    if user['role'] != 'device' or user['devices'] != [user['subject']]:
        fail(403, 'device_required', 'A device credential is required')
    return user['subject']


def routes(factory, task_dto, insert_device):
    router = APIRouter(prefix='/api/v1/analysis/tasks')

    @router.post('/edge-claim', response_model=EdgeClaim | None)
    def claim(request: Request):
        device_id = device_identity(request)
        stamp = now()
        eligible = or_(TaskRow.status == 'queued',
                       (TaskRow.status == 'running') & (TaskRow.updated_at <= stamp - LEASE_DURATION))
        with factory.begin() as session:
            device = session.get(Device, device_id)
            if device is None:
                insert_device(session, device_id)
                device = session.get(Device, device_id)
                device.last_seen = stamp
                return None
            row = session.scalar(select(TaskRow)
                .where(TaskRow.device_id == device_id, TASK_TYPE == STAGE_SKILL, eligible)
                .order_by(TaskRow.created_at, TaskRow.task_id).limit(1))
            if row is None:
                if not device.last_seen or device.last_seen < stamp - timedelta(seconds=30):
                    device.last_seen = stamp
                return None
            claimed = session.execute(update(TaskRow)
                .where(TaskRow.task_id == row.task_id, eligible)
                .values(status='running', updated_at=stamp))
            if claimed.rowcount != 1:
                return None
            device.last_seen = stamp
            return EdgeClaim(task_id=row.task_id, lease=stamp, request=EdgeAnalysisRequest.model_validate(row.request))

    @router.post('/{task_id}/edge-completion', response_model=AnalysisTask)
    def complete(task_id: UUID, body: EdgeCompletion, request: Request):
        device_id = device_identity(request)
        key = str(task_id)
        outcome = body.outcome
        evidence = outcome.evidence
        data = outcome.model_dump(mode='json')
        with factory.begin() as session:
            row = session.get(TaskRow, key)
            if row is None or row.device_id != device_id or row.request.get('analysis_type') != STAGE_SKILL:
                fail(404, 'not_found', 'Task not found')
            if evidence and evidence.epoch != EdgeAnalysisRequest.model_validate(row.request).epoch:
                fail(422, 'evidence_mismatch', 'Evidence epoch differs from the requested epoch')

            # Acquire the write lock before persisting any result. UPDATE rechecks the
            # lease after another transaction completes or reclaims the task.
            written = session.execute(update(TaskRow)
                .where(TaskRow.task_id == key, TaskRow.status == 'running', TaskRow.updated_at == body.lease)
                .values(status='succeeded' if evidence else 'failed', updated_at=now(), error=data['error'])
                .execution_options(synchronize_session=False))
            session.refresh(row)
            if written.rowcount != 1:
                result = session.get(AnalysisResult, key)
                if ((row.status == 'succeeded' and result and result.result == data)
                        or (row.status == 'failed' and outcome.error and row.error == data['error'])):
                    return task_dto(row)
                fail(409, 'lease_conflict', 'Claim is no longer current or a different result was recorded')

            device = session.get(Device, device_id)
            device.last_seen = device.last_sync = row.updated_at
            if evidence:
                event = CloudAnalysisEvent(event_id=uuid4(), device_id=device_id, timestamp=evidence.executed_at,
                    event_type='cloud_analysis_result', source='model', model_name=evidence.model_id,
                    payload={'task_id': task_id, 'summary': f'PPG 睡眠分期预测 · {evidence.predicted_stage}',
                             'findings': [f'{evidence.epoch.dataset_id} / subject {evidence.epoch.subject_id} / epoch {evidence.epoch.epoch_id}',
                                          evidence.backend, *evidence.limitations]})
                event_data = event.model_dump(mode='json')
                event_id = str(event.event_id)
                session.add(EventRow(event_id=event_id, device_id=device_id, timestamp=event.timestamp,
                    event_type=event.event_type, data=event_data, content_hash=digest(event_data)))
                session.flush()
                session.add(AnalysisResult(task_id=key, event_id=event_id, result=data))
                row.result_event_id = event_id
            return task_dto(row)

    return router
