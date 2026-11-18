import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from statistics import median
from typing import Any, Literal, Protocol

from shared.schemas.events import ContractModel

from .contracts import (
    ActivityOutput, EvidenceRef, HistoryOutput, HistoryRequest, InspectionOutput,
    InspectionRequest, MotionFeatures, PulseFeatures, RegisteredActivity, SourceQuality,
    TimeWindow, VerificationOutput, VerificationRequest, VerificationWindow, WindowRequest,
)
from .video_pose import VIDEO_POSE, VideoArchive, VideoPoseOutput, VideoPoseRequest, VideoRange
from .sleep_stage import STAGE_SKILL, AuthorizedEpoch, SleepStageOutput, SleepStageRequest, SleepStageRuntime


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    input_model: type[ContractModel]
    output_model: type[ContractModel]
    handler: Callable[[Any], Awaitable[ContractModel]]


def video_pose_skill(archive: VideoArchive, grants: Mapping[str, VideoRange]) -> Skill:
    async def summarize(request: VideoPoseRequest) -> VideoPoseOutput:
        return await asyncio.to_thread(archive.read, request, grants)

    authorized = ', '.join(video_id for video_id in grants if video_id in archive.exports) or 'none'
    return Skill(VIDEO_POSE,
                 'Read archived per-frame person and keypoint statistics using video-relative milliseconds. '
                 f'Authorized video IDs: {authorized}. Use for pose evidence, without behavior classification.',
                 VideoPoseRequest, VideoPoseOutput, summarize)


def sleep_stage_skill(runtime: SleepStageRuntime, grants: Iterable[AuthorizedEpoch]) -> Skill:
    authorized = {asset.key: asset for asset in grants}

    async def infer(request: SleepStageRequest) -> SleepStageOutput:
        return await asyncio.to_thread(runtime.infer, request, authorized)

    return Skill(STAGE_SKILL, 'Infer an experimental five-class sleep stage on CIX NOE NPU. '
                 'Only use authorized dataset, subject and epoch identifiers: '
                 + str(list(authorized)) + '. The model is low accuracy; report its limitations.',
                 SleepStageRequest, SleepStageOutput, infer)


class WindowReader(Protocol):
    def window(self, session: str, start: float) -> tuple[dict, dict]: ...


def open_edge_ppg(root: Path) -> WindowReader:
    # Optional, trusted runtime dependency. Import only when inspecting local data.
    from inference.adapter import Adapter

    return Adapter(root)


def _public_reason(reason: str) -> str:
    # Adapter exception details may contain local paths or database internals.
    return reason.split(':', 1)[0]


def _read_window(
    request: InspectionRequest,
    root: Path | None,
    open_reader: Callable[[Path], WindowReader],
) -> tuple[InspectionOutput, dict]:
    context = request.model_dump()
    if root is None:
        return InspectionOutput(**context, status='unavailable', reason='unknown_dataset'), {}

    available = {name: (root / f'{name}.sqlite3').is_file() for name in ('ppg', 'gsr')}
    sources = [
        SourceQuality(source=source, status='unchecked' if available[name] else 'missing',
                      reasons=[] if available[name] else ['missing_database'])
        for name, source in (('ppg', 'ppg'), ('gsr', 'gsr_imu'))
    ]
    if not any(available.values()):
        return InspectionOutput(**context, status='unavailable', reason='missing_databases', sources=sources), {}
    try:
        reader = open_reader(root)
    except ImportError:
        return InspectionOutput(**context, status='unavailable', reason='edge_ppg_dependency_unavailable',
                                sources=sources), {}
    start, end = request.window.start.timestamp(), request.window.end.timestamp()
    arrays, metadata = reader.window(request.session_id, start)
    evidence = []
    for source, name in zip(sources, ('ppg', 'gsr'), strict=True):
        if not available[name]:
            continue
        details = metadata['sources'][name]
        source.status = 'usable' if details['valid'] else 'rejected'
        source.raw_points = details['raw_points']
        rate = details['estimated_rate_hz']
        source.estimated_rate_hz = rate if rate is not None and rate > 0 else None
        source.estimated_source_span_s = details.get('estimated_source_span_s')
        source.receipt_fit_residual_s = details.get('max_fit_residual_s')
        if 'estimated_source_start' in details and 'estimated_source_end' in details:
            overlap = min(end, details['estimated_source_end']) - max(start, details['estimated_source_start'])
            source.estimated_span_coverage = max(0.0, overlap) / (end - start)
        source.reasons = [_public_reason(reason) for reason in details['rejection_reasons']]
        if details['raw_points'] > 0:
            evidence.append(EvidenceRef(**context, source=source.source))

    fusion = metadata['fusion_usable'] and all(available.values())
    usable = any(source.status == 'usable' for source in sources)
    status = 'ok' if fusion else 'partial' if usable else 'unavailable'
    reason = None if fusion else 'incomplete_physiological_window' if usable else 'no_usable_window'
    return InspectionOutput(**context, status=status, reason=reason, sources=sources,
                            evidence=evidence, time_reference=metadata['time_reference'], fusion_usable=fusion), arrays


