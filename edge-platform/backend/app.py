"""Local edge MVP. Run from repository root with PYTHONPATH set to that root."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import sqlite3
import time
from contextlib import asynccontextmanager
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from shared.interfaces import ModelInput, ModelOutput
from shared.schemas.api import AnalysisRequest, AnalysisTask, BatchResponse, DeviceStatus, EventQuery, HealthResponse, Page, RealtimeMessage
from shared.schemas.events import Event
# Support the existing app-dir entry point and package-based board deployment.
if __package__:
    from . import agent_workbench, cloud_tasks, depth_preview
else:
    import agent_workbench
    import cloud_tasks
    import depth_preview

EVENT_ADAPTER = TypeAdapter(Event)
ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads((ROOT / 'shared/examples/events.json').read_text(encoding='utf-8'))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def error(status: int, code: str, message: str) -> JSONResponse:
    request_id = str(uuid4())
    return JSONResponse(status_code=status, headers={'X-Request-ID': request_id},
                        content={'request_id': request_id, 'error': {'code': code, 'message': message, 'details': {}}})


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                  event_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                  event_type TEXT NOT NULL, received_at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_order ON events(timestamp DESC,event_id DESC);
                CREATE TABLE IF NOT EXISTS outbox (
                  event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                  state TEXT NOT NULL, retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT);
                CREATE TABLE IF NOT EXISTS tasks (
                  task_key TEXT PRIMARY KEY, request_json TEXT NOT NULL, status TEXT NOT NULL,
                  cloud_task_id TEXT, last_error TEXT);
            ''')
            db.execute("UPDATE outbox SET state='pending' WHERE state='in_flight'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def add(self, event: Event, upload: bool = True) -> bool:
        body = event.model_dump(mode='json')
        serialized = json.dumps(body, sort_keys=True, separators=(',', ':'))
        with self.connect() as db:
            old = db.execute('SELECT body FROM events WHERE event_id=?', (str(event.event_id),)).fetchone()
            if old:
                if old['body'] != serialized:
                    raise ValueError('event_id_conflict')
                return False
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                       (str(event.event_id), event.device_id, event.timestamp.isoformat(),
                        event.event_type, utcnow().isoformat(), serialized))
            if upload and event.source != 'cloud' and event.event_type != 'cloud_analysis_result':
                db.execute("INSERT INTO outbox(event_id,state) VALUES (?,'pending')", (str(event.event_id),))
        return True

    def recent(self, query: EventQuery, scope: str) -> Page[Event]:
        filters = ['device_id=?']
        args: list[Any] = [scope]
        snapshot = utcnow().isoformat()
        last_time = last_id = None
        if query.cursor:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(query.cursor + '==='))
                expected = self._fingerprint(query, scope)
                if cursor['filter'] != expected or time.time() - cursor['issued'] > 3600:
                    raise ValueError('cursor_expired')
                snapshot, last_time, last_id = cursor['snapshot'], cursor['time'], cursor['id']
            except Exception as exc:
                raise ValueError('invalid_cursor') from exc
        filters.append('received_at<=?')
        args.append(snapshot)
        if query.event_type:
            filters.append('event_type=?')
            args.append(query.event_type)
        if query.start:
            filters.append('timestamp>=?')
            args.append(query.start.isoformat())
        if query.end:
            filters.append('timestamp<?')
            args.append(query.end.isoformat())
        if last_time:
            filters.append('(timestamp<? OR (timestamp=? AND event_id<?))')
            args.extend([last_time, last_time, last_id])
        sql = 'SELECT body,timestamp,event_id FROM events WHERE ' + ' AND '.join(filters) + ' ORDER BY timestamp DESC,event_id DESC LIMIT ?'
        with self.connect() as db:
            rows = db.execute(sql, (*args, query.limit + 1)).fetchall()
        items = [EVENT_ADAPTER.validate_json(row['body']) for row in rows[:query.limit]]
        next_cursor = None
        if len(rows) > query.limit:
            last = rows[query.limit - 1]
            cursor = {'filter': self._fingerprint(query, scope), 'snapshot': snapshot,
                      'time': last['timestamp'], 'id': last['event_id'], 'issued': time.time()}
            next_cursor = base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode().rstrip('=')
        return Page[Event](items=items, next_cursor=next_cursor)

    @staticmethod
    def _fingerprint(query: EventQuery, scope: str) -> str:
        data = query.model_dump(mode='json', exclude={'cursor', 'limit'})
        return hashlib.sha256(json.dumps([scope, data], sort_keys=True).encode()).hexdigest()

    def pending_count(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM outbox WHERE state IN ('pending','in_flight')").fetchone()[0]

    def pending_batch(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT e.body FROM outbox o JOIN events e USING(event_id) WHERE o.state='pending' AND o.retry_at<=? ORDER BY e.timestamp LIMIT 100", (time.time(),)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def mark_flight(self, ids: list[str]):
        with self.connect() as db:
            db.executemany("UPDATE outbox SET state='in_flight' WHERE event_id=?", [(event_id,) for event_id in ids])

    def mark_result(self, event_id: str, state: str, message: str | None = None, delay: float = 0):
        with self.connect() as db:
            db.execute('UPDATE outbox SET state=?,retry_at=?,attempts=attempts+1,last_error=? WHERE event_id=?',
                       (state, time.time() + delay, message, event_id))

    def queue_task(self, request: AnalysisRequest, key: str):
        body = request.model_dump_json()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO tasks VALUES (?,?,'pending',NULL,NULL)", (key, body))

    def pending_tasks(self):
        with self.connect() as db:
            return db.execute("SELECT task_key,request_json FROM tasks WHERE status='pending'").fetchall()

    def active_tasks(self):
        with self.connect() as db:
            return db.execute("SELECT * FROM tasks WHERE status IN ('queued','running')").fetchall()

    def list_tasks(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY rowid DESC LIMIT 100')]

    def task_result(self, key: str, status: str, cloud_id: str | None, message: str | None):
        with self.connect() as db:
            db.execute('UPDATE tasks SET status=?,cloud_task_id=?,last_error=? WHERE task_key=?',
                       (status, cloud_id, message, key))


class AdapterRegistry:
    def __init__(self):
        self.adapters: dict[str, Any] = {}

    def register(self, modality: str, adapter: Any):
        if modality not in {'physiological', 'vision', 'audio', 'llm'}:
            raise ValueError('unknown modality')
        self.adapters[modality] = adapter

    async def infer(self, request: ModelInput) -> ModelOutput:
        adapter = self.adapters.get(request.modality)
        if adapter is None:
            return ModelOutput(request_id=request.request_id, status='unavailable', message='Adapter not registered')
        output = await adapter.infer(request)
        if output.request_id != request.request_id:
            raise ValueError('adapter request_id mismatch')
        for event in output.events:
            if event.device_id != request.device_id:
                raise ValueError('adapter device_id mismatch')
        return output


class MockGenerator:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.tick = 0

    def next_events(self) -> list[Event]:
        self.tick += 1
        now = utcnow()
        kinds = ['physiological_window', 'pose_estimation', 'sleep_posture', 'audio_event', 'device_metrics']
        if self.tick % 5 == 0:
            kinds.append('anomaly_event')
        result = []
        for kind in kinds:
            template = next(item for item in EXAMPLES if item['event_type'] == kind)
            raw = json.loads(json.dumps(template))
            raw.update(event_id=str(uuid4()), device_id=self.device_id, timestamp=now.isoformat(), source='mock')
            payload = raw['payload']
            if kind == 'physiological_window':
                payload.update(window_start=(now - timedelta(seconds=1)).isoformat(), window_end=now.isoformat(), connection='connected')
                payload['channels'] = [
                    {'name': name, 'unit': None, 'sampling_rate_hz': None,
                     'samples': [round(random.uniform(0.2, 0.8), 3) for _ in range(20)], 'quality': 'valid'}
                    for name in ('mock-PPG', 'mock-GSR', 'mock-IMU')]
            elif kind == 'pose_estimation':
                payload.update(status='available', persons=[{'track_id': 'mock-person-1', 'keypoints': [
                    {'name': 'head', 'x': 0.5, 'y': 0.3, 'confidence': 0.92},
                    {'name': 'center', 'x': 0.5, 'y': 0.6, 'confidence': 0.88}]}])
            elif kind == 'sleep_posture':
                payload.update(label=['仰卧', '侧卧', '俯卧'][self.tick % 3], status='available', reason=None)
                raw['confidence'] = 0.8
            elif kind == 'audio_event':
                payload.update(label='模拟环境声', status='available', duration_ms=1000, reason=None)
            elif kind == 'device_metrics':
                payload.update(cpu_percent=None, memory_used_bytes=None, disk_free_bytes=None,
                               pending_upload_count=0, sensors={'camera': 'unknown', 'serial': 'unknown'})
            elif kind == 'anomaly_event':
                payload.update(code='mock_alert', severity='warning', description='模拟异常事件，仅用于测试', related_event_ids=[])
            result.append(EVENT_ADAPTER.validate_python(raw))
        return result


class HttpCloudClient:
    def __init__(self, url: str, token: str, timeout: float = 5):
        self.url, self.token, self.timeout = url.rstrip('/'), token, timeout

    async def upload(self, events: list[dict]) -> BatchResponse:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.url + '/api/v1/events/batch', json={'events': events},
                                         headers={'Authorization': 'Bearer ' + self.token})
            response.raise_for_status()
            return BatchResponse.model_validate(response.json())

    async def create_task(self, request: AnalysisRequest, idempotency_key: str) -> AnalysisTask:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.url + '/api/v1/analysis/tasks', json=request.model_dump(mode='json'),
                                         headers={'Authorization': 'Bearer ' + self.token, 'Idempotency-Key': idempotency_key})
            response.raise_for_status()
            return AnalysisTask.model_validate(response.json())

    async def get_task(self, task_id: UUID) -> AnalysisTask:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.url + f'/api/v1/analysis/tasks/{task_id}',
                                        headers={'Authorization': 'Bearer ' + self.token})
            response.raise_for_status()
            return AnalysisTask.model_validate(response.json())


    async def get_event(self, event_id: UUID) -> Event:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.url + f'/api/v1/events/{event_id}',
                                        headers={'Authorization': 'Bearer ' + self.token})
            response.raise_for_status()
            return EVENT_ADAPTER.validate_python(response.json())


class Runtime:
    def __init__(self, store: Store, device_id: str, client: HttpCloudClient | None, mock: bool):
        self.store, self.device_id, self.client, self.mock = store, device_id, client, mock
        self.registry = AdapterRegistry()
        self.generator = MockGenerator(device_id)
        self.listeners: set[asyncio.Queue] = set()
        self.cloud_state = 'disabled' if client is None else 'offline'
        self.last_seen: datetime | None = None
        self.running = True

    async def record(self, event: Event, dispatch: bool = True):
        if self.store.add(event):
            self.last_seen = event.timestamp
            for listener in tuple(self.listeners):
                try:
                    listener.put_nowait(event)
                except asyncio.QueueFull:
                    listener.get_nowait()
                    listener.put_nowait(None)
            if dispatch and event.event_type == 'anomaly_event':
                await self.schedule(event)

    async def schedule(self, anomaly: Event):
        now = utcnow()
        decision = EVENT_ADAPTER.validate_python({
            'event_id': str(uuid4()), 'device_id': self.device_id, 'timestamp': now,
            'event_type': 'agent_decision', 'source': 'mock' if anomaly.source == 'mock' else 'rule',
            'payload': {'decision': 'request_history', 'rationale': f'Rule: {anomaly.payload.code}; long-term history required',
                        'actions': ['record_local', 'queue_cloud_analysis'],
                        'related_event_ids': [str(anomaly.event_id)], 'execution_status': 'executed'}})
        await self.record(decision, dispatch=False)
        request = AnalysisRequest(device_id=self.device_id, start=now - timedelta(hours=24), end=now + timedelta(microseconds=1))
        self.store.queue_task(request, str(anomaly.event_id))

    async def generate(self):
        while self.running:
            for event in self.generator.next_events():
                await self.record(event)
            await asyncio.sleep(2)

    async def sync(self):
        attempt = 0
        while self.running:
            if self.client is None:
                await asyncio.sleep(2)
                continue
            batch = self.store.pending_batch()
            try:
                if batch:
                    ids = [item['event_id'] for item in batch]
                    self.store.mark_flight(ids)
                    response = await self.client.upload(batch)
                    results = {item.index: item for item in response.results}
                    for index, event_id in enumerate(ids):
                        result = results.get(index)
                        if result and result.event_id == UUID(event_id) and result.status in ('accepted', 'duplicate'):
                            self.store.mark_result(event_id, 'acknowledged')
                        elif result and result.status == 'rejected':
                            self.store.mark_result(event_id, 'quarantined', str(result.error))
                        else:
                            self.store.mark_result(event_id, 'pending', 'retryable or incomplete response', min(60, 2 ** attempt))
                # Upload snapshot may predate an anomaly queued during await upload.
                # Never start analysis until its source events are acknowledged.
                for task in (self.store.pending_tasks() if self.store.pending_count() == 0 else []):
                    result = await self.client.create_task(AnalysisRequest.model_validate_json(task['request_json']), task['task_key'])
                    # Replayed creation can already be terminal after a lost response.
                    # Fetch/persist a successful result before marking local success.
                    self.store.task_result(task['task_key'], 'queued' if result.status == 'succeeded' else result.status,
                                           str(result.task_id), result.error.model_dump_json() if result.error else None)
                for task in self.store.active_tasks():
                    result = await self.client.get_task(UUID(task['cloud_task_id']))
                    if result.device_id != self.device_id:
                        raise ValueError('task device mismatch')
                    if result.status == 'succeeded':
                        if not result.result_event_id:
                            raise ValueError('missing result event')
                        event = await self.client.get_event(result.result_event_id)
                        if (event.device_id != self.device_id or event.event_type != 'cloud_analysis_result'
                                or event.payload.task_id != result.task_id):
                            raise ValueError('result association mismatch')
                        await self.record(event, dispatch=False)
                    self.store.task_result(task['task_key'], result.status, str(result.task_id),
                                           result.error.model_dump_json() if result.error else None)
                self.cloud_state = 'online'
                attempt = 0
            except (httpx.HTTPError, ValidationError, ValueError) as exc:
                self.cloud_state = 'offline'
                attempt = min(attempt + 1, 6)
                for event in batch:
                    self.store.mark_result(event['event_id'], 'pending', type(exc).__name__, min(60, 2 ** attempt))
            await asyncio.sleep(2 if attempt == 0 else min(60, 2 ** attempt) + random.random())


def create_app(db_path: Path | None = None, mock: bool | None = None, cloud_client: HttpCloudClient | None = None) -> FastAPI:
    device_id = os.getenv('EDGE_DEVICE_ID', 'edge-mock-001')
    data_path = db_path or Path(os.getenv('EDGE_DB_PATH', str(Path(__file__).resolve().parents[1] / 'data/events.sqlite3')))
    url, token = os.getenv('EDGE_CLOUD_URL'), os.getenv('EDGE_CLOUD_TOKEN')
    client = cloud_client or (HttpCloudClient(url, token) if url and token else None)
    runtime = Runtime(Store(data_path), device_id, client, mock if mock is not None else os.getenv('EDGE_MOCK', '1') == '1')

    operator_token = os.getenv('EDGE_OPERATOR_TOKEN', 'local-demo-token')
    runs = agent_workbench.WorkbenchRuns(agent_workbench.configured_runner(operator_token))
    assets = os.getenv('EDGE_SLEEP_STAGE_ASSETS')
    cloud_worker = (cloud_tasks.CloudTaskWorker(runtime.store, device_id, url, token, Path(assets))
                    if assets and url and token and not runtime.mock else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(runtime.sync())]
        if cloud_worker:
            tasks.append(asyncio.create_task(cloud_worker.loop()))
        if runtime.mock:
            tasks.append(asyncio.create_task(runtime.generate()))
        try:
            yield
        finally:
            await runs.close()
            runtime.running = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title='Health Monitor Edge', version='1.0.0', lifespan=lifespan)
    app.state.runtime = runtime
    sessions: dict[str, float] = {}

    @app.middleware('http')
    async def request_id(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault('X-Request-ID', str(uuid4()))
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error(422, 'validation_error', 'Invalid request')

    def authorized(token: str | None, session: str | None) -> bool:
        return bool((token and hmac.compare_digest(token, operator_token)) or
                    (session and sessions.get(session, 0) > time.time()))

    def require_auth(authorization: str | None, session: str | None):
        token = authorization.removeprefix('Bearer ') if authorization else None
        if not authorized(token, session):
            raise HTTPException(status_code=401, detail='unauthorized')

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error(exc.status_code, 'unauthorized' if exc.status_code == 401 else 'http_error', str(exc.detail))

    app.include_router(agent_workbench.routes(runs, require_auth))
    app.include_router(depth_preview.routes(require_auth, error))

    @app.get('/health', response_model=HealthResponse)
    def health():
        return HealthResponse(status='ok', service='edge')

    @app.post('/api/v1/session')
    def session(response: Response, authorization: str | None = Header(None)):
        token = authorization.removeprefix('Bearer ') if authorization else None
        if not token or not hmac.compare_digest(token, operator_token):
            raise HTTPException(401, 'unauthorized')
        value = str(uuid4())
        sessions[value] = time.time() + 3600
        response.set_cookie('edge_session', value, httponly=True, samesite='strict', secure=False, max_age=3600)
        return {'expires_at': (utcnow() + timedelta(hours=1)).isoformat()}

    @app.get('/api/v1/device/status', response_model=DeviceStatus)
    def status(authorization: str | None = Header(None), edge_session: str | None = Cookie(None)):
        require_auth(authorization, edge_session)
        return DeviceStatus(device_id=device_id, last_seen=runtime.last_seen,
                            connectivity='online' if runtime.last_seen else 'unknown',
                            pending_upload_count=runtime.store.pending_count(),
                            sensors={'camera': 'unknown', 'serial': 'unknown', 'cloud': 'connected' if runtime.cloud_state == 'online' else 'unknown' if runtime.cloud_state == 'disabled' else 'disconnected'})

    @app.get('/api/v1/events/recent', response_model=Page[Event])
    def recent(request: Request, authorization: str | None = Header(None)):
        require_auth(authorization, request.cookies.get('edge_session'))
        try:
            query = EventQuery.model_validate(dict(request.query_params))
            if query.device_id and query.device_id != device_id:
                return Page[Event](items=[])
            return runtime.store.recent(query, device_id)
        except ValidationError:
            return error(422, 'validation_error', 'Invalid event query')
        except ValueError:
            return error(400, 'invalid_cursor', 'Invalid or expired cursor')

    @app.get('/api/v1/analysis/tasks/local')
    def local_tasks(request: Request, authorization: str | None = Header(None)):
        require_auth(authorization, request.cookies.get('edge_session'))
        return {'items': runtime.store.list_tasks()}

    @app.websocket('/ws/realtime')
    async def realtime(ws: WebSocket):
        origin = ws.headers.get('origin')
        allowed_origin = os.getenv('EDGE_UI_ORIGIN', 'http://127.0.0.1:5173')
        if origin and origin not in (allowed_origin, f'{ws.url.scheme.replace("ws", "http")}://{ws.headers.get("host")}'):
            await ws.close(code=1008)
            return
        header = ws.headers.get('authorization')
        token = header.removeprefix('Bearer ') if header else None
        if not authorized(token, ws.cookies.get('edge_session')):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        runtime.listeners.add(queue)
        sequence = 0
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    event = 'heartbeat'
                if event is None:
                    await ws.send_json(RealtimeMessage(kind='gap', timestamp=utcnow(), sequence=sequence, reason='Queue overflow').model_dump(mode='json'))
                    await ws.close(code=1013)
                    break
                message = RealtimeMessage(kind='heartbeat' if event == 'heartbeat' else 'event',
                                          timestamp=utcnow(), sequence=sequence,
                                          event=None if event == 'heartbeat' else event)
                await ws.send_json(message.model_dump(mode='json'))
                sequence += 1
        except WebSocketDisconnect:
            pass
        finally:
            runtime.listeners.discard(queue)

    return app


app = create_app()
