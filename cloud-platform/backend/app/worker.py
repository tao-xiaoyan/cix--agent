"""One in-process polling worker. No model, no medical inference."""
import threading
import logging
from uuid import uuid4
from sqlalchemy import func, select, update
from pydantic import TypeAdapter
from shared.schemas.events import Event
from .db import TaskRow, EventRow, AnalysisResult, now
from .security import digest

ADAPTER = TypeAdapter(Event)
MOCK_TASK = func.coalesce(TaskRow.request['analysis_type'].as_string(), 'multimodal_summary') == 'multimodal_summary'


def run_one(factory):
    with factory() as session:
        task = session.scalar(select(TaskRow).where(TaskRow.status == 'queued', MOCK_TASK).order_by(TaskRow.created_at).limit(1))
        if task is None:
            return False
        task_id = task.task_id
        claimed = session.execute(update(TaskRow).where(TaskRow.task_id == task_id, TaskRow.status == 'queued')
                                  .values(status='running', updated_at=now()))
        session.commit()
        if claimed.rowcount != 1:
            return True
    try:
        with factory.begin() as session:
            task = session.get(TaskRow, task_id)
            from shared.schemas.api import AnalysisRequest
            request = AnalysisRequest.model_validate(task.request)
            rows = session.scalars(select(EventRow).where(EventRow.device_id == task.device_id,
                EventRow.timestamp >= request.start, EventRow.timestamp < request.end,
                EventRow.event_type != 'cloud_analysis_result').order_by(EventRow.timestamp.desc(), EventRow.event_id.desc())).all()
            mock_count = sum(r.data['source'] == 'mock' for r in rows)
            event = ADAPTER.validate_python({'schema_version': '1.0.0', 'event_id': str(uuid4()),
                'device_id': task.device_id, 'timestamp': now(), 'source': 'mock',
                'event_type': 'cloud_analysis_result', 'model_name': 'mock-analysis-worker', 'model_version': '1',
                'payload': {'task_id': task_id, 'summary': 'MOCK：仅用于端云闭环测试，不是医学判断。',
                    'findings': [f'MOCK：区间内共 {len(rows)} 条事件，其中 {mock_count} 条来源为 mock。'],
                    'related_event_ids': list(dict.fromkeys(
                        [r.event_id for r in rows if r.event_id == task.idempotency_key]
                        + [r.event_id for r in rows]))[:100]}})
            data = event.model_dump(mode='json')
            session.add(EventRow(event_id=str(event.event_id), device_id=event.device_id,
                timestamp=event.timestamp, event_type=event.event_type, data=data, content_hash=digest(data)))
            session.flush()
            session.add(AnalysisResult(task_id=task_id, event_id=str(event.event_id), result=data))
            task.result_event_id = str(event.event_id)
            task.status = 'succeeded'
            task.updated_at = now()
    except Exception:
        with factory.begin() as session:
            task = session.get(TaskRow, task_id)
            task.status = 'failed'
            task.error = {'code': 'mock_analysis_failed', 'message': 'Mock worker failed; retry with a new key', 'details': {}}
            task.updated_at = now()
    return True


class MockWorker:
    def __init__(self, factory):
        self.factory = factory
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        # MVP is explicitly single API process / worker. Interrupted jobs become failed.
        with self.factory.begin() as session:
            session.execute(update(TaskRow).where(TaskRow.status == 'running', MOCK_TASK).values(status='failed', updated_at=now(),
                error={'code': 'worker_interrupted', 'message': 'Worker restarted during analysis', 'details': {}}))
        self.thread.start()

    def loop(self):
        while not self.stop_event.is_set():
            try:
                if run_one(self.factory):
                    continue
            except Exception:
                logging.getLogger(__name__).warning('Mock worker could not poll database; queued jobs remain pending')
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