def _pulse_features(root: Path, request: InspectionRequest) -> PulseFeatures | None:
    # Reuse the acquisition pipeline's rolling rate, with its existing quality flags.
    try:
        from feature_delivery import readonly

        with closing(readonly(root / 'ppg.sqlite3')) as db:
            rows = db.execute(
                'SELECT CASE WHEN length(payload)<=8192 THEN payload END FROM metrics '
                'WHERE session=? AND recorded_utc>=? AND recorded_utc<? ORDER BY recorded_utc LIMIT 21',
                (request.session_id, request.window.start.timestamp(), request.window.end.timestamp()),
            ).fetchall()
        if not 0 < len(rows) <= 20:
            return None
        observations = [json.loads(payload) for (payload,) in rows]
        rates = [item.get('heart_rate_bpm') for item in observations
                 if item.get('quality') == 'usable' and item.get('measurement_enabled') is True]
        rates = [rate for rate in rates if type(rate) in (int, float) and isfinite(rate) and rate > 0]
        if rates:
            return PulseFeatures(pulse_rate_median_bpm=median(rates), usable_observations=len(rates))
    except (ImportError, sqlite3.Error, OSError, TypeError, ValueError, AttributeError):
        return None
    return None


def _verification_window(
    request: VerificationRequest, window: TimeWindow, role: Literal['before', 'event', 'after'],
    root: Path | None, open_reader: Callable[[Path], WindowReader],
) -> VerificationWindow:
    inspection_request = InspectionRequest(dataset_id=request.dataset_id, session_id=request.session_id, window=window)
    inspection, arrays = _read_window(inspection_request, root, open_reader)
    refs = inspection.evidence.copy()
    reasons = [inspection.reason] if inspection.reason else []
    usable = {item.source for item in inspection.sources if item.status == 'usable'}
    pulse = _pulse_features(root, inspection_request) if root is not None and 'ppg' in usable else None
    if pulse:
        refs.append(EvidenceRef(**inspection_request.model_dump(), source='ppg', record_id='metrics'))
    else:
        reasons.append('usable_rolling_pulse_rate_unavailable')
    motion = None
    if 'gsr_imu' in usable:
        axes = arrays['gsr_imu'][1:].astype('float64')
        centered = axes - axes.mean(axis=1, keepdims=True)
        axis_power = (centered ** 2).mean(axis=1)
        motion = MotionFeatures(accel_centered_rms_raw_count=float(axis_power[:3].sum() ** 0.5),
                                gyro_centered_rms_raw_count=float(axis_power[3:].sum() ** 0.5),
                                sample_count=axes.shape[1])
    else:
        reasons.append('usable_imu_unavailable')
    return VerificationWindow(role=role, window=window, physiological_features=pulse, motion_features=motion,
                              signal_quality=inspection.sources, time_reference=inspection.time_reference,
                              source_refs=refs, uncertainty_reasons=reasons)


