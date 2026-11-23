import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from shared.schemas.events import Event
from shared.schemas.api import (AnalysisTask, BatchRequest, BatchResponse,
    DeviceStatus, EventQuery, HealthResponse, Page)
from shared.schemas.skill_tasks import EdgeAnalysisRequest, EdgeOutcome, EdgeTaskDetail, EdgeTaskStatus, STAGE_SKILL, TaskRequest
from .config import Settings
from .db import AnalysisResult, Device, EventRow, TaskRow, database, now
from . import edge_tasks
from .security import canonical, check_device, decode_cursor, digest, encode_cursor, fail, identity, scope
from .worker import MockWorker

EVENT = TypeAdapter(Event)


def task_dto(row):
    return AnalysisTask.model_validate({k: getattr(row, k) for k in
        ('task_id', 'device_id', 'status', 'created_at', 'updated_at', 'result_event_id', 'error')})


def insert_device(session, device_id):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if session.bind.dialect.name == 'postgresql' else sqlite_insert
    session.execute(insert(Device).values(device_id=device_id, created_at=now()).on_conflict_do_nothing())


def create_app(settings=None):
    settings = settings or Settings.from_env()
    engine, factory = database(settings.database_url)
    worker = MockWorker(factory)

    @asynccontextmanager
    async def lifespan(app):
        if settings.worker_enabled:
            worker.start()
        yield
        if settings.worker_enabled:
            worker.stop()
        engine.dispose()

    app = FastAPI(title='Health Monitor Cloud', version='1.0.0', lifespan=lifespan)
    app.state.engine, app.state.factory = engine, factory
    app.include_router(edge_tasks.routes(factory, task_dto, insert_device))
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.origins),
        allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key', 'X-Request-ID'],
        expose_headers=['X-Request-ID'])

    def error(request, status, detail):
        return JSONResponse({'request_id': request.state.request_id, 'error': detail}, status_code=status,
                            headers={'X-Request-ID': request.state.request_id})

    @app.middleware('http')
    async def guard(request, call_next):
        incoming = request.headers.get('X-Request-ID', '')
        request.state.request_id = incoming if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', incoming) else str(uuid4())
        try:
            if request.url.path.startswith('/api/') and request.method != 'OPTIONS':
                request.state.user = identity(settings, request.headers.get('Authorization'))
            if request.method == 'POST':
                size = 0
                chunks = []
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 1024 * 1024:
                        fail(413, 'payload_too_large', 'Request limit is 1 MiB')
                    chunks.append(chunk)
                request._body = b''.join(chunks)
            response = await call_next(request)
        except HTTPException as exc:
            response = error(request, exc.status_code, exc.detail)
        except SQLAlchemyError:
            response = error(request, 503, {'code': 'storage_unavailable', 'message': 'Database unavailable', 'details': {}})
        response.headers['X-Request-ID'] = request.state.request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {'code': 'http_error', 'message': str(exc.detail), 'details': {}}
        return error(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        invalid_json = any(e['type'] == 'json_invalid' for e in exc.errors())
        return error(request, 400 if invalid_json else 422, {'code': 'invalid_json' if invalid_json else 'validation_error',
            'message': 'Invalid JSON' if invalid_json else 'Invalid request fields',
            'details': {'fields': [{'location': list(e['loc']), 'type': e['type']} for e in exc.errors()]}})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return error(request, 500, {'code': 'internal_error', 'message': 'Unexpected server error', 'details': {}})

    @app.get('/health', response_model=HealthResponse)
    def health():
        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
            status = 'ok'
        except SQLAlchemyError:
            status = 'degraded'
        return HealthResponse(status=status, service='cloud')

    @app.post('/api/v1/events/batch', response_model=BatchResponse)
    def batch(body: BatchRequest, request: Request):
        user = request.state.user
        results = []
        for index, raw in enumerate(body.events):
            event_id = None
            try:
                event_id = str(UUID(str(raw.get('event_id'))))
            except (ValueError, TypeError):
                pass
            result = {'index': index, 'event_id': event_id, 'status': 'rejected', 'error': None}
            code = None
            if raw.get('device_id') not in user['devices']:
                code = 'device_mismatch'
            elif raw.get('schema_version', '1.0.0') != '1.0.0':
                code = 'unsupported_schema'
            else:
                try:
                    event = EVENT.validate_python(raw)
                    data = event.model_dump(mode='json')
                except ValidationError:
                    code = 'validation_error'
                else:
                    try:
                        with factory.begin() as session:
                            existing = session.get(EventRow, event_id)
                            if existing:
                                if existing.device_id != event.device_id or existing.content_hash != digest(data):
                                    code = 'event_id_conflict'
                                else:
                                    result['status'] = 'duplicate'
                            else:
                                insert_device(session, event.device_id)
                                session.add(EventRow(event_id=event_id, device_id=event.device_id, timestamp=event.timestamp,
                                    event_type=event.event_type, data=data, content_hash=digest(data)))
                                session.flush()
                                result['status'] = 'accepted'
                            if code is None:
                                device = session.get(Device, event.device_id)
                                device.last_seen = now()
                                device.last_sync = now()
                    except IntegrityError:
                        # Another uploader may have committed this UUID concurrently.
                        with factory() as session:
                            existing = session.get(EventRow, event_id)
                            if existing and existing.device_id == event.device_id and existing.content_hash == digest(data):
                                result['status'] = 'duplicate'
                            else:
                                code = 'event_id_conflict'
                    except SQLAlchemyError:
                        code = 'storage_unavailable'
                        result['status'] = 'retryable'
            if code:
                if result['status'] != 'retryable':
                    result['status'] = 'rejected'
                result['error'] = {'code': code, 'message': code.replace('_', ' '), 'details': {}}
            results.append(result)
        return BatchResponse(request_id=request.state.request_id, results=results)

    def filtered(query, user):
        if query.device_id:
            check_device(user, query.device_id)
        stmt = select(EventRow).where(EventRow.device_id.in_(user['devices']))
        if query.device_id:
            stmt = stmt.where(EventRow.device_id == query.device_id)
        if query.event_type:
            stmt = stmt.where(EventRow.event_type == query.event_type)
        if query.start:
            stmt = stmt.where(EventRow.timestamp >= query.start)
        if query.end:
            stmt = stmt.where(EventRow.timestamp < query.end)
        return stmt

    @app.get('/api/v1/events', response_model=Page[Event])
    def events(request: Request, query: Annotated[EventQuery, Query()]):
        user = request.state.user
        stmt = filtered(query, user)
        context = {'kind': 'events', 'scope': scope(user), 'filters': query.model_dump(mode='json', exclude={'cursor', 'limit'})}
        snapshot = now().isoformat()
        expires = None
        if query.cursor:
            cursor = decode_cursor(settings, query.cursor, context)
            snapshot, expires = cursor['snapshot'], cursor['expires']
            stamp, eid = cursor['position']
            stamp = datetime.fromisoformat(stamp)
            stmt = stmt.where(or_(EventRow.timestamp < stamp, and_(EventRow.timestamp == stamp, EventRow.event_id < eid)))
        stmt = stmt.where(EventRow.received_at <= datetime.fromisoformat(snapshot))
        with factory() as session:
            rows = session.scalars(stmt.order_by(EventRow.timestamp.desc(), EventRow.event_id.desc()).limit(query.limit + 1)).all()
            more = len(rows) > query.limit
            rows = rows[:query.limit]
            token = encode_cursor(settings, context, [rows[-1].timestamp.isoformat(), rows[-1].event_id], snapshot, expires) if more else None
            return Page(items=[EVENT.validate_python(r.data) for r in rows], next_cursor=token)

    @app.get('/api/v1/events/{event_id}', response_model=Event)
    def event_detail(event_id: UUID, request: Request):
        with factory() as session:
            row = session.get(EventRow, str(event_id))
            if row is None or row.device_id not in request.state.user['devices']:
                fail(404, 'not_found', 'Event not found')
            return EVENT.validate_python(row.data)

    @app.post('/api/v1/analysis/tasks', response_model=AnalysisTask, status_code=202)
    def create_task(body: TaskRequest, request: Request,
                    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]):
        user = request.state.user
        check_device(user, body.device_id)
        if isinstance(body, EdgeAnalysisRequest) and user['role'] != 'operator':
            fail(403, 'operator_required', 'An operator credential is required to submit inference')
        data = body.model_dump(mode='json')
        def existing_task(session):
            return session.scalar(select(TaskRow).where(TaskRow.identity == user['subject'], TaskRow.idempotency_key == idempotency_key))
        def checked(row):
            if row.request_hash != digest(data):
                fail(409, 'idempotency_conflict', 'Key already used for a different request')
            return task_dto(row)
        try:
            with factory.begin() as session:
                existing = existing_task(session)
                if existing:
                    return checked(existing)
                insert_device(session, body.device_id)
                row = TaskRow(task_id=str(uuid4()), device_id=body.device_id, identity=user['subject'],
                    idempotency_key=idempotency_key, request_hash=digest(data), request=data, status='queued')
                session.add(row)
                session.flush()
                result = task_dto(row)
            return result
        except IntegrityError:
            with factory() as session:
                existing = existing_task(session)
                if existing:
                    return checked(existing)
            raise

    @app.get('/api/v1/analysis/tasks/{task_id}', response_model=EdgeTaskDetail | AnalysisTask)
    def task_detail(task_id: UUID, request: Request):
        with factory() as session:
            row = session.get(TaskRow, str(task_id))
            if row is None or row.device_id not in request.state.user['devices']:
                fail(404, 'not_found', 'Task not found')
            if row.request.get('analysis_type') == STAGE_SKILL:
                result = session.get(AnalysisResult, row.task_id)
                outcome = result.result if result else ({'error': row.error} if row.error else None)
                return EdgeTaskDetail(**task_dto(row).model_dump(),
                                      outcome=EdgeOutcome.model_validate(outcome) if outcome else None)
            return task_dto(row)

    @app.get('/api/v1/analysis/tasks', response_model=Page[EdgeTaskStatus | AnalysisTask])
    def tasks(request: Request, device_id: str | None = None, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        user = request.state.user
        if device_id:
            check_device(user, device_id)
        context = {'kind': 'tasks', 'scope': scope(user), 'device_id': device_id}
        stmt = select(TaskRow).where(TaskRow.device_id.in_(user['devices']))
        if device_id:
            stmt = stmt.where(TaskRow.device_id == device_id)
        snapshot = now().isoformat()
        expires = None
        if cursor:
            value = decode_cursor(settings, cursor, context)
            snapshot, expires = value['snapshot'], value['expires']
            stmt = stmt.where(TaskRow.task_id > value['position'])
        with factory() as session:
            rows = session.scalars(stmt.where(TaskRow.created_at <= datetime.fromisoformat(snapshot)).order_by(TaskRow.task_id).limit(limit + 1)).all()
            more, rows = len(rows) > limit, rows[:limit]
            token = encode_cursor(settings, context, rows[-1].task_id, snapshot, expires) if more else None
            items = []
            for row in rows:
                entry = task_dto(row)
                if row.request.get('analysis_type') == STAGE_SKILL:
                    entry = EdgeTaskStatus(**entry.model_dump())
                items.append(entry)
            return Page(items=items, next_cursor=token)

    @app.get('/api/v1/devices', response_model=Page[DeviceStatus])
    def devices(request: Request, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        user = request.state.user
        context = {'kind': 'devices', 'scope': scope(user)}
        stmt = select(Device).where(Device.device_id.in_(user['devices']))
        snapshot = now().isoformat()
        expires = None
        if cursor:
            value = decode_cursor(settings, cursor, context)
            snapshot, expires = value['snapshot'], value['expires']
            stmt = stmt.where(Device.device_id > value['position'])
        with factory() as session:
            rows = session.scalars(stmt.where(Device.created_at <= datetime.fromisoformat(snapshot)).order_by(Device.device_id).limit(limit + 1)).all()
            more, rows = len(rows) > limit, rows[:limit]
            items = [DeviceStatus(device_id=r.device_id, last_seen=r.last_seen,
                connectivity='online' if r.last_seen and r.last_seen > now() - timedelta(seconds=90) else 'offline') for r in rows]
            token = encode_cursor(settings, context, rows[-1].device_id, snapshot, expires) if more else None
            return Page(items=items, next_cursor=token)

    @app.get('/api/v1/dashboard')
    def dashboard(request: Request, query: Annotated[EventQuery, Query()]):
        user = request.state.user
        if user['role'] != 'operator':
            fail(403, 'forbidden', 'Operator access required')
        stmt = filtered(query, user)
        subset = stmt.subquery()
        with factory() as session:
            counts = dict(session.execute(select(subset.c.event_type, func.count()).group_by(subset.c.event_type)).all())
            latest = session.scalar(select(func.max(subset.c.received_at)))
            devices = session.scalars(select(Device).where(Device.device_id.in_(user['devices']))).all()
            stats = []
            for device in devices:
                rows = session.execute(select(func.count(), func.max(EventRow.received_at)).where(EventRow.device_id == device.device_id)).one()
                stats.append({'device_id': device.device_id, 'event_count': rows[0], 'last_sync': rows[1]})
            return {'event_count': sum(counts.values()), 'anomaly_count': counts.get('anomaly_event', 0),
                'device_count': len(devices), 'latest_sync': latest, 'event_types': counts, 'devices': stats}

    return app
