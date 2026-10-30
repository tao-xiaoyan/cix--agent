"""Version 1 event contract. No hardware or model dependencies."""
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, field_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class UTCModel(ContractModel):
    @field_validator('*')
    @classmethod
    def normalize_utc(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError('timezone required')
            return value.astimezone(timezone.utc)
        return value


class Channel(ContractModel):
    name: str = Field(min_length=1)
    unit: str | None = None
    sampling_rate_hz: float | None = Field(default=None, gt=0)
    samples: list[float | None] = Field(default_factory=list)
    quality: Literal['valid', 'missing', 'invalid', 'unknown'] = 'unknown'
    missing_reason: str | None = None


class PhysiologicalPayload(UTCModel):
    window_start: AwareDatetime
    window_end: AwareDatetime
    channels: list[Channel] = Field(default_factory=list)
    connection: Literal['connected', 'disconnected', 'unknown']
    protocol: str | None = None

    @field_validator('window_end')
    @classmethod
    def ordered_window(cls, value, info):
        start = info.data.get('window_start')
        if start and value < start:
            raise ValueError('window_end precedes window_start')
        return value


class Keypoint(ContractModel):
    name: str
    x: float
    y: float
    confidence: float | None = Field(default=None, ge=0, le=1)


class PersonPose(ContractModel):
    track_id: str | None = None
    keypoints: list[Keypoint]


class PosePayload(ContractModel):
    coordinate_system: Literal['normalized', 'pixels']
    frame_width: int | None = Field(default=None, gt=0)
    frame_height: int | None = Field(default=None, gt=0)
    persons: list[PersonPose]
    status: Literal['available', 'no_detection', 'unavailable']


class SleepPayload(ContractModel):
    label: str | None = None
    status: Literal['available', 'unknown', 'unavailable']
    reason: str | None = None


class AudioPayload(ContractModel):
    label: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    status: Literal['available', 'no_detection', 'unavailable']
    reason: str | None = None


class AnomalyPayload(ContractModel):
    code: str
    severity: Literal['info', 'warning', 'critical']
    description: str
    related_event_ids: list[UUID] = Field(default_factory=list)


class DecisionPayload(ContractModel):
    decision: str
    rationale: str
    actions: list[str] = Field(default_factory=list)
    related_event_ids: list[UUID] = Field(default_factory=list)
    execution_status: Literal['proposed', 'executed', 'failed', 'skipped'] = 'proposed'


class AnalysisPayload(ContractModel):
    task_id: UUID
    summary: str
    findings: list[str] = Field(default_factory=list)
    related_event_ids: list[UUID] = Field(default_factory=list)


class MetricsPayload(ContractModel):
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_used_bytes: int | None = Field(default=None, ge=0)
    disk_free_bytes: int | None = Field(default=None, ge=0)
    temperature_celsius: float | None = None
    pending_upload_count: int = Field(default=0, ge=0)
    sensors: dict[str, Literal['connected', 'disconnected', 'unknown']] = Field(default_factory=dict)


class EventBase(UTCModel):
    schema_version: Literal['1.0.0'] = '1.0.0'
    event_id: UUID
    device_id: str = Field(min_length=1, max_length=128)
    timestamp: AwareDatetime
    source: Literal['mock', 'model', 'sensor', 'rule', 'agent', 'cloud', 'system']
    model_name: str | None = None
    model_version: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class PhysiologicalEvent(EventBase):
    event_type: Literal['physiological_window']
    payload: PhysiologicalPayload


class PoseEvent(EventBase):
    event_type: Literal['pose_estimation']
    payload: PosePayload


class SleepEvent(EventBase):
    event_type: Literal['sleep_posture']
    payload: SleepPayload


class AudioEvent(EventBase):
    event_type: Literal['audio_event']
    payload: AudioPayload


class AnomalyEvent(EventBase):
    event_type: Literal['anomaly_event']
    payload: AnomalyPayload


class AgentDecisionEvent(EventBase):
    event_type: Literal['agent_decision']
    payload: DecisionPayload


class CloudAnalysisEvent(EventBase):
    event_type: Literal['cloud_analysis_result']
    payload: AnalysisPayload


class DeviceMetricsEvent(EventBase):
    event_type: Literal['device_metrics']
    payload: MetricsPayload


Event = Annotated[PhysiologicalEvent | PoseEvent | SleepEvent | AudioEvent | AnomalyEvent | AgentDecisionEvent | CloudAnalysisEvent | DeviceMetricsEvent, Field(discriminator='event_type')]