def _verify(
    request: VerificationRequest, activity: RegisteredActivity | None,
    root: Path | None, open_reader: Callable[[Path], WindowReader],
) -> VerificationOutput:
    context = request.model_dump()
    context['analysis_window'] = request.window
    if activity is None:
        return VerificationOutput(**context, status='unavailable', reason='unknown_activity_event',
                                  uncertainty_reasons=['unknown_activity_event'])
    event = activity.event
    if request.event_source not in (None, event.source_ref):
        return VerificationOutput(**context, status='unavailable', reason='event_source_mismatch',
                                  uncertainty_reasons=['event_source_mismatch'])
    if not (request.window.start <= event.start_time and event.end_time <= request.window.end):
        return VerificationOutput(**context, status='unavailable', reason='event_outside_analysis_window',
                                  uncertainty_reasons=['event_outside_analysis_window'])
    context['event_window'] = event.source_ref.window
    if ((request.before_window and request.before_window.end > event.start_time)
            or (request.after_window and request.after_window.start < event.end_time)):
        return VerificationOutput(**context, status='unavailable', reason='control_window_overlaps_event',
                                  uncertainty_reasons=['control_window_overlaps_event'])

    summary = _verification_window(request, request.window, 'event', root, open_reader)
    controls = [_verification_window(request, window, role, root, open_reader)
                for role, window in (('before', request.before_window), ('after', request.after_window)) if window]
    refs = [event.source_ref, *summary.source_refs, *(ref for item in controls for ref in item.source_refs)]
    reasons = [*event.uncertainty_reasons, *summary.uncertainty_reasons, 'no_validated_motion_criterion']
    if activity.synchronization.status != 'verified':
        reasons.append('camera_wearable_alignment_unverified')
    if event.provenance == 'test':
        reasons.append('synthetic_camera_event')
    if event.quality_status != 'usable':
        reasons.append('camera_event_quality_not_usable')
    feature_count = sum(feature is not None for feature in (summary.physiological_features, summary.motion_features))
    return VerificationOutput(
        **context, status=('unavailable', 'partial', 'ok')[feature_count],
        event_provenance=event.provenance, reason='insufficient_evidence', evidence=refs, source_refs=refs,
        physiological_features=summary.physiological_features, motion_features=summary.motion_features,
        signal_quality=summary.signal_quality, time_reference=summary.time_reference,
        synchronization_quality=activity.synchronization,
        controls=controls, uncertainty_reasons=reasons,
    )


def local_skills(
    datasets: Mapping[str, Path] | None = None,
    *,
    open_reader: Callable[[Path], WindowReader] = open_edge_ppg,
    activities: Iterable[RegisteredActivity] = (),
) -> tuple[Skill, ...]:
    roots = {key: path.resolve() for key, path in (datasets or {}).items()}
    registered = {}
    for item in activities:
        ref = item.event.source_ref
        key = (ref.dataset_id, ref.session_id, item.event.event_id)
        if key in registered:
            raise ValueError('duplicate registered activity event')
        registered[key] = RegisteredActivity.model_validate(item.model_dump())

    async def inspect(request: InspectionRequest) -> InspectionOutput:
        result, _ = await asyncio.to_thread(_read_window, request, roots.get(request.dataset_id), open_reader)
        return result

    async def activity(request: WindowRequest) -> ActivityOutput:
        events = [item.event for (dataset, session, _), item in registered.items()
                  if (dataset, session) == (request.dataset_id, request.session_id)
                  and request.window.start <= item.event.start_time and item.event.end_time <= request.window.end]
        if not events:
            return ActivityOutput(**request.model_dump(), status='unavailable', reason='not_implemented')
        return ActivityOutput(**request.model_dump(), status='ok', events=events,
                              evidence=[event.source_ref for event in events])

    async def verify(request: VerificationRequest) -> VerificationOutput:
        item = registered.get((request.dataset_id, request.session_id, request.event_id))
        return await asyncio.to_thread(_verify, request, item, roots.get(request.dataset_id), open_reader)

    async def compare(request: HistoryRequest) -> HistoryOutput:
        return HistoryOutput(**request.model_dump(), status='unavailable', reason='no_comparable_history')

    return (
        Skill('data_inspection', 'Check 10 seconds of local PPG and GSR/IMU coverage, quality and timing.',
              InspectionRequest, InspectionOutput, inspect),
        Skill('sleep_activity_analysis', 'Read registered depth or thermal activity events in the requested window.',
              WindowRequest, ActivityOutput, activity),
        Skill('evidence_verification', 'Check PPG and IMU evidence for a registered activity event in a 10-second window.',
              VerificationRequest, VerificationOutput, verify),
        Skill('historical_comparison', 'Compare activity metrics against comparable historical sessions.',
              HistoryRequest, HistoryOutput, compare),
    )
