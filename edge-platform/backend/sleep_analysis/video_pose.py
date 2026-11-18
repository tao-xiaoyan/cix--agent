"""Read a caller-pinned CIX NPU pose export using its presentation timeline."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, model_validator

from shared.schemas.events import ContractModel

VIDEO_POSE = 'video_pose_analysis'
VideoId = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class VideoRange(ContractModel):
    """Half-open interval in milliseconds relative to the first source PTS."""

    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)

    @model_validator(mode='after')
    def ordered(self):
        if self.end_ms <= self.start_ms:
            raise ValueError('end_ms must exceed start_ms')
        return self

    def contains(self, other: 'VideoRange') -> bool:
        return self.start_ms <= other.start_ms < other.end_ms <= self.end_ms


class VideoPoseRequest(ContractModel):
    video_id: VideoId
    range_ms: VideoRange | None = None


class VideoPoseEvidence(ContractModel):
    video_id: VideoId
    video_sha256: Digest
    model_sha256: Digest
    manifest_sha256: Digest
    frames_sha256: Digest
    backend: Literal['CIX NOE NPU'] = 'CIX NOE NPU'
    time_basis: Literal['relative_source_pts'] = 'relative_source_pts'
    time_base: str
    source_pts_origin: int
    first_frame: int | None = None
    last_frame: int | None = None
    first_relative_pts: int | None = None
    last_relative_pts: int | None = None
    source_modality: Literal['unverified'] = 'unverified'


class VideoPoseSummary(ContractModel):
    frames_examined: int = Field(ge=0)
    frames_with_detections: int = Field(ge=0)
    person_detections: int = Field(ge=0, description='Per-frame post-NMS boxes, without cross-frame tracking')
    keypoints: int = Field(ge=0)
    keypoints_score_gt_0_5: int = Field(ge=0)
    high_score_keypoints_outside_frame: int = Field(ge=0)


class VideoPoseOutput(VideoPoseRequest):
    status: Literal['ok', 'unavailable']
    reason: Literal['export_missing', 'export_hash_mismatch', 'invalid_export', 'no_frames_in_range'] | None = None
    effective_range_ms: VideoRange
    summary: VideoPoseSummary | None = None
    evidence: VideoPoseEvidence | None = None
    quality_flags: list[str] = Field(default_factory=list)


class VideoExport(ContractModel):
    directory: str
    manifest_sha256: Digest
    range_ms: VideoRange


class VideoAccessDenied(ValueError):
    """An attempted video read was outside the trusted grant or archive."""


class ExportHashMismatch(ValueError):
    pass


def _read_verified(path: Path, expected: str, limit: int) -> bytes:
    with path.open('rb') as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError('export exceeds byte budget')
    if hashlib.sha256(content).hexdigest() != expected:
        raise ExportHashMismatch()
    return content


@dataclass(frozen=True)
class VideoArchive:
    root: Path
    exports: Mapping[str, VideoExport]

    def __post_init__(self):
        object.__setattr__(self, 'root', self.root.resolve())
        object.__setattr__(self, 'exports', MappingProxyType({
            video_id: VideoExport.model_validate(export.model_dump()) for video_id, export in self.exports.items()
        }))

    def read(self, request: VideoPoseRequest, grants: Mapping[str, VideoRange]) -> VideoPoseOutput:
        export = self.exports.get(request.video_id)
        if export is None:
            raise VideoAccessDenied('video_not_found')
        grant = grants.get(request.video_id)
        if grant is None:
            raise VideoAccessDenied('video_not_authorized')
        window = request.range_ms if request.range_ms is not None else grant
        if not export.range_ms.contains(grant) or not grant.contains(window):
            raise VideoAccessDenied('video_range_out_of_scope')
        try:
            paths = [(self.root / export.directory / name).resolve()
                     for name in ('frames.manifest.json', 'frames.jsonl')]
        except (OSError, RuntimeError) as exc:
            raise VideoAccessDenied('video_path_out_of_scope') from exc
        if Path(export.directory).is_absolute() or not all(path.is_relative_to(self.root) for path in paths):
            raise VideoAccessDenied('video_path_out_of_scope')
        fields = {**request.model_dump(), 'effective_range_ms': window}
        try:
            manifest = json.loads(_read_verified(paths[0], export.manifest_sha256, 64 * 1024))
            frames_sha256 = manifest['outputs']['jsonl']['sha256']
            content = _read_verified(paths[1], frames_sha256, 16 * 1024 * 1024)
            stats, evidence = _summarize(content, manifest, export, request.video_id, window)
        except FileNotFoundError:
            return VideoPoseOutput(**fields, status='unavailable', reason='export_missing')
        except ExportHashMismatch:
            return VideoPoseOutput(**fields, status='unavailable', reason='export_hash_mismatch')
        except (OSError, KeyError, TypeError, ValueError, ZeroDivisionError):
            return VideoPoseOutput(**fields, status='unavailable', reason='invalid_export')
        flags = ['source_modality_unverified', 'capture_time_utc_unverified']
        if stats.frames_examined and not stats.frames_with_detections:
            flags.append('no_pose_detections')
        if stats.high_score_keypoints_outside_frame:
            flags.append('high_score_keypoints_outside_frame')
        return VideoPoseOutput(**fields, status='ok' if stats.frames_examined else 'unavailable',
                               reason=None if stats.frames_examined else 'no_frames_in_range',
                               summary=stats, evidence=evidence, quality_flags=flags)


def _summarize(
    content: bytes, manifest: dict, export: VideoExport, video_id: str, window: VideoRange,
) -> tuple[VideoPoseSummary, VideoPoseEvidence]:
    if (manifest['schema'] != 'cix.pose.frames.v1' or manifest['backend'] != 'CIX NOE NPU'
            or manifest['model']['family'] != 'YOLOv8s-Pose' or manifest['source_modality'] != 'unverified'
            or manifest['capture_start_utc'] is not None):
        raise ValueError('unsupported export')
    source_hash = manifest['input_video']['sha256']
    model_hash = manifest['model']['sha256']
    time_base = manifest['source_stream']['time_base']
    origin = manifest['source_pts_origin']
    milliseconds_per_tick = Fraction(time_base) * 1000
    if milliseconds_per_tick <= 0 or type(origin) is not int:
        raise ValueError('invalid source timeline')
    frame_count = detected_frames = detections = keypoints = high = outside = 0
    first = last = (None, None)
    previous_pts = -1
    for index, line in enumerate(content.splitlines()):
        row = json.loads(line)
        pts = row['relative_pts']
        if (type(pts) is not int or type(row['source_pts']) is not int or row['frame_index'] != index
                or pts <= previous_pts or (index == 0 and pts != 0) or row['source_pts'] != origin + pts
                or row['time_base'] != time_base or row['video_sha256'] != source_hash
                or row['model_sha256'] != model_hash):
            raise ValueError('export frame identity or timeline mismatch')
        previous_pts = pts
        selected_ms = pts * milliseconds_per_tick
        if not window.start_ms <= selected_ms < window.end_ms:
            continue
        people = row['detections']
        width, height = row['decoded_size_wh']
        if not isinstance(people, list) or width <= 0 or height <= 0:
            raise ValueError('invalid detection frame')
        if frame_count == 0:
            first = (index, pts)
        last = (index, pts)
        frame_count += 1
        detected_frames += bool(people)
        detections += len(people)
        for person in people:
            points = person['frame_keypoints_xy_score']
            if len(points) != 17 or not 0 <= person['score'] <= 1:
                raise ValueError('invalid pose detection')
            for x, y, score in points:
                if not isfinite(x) or not isfinite(y) or not 0 <= score <= 1:
                    raise ValueError('invalid keypoint')
                keypoints += 1
                if score > 0.5:
                    high += 1
                    outside += not (0 <= x < width and 0 <= y < height)
    if (previous_pts < 0 or index + 1 != manifest['frames']
            or export.range_ms.start_ms != 0 or export.range_ms.end_ms != int(previous_pts * milliseconds_per_tick) + 1):
        raise ValueError('export frame count or extent mismatch')
    summary = VideoPoseSummary(frames_examined=frame_count, frames_with_detections=detected_frames,
                               person_detections=detections, keypoints=keypoints, keypoints_score_gt_0_5=high,
                               high_score_keypoints_outside_frame=outside)
    evidence = VideoPoseEvidence(video_id=video_id, video_sha256=source_hash, model_sha256=model_hash,
                                 manifest_sha256=export.manifest_sha256,
                                 frames_sha256=manifest['outputs']['jsonl']['sha256'],
                                 time_base=time_base, source_pts_origin=origin,
                                 first_frame=first[0], first_relative_pts=first[1],
                                 last_frame=last[0], last_relative_pts=last[1])
    return summary, evidence
