from datetime import timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.events import ContractModel, UTCModel

Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class TimeWindow(UTCModel):
    """Half-open UTC interval [start, end)."""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode='after')
    def check_order(self):
        if self.end <= self.start:
            raise ValueError('end must be after start')
        return self


class WindowRequest(ContractModel):
    dataset_id: Identifier
    session_id: Identifier
    window: TimeWindow

    def scoped_windows(self) -> tuple[TimeWindow, ...]:
        return (self.window,)


class InspectionRequest(WindowRequest):
    @model_validator(mode='after')
    def check_duration(self):
        if self.window.end - self.window.start != timedelta(seconds=10):
            raise ValueError('edge_ppg inspection requires a 10-second window')
        return self


class EvidenceRef(WindowRequest):
    source: Literal['ppg', 'gsr_imu', 'thermal', 'depth', 'history']
    record_id: Identifier | None = None


class SkillOutput(WindowRequest):
    status: Literal['ok', 'partial', 'unavailable']
    reason: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class SourceQuality(ContractModel):
    source: Literal['ppg', 'gsr_imu']
    status: Literal['usable', 'missing', 'rejected', 'unchecked']
    raw_points: int | None = Field(default=None, ge=0)
    estimated_rate_hz: float | None = Field(default=None, gt=0)
    estimated_source_span_s: float | None = Field(default=None, ge=0)
    receipt_fit_residual_s: float | None = Field(default=None, ge=0)
    estimated_span_coverage: float | None = Field(default=None, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)


class InspectionOutput(SkillOutput):
    sources: list[SourceQuality] = Field(default_factory=list)
    time_reference: str | None = None
    fusion_usable: bool | None = None
    cross_device_synchronization_verified: Literal[False] = False
    camera_status: Literal['not_inspected'] = 'not_inspected'


class ActivityMetric(ContractModel):
    value: float
    unit: Identifier
    definition: Identifier


class ActivityEvent(UTCModel):
    event_id: Identifier
    start_time: AwareDatetime
    end_time: AwareDatetime
    event_type: Identifier
    activity_metrics: dict[Identifier, ActivityMetric]
    source_modality: Literal['depth', 'thermal']
    source_ref: EvidenceRef
    quality_status: Literal['usable', 'degraded', 'unknown']
    algorithm_version: Identifier
    uncertainty_reasons: list[Identifier]
    provenance: Literal['detected', 'test']

    @model_validator(mode='after')
    def check_source(self):
        window = TimeWindow(start=self.start_time, end=self.end_time)
        if self.source_ref.source != self.source_modality or self.source_ref.window != window:
            raise ValueError('source modality and window must match the event')
        if self.source_ref.record_id is None:
            raise ValueError('camera event requires a source record reference')
        return self


class SynchronizationQuality(ContractModel):
    status: Literal['verified', 'unverified'] = 'unverified'
    alignment_ref: Identifier | None = None
    error_bound_s: float | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def check_evidence(self):
        if self.status == 'verified' and (self.alignment_ref is None or self.error_bound_s is None):
            raise ValueError('verified alignment requires a reference and error bound')
        return self


class RegisteredActivity(ContractModel):
    event: ActivityEvent
    synchronization: SynchronizationQuality = Field(default_factory=SynchronizationQuality)


class ActivityOutput(SkillOutput):
    events: list[ActivityEvent] | None = None
    observed_seconds: float | None = Field(default=None, ge=0)


class VerificationRequest(InspectionRequest):
    event_id: Identifier
    event_source: EvidenceRef | None = None
    before_window: TimeWindow | None = None
    after_window: TimeWindow | None = None

    @model_validator(mode='after')
    def check_controls(self):
        for window in (self.before_window, self.after_window):
            if window and window.end - window.start != timedelta(seconds=10):
                raise ValueError('control windows must span 10 seconds')
        if self.before_window and self.before_window.end > self.window.start:
            raise ValueError('before_window overlaps analysis window')
        if self.after_window and self.after_window.start < self.window.end:
            raise ValueError('after_window overlaps analysis window')
        return self

    def scoped_windows(self) -> tuple[TimeWindow, ...]:
        return tuple(window for window in (self.before_window, self.window, self.after_window) if window)


class PulseFeatures(ContractModel):
    pulse_rate_median_bpm: float = Field(gt=0)
    usable_observations: int = Field(gt=0)
    method: Literal['edge_ppg_usable_rolling_pulse_rate_median_v1'] = 'edge_ppg_usable_rolling_pulse_rate_median_v1'


class MotionFeatures(ContractModel):
    accel_centered_rms_raw_count: float = Field(ge=0)
    gyro_centered_rms_raw_count: float = Field(ge=0)
    sample_count: int = Field(gt=0)
    method: Literal['edge_ppg_resampled_axes_centered_rms_v1'] = 'edge_ppg_resampled_axes_centered_rms_v1'


class VerificationWindow(ContractModel):
    role: Literal['before', 'event', 'after']
    window: TimeWindow
    physiological_features: PulseFeatures | None = None
    motion_features: MotionFeatures | None = None
    signal_quality: list[SourceQuality]
    time_reference: str | None = None
    source_refs: list[EvidenceRef]
    uncertainty_reasons: list[str]


class VerificationOutput(SkillOutput):
    event_id: Identifier
    event_source: EvidenceRef | None = None
    event_window: TimeWindow | None = None
    event_provenance: Literal['detected', 'test'] | None = None
    analysis_window: TimeWindow
    before_window: TimeWindow | None = None
    after_window: TimeWindow | None = None
    source_refs: list[EvidenceRef] = Field(default_factory=list)
    physiological_features: PulseFeatures | None = None
    motion_features: MotionFeatures | None = None
    signal_quality: list[SourceQuality] = Field(default_factory=list)
    time_reference: str | None = None
    synchronization_quality: SynchronizationQuality = Field(default_factory=SynchronizationQuality)
    controls: list[VerificationWindow] = Field(default_factory=list)
    hypothesis: Literal['camera_activity_coincides_with_observable_wearable_motion'] = (
        'camera_activity_coincides_with_observable_wearable_motion'
    )
    verification_status: Literal['supported', 'inconsistent', 'insufficient_evidence'] = 'insufficient_evidence'
    uncertainty_reasons: list[str]


class HistoryRequest(WindowRequest):
    history_window: TimeWindow
    metric_version: Identifier


class HistoryOutput(SkillOutput):
    history_window: TimeWindow
    metric_version: Identifier
    comparable_nights: int | None = Field(default=None, ge=0)
    baseline_events_per_hour: float | None = Field(default=None, ge=0)
    delta_events_per_hour: float | None = None
