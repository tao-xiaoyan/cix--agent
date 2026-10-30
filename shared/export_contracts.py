"""Run from repo root: python -m shared.export_contracts. No running service."""
import json
from pathlib import Path
from uuid import UUID
from fastapi import FastAPI, Query
from pydantic import TypeAdapter
from .schemas.events import Event
from .schemas.api import (
    HealthResponse, DeviceStatus, Page, EventQuery, BatchRequest,
    BatchResponse, AnalysisRequest, AnalysisTask, ErrorResponse,
)

ROOT = Path(__file__).resolve().parent


def export():
    (ROOT / 'schemas' / 'event.schema.json').write_text(
        json.dumps(TypeAdapter(Event).json_schema(), indent=2), encoding='utf-8')
    for platform in ('edge', 'cloud'):
        app = FastAPI(title=f'Health Monitor {platform} contract', version='1.0.0')

        def contract_only():
            raise NotImplementedError('Contract export only')

        errors = {code: {'model': ErrorResponse} for code in (400, 401, 403, 404, 409, 413, 422, 429, 503)}

        def add(path, method, response, endpoint=contract_only, status=200):
            app.add_api_route(path, endpoint, methods=[method], response_model=response,
                             status_code=status, responses=errors)

        add('/health', 'GET', HealthResponse)

        def event_list(query: EventQuery = Query()):
            return contract_only()

        if platform == 'edge':
            add('/api/v1/device/status', 'GET', DeviceStatus)
            add('/api/v1/events/recent', 'GET', Page[Event], event_list)
        else:
            def batch(body: BatchRequest):
                return contract_only()

            def event_detail(event_id: UUID):
                return contract_only()

            def create_task(body: AnalysisRequest):
                return contract_only()

            def task_detail(task_id: UUID):
                return contract_only()

            def devices(cursor: str | None = None, limit: int = Query(50, ge=1, le=200)):
                return contract_only()

            add('/api/v1/events/batch', 'POST', BatchResponse, batch)
            add('/api/v1/events', 'GET', Page[Event], event_list)
            add('/api/v1/events/{event_id}', 'GET', Event, event_detail)
            add('/api/v1/analysis/tasks', 'POST', AnalysisTask, create_task, 202)
            add('/api/v1/analysis/tasks/{task_id}', 'GET', AnalysisTask, task_detail)
            add('/api/v1/devices', 'GET', Page[DeviceStatus], devices)
        spec = app.openapi()
        spec['components']['securitySchemes'] = {
            'DeviceBearer': {'type': 'http', 'scheme': 'bearer',
                             'description': 'Per-device opaque token; dashboard uses scoped operator token.'}}
        for path, methods in spec['paths'].items():
            for operation in methods.values():
                if path != '/health':
                    operation['security'] = [{'DeviceBearer': []}]
                if path == '/api/v1/analysis/tasks':
                    operation.setdefault('parameters', []).append({
                        'name': 'Idempotency-Key', 'in': 'header', 'required': True,
                        'schema': {'type': 'string', 'minLength': 1, 'maxLength': 128}})
        (ROOT / f'openapi-{platform}.json').write_text(json.dumps(spec, indent=2), encoding='utf-8')


if __name__ == '__main__':
    export()
