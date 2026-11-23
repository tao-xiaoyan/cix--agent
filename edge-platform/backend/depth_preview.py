"""Authenticated edge routes for previewing the latest published depth BMP."""
import os
import stat
import struct
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Cookie, Header, Response
from pydantic import BaseModel

DEPTH_MAX_AGE_MS = 3000
DEPTH_MAX_BYTES = 8 * 1024 * 1024


class DepthStatus(BaseModel):
    state: Literal['disabled', 'waiting', 'live', 'stale', 'error']
    updated_at: datetime | None = None
    age_ms: int | None = None


def read_depth_bmp(path: Path | None) -> tuple[DepthStatus, bytes | None]:
    if path is None:
        return DepthStatus(state='disabled'), None
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return DepthStatus(state='error'), None
        with path.open('rb') as frame:
            before = os.fstat(frame.fileno())
            # Read from one handle so an atomic replacement cannot mix two frames.
            data = frame.read(DEPTH_MAX_BYTES + 1)
            after = os.fstat(frame.fileno())
    except FileNotFoundError:
        return DepthStatus(state='waiting'), None
    except OSError:
        return DepthStatus(state='error'), None

    updated_at = datetime.fromtimestamp(after.st_mtime, tz=timezone.utc)
    age_ms = max(0, (time.time_ns() - after.st_mtime_ns) // 1_000_000)
    status = DepthStatus(state='error', updated_at=updated_at, age_ms=age_ms)
    if (before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size
            or len(data) != after.st_size or not 54 <= len(data) <= DEPTH_MAX_BYTES
            or data[:2] != b'BM'):
        return status, None
    declared_size, offset, dib_size, width, height, planes, bits, compression = (
        struct.unpack_from('<I4xIIiiHHI', data, 2))
    if (declared_size != len(data) or dib_size < 40 or not 14 + dib_size <= offset < len(data)
            or width <= 0 or height == 0 or planes != 1 or bits not in (1, 4, 8, 16, 24, 32)
            or compression not in (0, 3) or (compression == 3 and bits not in (16, 32))
            or offset + ((width * bits + 31) // 32) * 4 * abs(height) > len(data)):
        return status, None
    if age_ms >= DEPTH_MAX_AGE_MS:
        status.state = 'stale'
        return status, None
    status.state = 'live'
    return status, data


def routes(
    require_auth: Callable[[str | None, str | None], None],
    error: Callable[[int, str, str], Response],
) -> APIRouter:
    """Use the host app's operator authentication and error format."""
    depth_preview_path = os.getenv('EDGE_DEPTH_PREVIEW_PATH')
    depth_path = Path(depth_preview_path) if depth_preview_path else None
    router = APIRouter()

    @router.get('/api/v1/depth/status', response_model=DepthStatus)
    def depth_status(response: Response, authorization: str | None = Header(None),
                     edge_session: str | None = Cookie(None)):
        require_auth(authorization, edge_session)
        response.headers['Cache-Control'] = 'no-store'
        status, _ = read_depth_bmp(depth_path)
        return status

    @router.get('/api/v1/depth/preview.bmp', response_class=Response,
             responses={200: {'content': {'image/bmp': {}}}})
    def depth_preview(authorization: str | None = Header(None), edge_session: str | None = Cookie(None)):
        require_auth(authorization, edge_session)
        status, data = read_depth_bmp(depth_path)
        headers = {'Cache-Control': 'no-store'}
        if status.updated_at is not None:
            headers['X-Depth-Updated-At'] = status.updated_at.isoformat()
        if data is None:
            code = 404 if status.state in ('disabled', 'waiting') else 503
            response = error(code, f'depth_{status.state}', 'Depth preview unavailable')
            response.headers.update(headers)
            return response
        return Response(data, media_type='image/bmp', headers=headers)

    return router
