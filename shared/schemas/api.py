"""Transport DTOs; endpoint behavior is specified in docs/api-contract.md."""
from typing import Generic, Literal, TypeVar
from uuid import UUID
from pydantic import AwareDatetime, Field, JsonValue, model_validator
from .events import ContractModel, UTCModel, Event

T = TypeVar('T')


class ErrorDetail(ContractModel):
    code: str
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ErrorResponse(ContractModel):
    request_id: str
    error: ErrorDetail


class HealthResponse(ContractModel):
    status: Literal['ok', 'degraded']
    service: Literal['edge', 'cloud']
    api_version: Literal['v1'] = 'v1'
    schema_version: Literal['1.0.0'] = '1.0.0'


class DeviceStatus(UTCModel):
    device_id: str
    last_seen: AwareDatetime | None = None
    connectivity: Literal['online', 'offline', 'unknown']
    pending_upload_count: int = Field(default=0, ge=0)
    sensors: dict[str, Literal['connected', 'disconnected', 'unknown']] = Field(default_factory=dict)


class Page(ContractModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class EventQuery(UTCModel):
    device_id: str | None = None
    event_type: Literal['physiological_window', 'pose_estimation', 'sleep_posture', 'audio_event', 'anomaly_event', 'agent_decision', 'cloud_analysis_result', 'device_metrics'] | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode='after')
    def check_range(self):
        if self.start and self.end and self.start >= self.end:
            raise ValueError('start must precede end')
        return self


class BatchRequest(ContractModel):
    # Validate each raw item separately so one invalid event cannot reject valid peers.
    events: list[dict[str, JsonValue]] = Field(min_length=1, max_length=100)


class BatchItemResult(ContractModel):
    index: int = Field(ge=0)
    event_id: UUID | None = None
    status: Literal['accepted', 'duplicate', 'rejected', 'retryable']
    error: ErrorDetail | None = None


class BatchResponse(ContractModel):
    request_id: str
    results: list[BatchItemResult]


class AnalysisRequest(UTCModel):
    device_id: str
    start: AwareDatetime
    end: AwareDatetime
    analysis_type: Literal['multimodal_summary'] = 'multimodal_summary'

    @model_validator(mode='after')
    def check_range(self):
        if self.start >= self.end:
            raise ValueError('start must precede end')
        return self


class AnalysisTask(UTCModel):
    task_id: UUID
    device_id: str
    status: Literal['queued', 'running', 'succeeded', 'failed']
    created_at: AwareDatetime
    updated_at: AwareDatetime
    result_event_id: UUID | None = None
    error: ErrorDetail | None = None


class RealtimeMessage(UTCModel):
    kind: Literal['event', 'heartbeat', 'gap', 'error']
    timestamp: AwareDatetime
    sequence: int = Field(ge=0)
    event: Event | None = None
    error: ErrorDetail | None = None
    reason: str | None = None

    @model_validator(mode='after')
    def check_kind(self):
        if (self.kind == 'event') != (self.event is not None):
            raise ValueError('event field is required only for event messages')
        if (self.kind == 'error') != (self.error is not None):
            raise ValueError('error field is required only for error messages')
        return self
