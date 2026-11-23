"""Additive v1 transport for device-executed sleep-stage tasks."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.interfaces import ToolResult
from .api import AnalysisRequest, AnalysisTask, ErrorDetail
from .events import ContractModel, UTCModel

STAGE_SKILL = 'sleep_stage_inference'
STAGE_MODEL_SHA256 = 'f24597fe2ec04170e0684e9c4266b7b05802dde3901833ed5264456c8ef92854'
SHA256 = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Stage = Literal['Wake', 'N1', 'N2', 'N3', 'REM']
STAGES = ('Wake', 'N1', 'N2', 'N3', 'REM')


class SleepStageRequest(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    subject_id: int = Field(ge=1, le=13, strict=True)
    epoch_id: int = Field(ge=1, strict=True)


class EdgeAnalysisRequest(ContractModel):
    device_id: str = Field(min_length=1, max_length=128)
    analysis_type: Literal['sleep_stage_inference']
    epoch: SleepStageRequest


TaskRequest = AnalysisRequest | EdgeAnalysisRequest


class EdgeClaim(UTCModel):
    task_id: UUID
    lease: AwareDatetime
    request: EdgeAnalysisRequest


class Evidence(UTCModel):
    skill: Literal['sleep_stage_inference']
    agent_run_id: UUID
    call_id: UUID
    executed_at: AwareDatetime
    epoch: SleepStageRequest
    backend: Literal['CIX NOE NPU']
    model_id: Literal['aauwss_ppg_cnn_int8_26q2']
    input_sha256: SHA256
    model_sha256: SHA256
    predicted_stage: Stage
    limitations: list[str] = Field(min_length=1, max_length=16)


class EdgeOutcome(ContractModel):
    result: ToolResult | None = None
    evidence: Evidence | None = None
    error: ErrorDetail | None = None

    @model_validator(mode='after')
    def check_evidence(self):
        if self.error is not None:
            if self.result is not None or self.evidence is not None:
                raise ValueError('failed outcomes carry only an error')
            return self
        e, r = self.evidence, self.result
        if not e or not r or r.status != 'succeeded' or r.error is not None or r.call_id != e.call_id:
            raise ValueError('success requires a matching tool result and evidence')
        data = r.data
        expected = {**e.epoch.model_dump(), 'status': 'ok', 'backend': e.backend,
                    'model_id': e.model_id, 'predicted_stage': e.predicted_stage,
                    'limitations': e.limitations}
        source = data.get('source')
        execution = data.get('execution')
        if (any(data.get(k) != v for k, v in expected.items()) or not isinstance(source, dict)
                or source.get('input_sha256') != e.input_sha256 or source.get('model_sha256') != e.model_sha256
                or e.model_sha256 != STAGE_MODEL_SHA256
                or not isinstance(execution, dict) or 'experimental_low_accuracy_model' not in e.limitations):
            raise ValueError('evidence does not match verified inference')
        events = execution.get('api_events')
        if not isinstance(events, list) or not any(
            isinstance(event, dict) and event.get('api') == 'noe_job_infer_sync'
            and event.get('status') in ('0', 'noe_status_t.NOE_STATUS_SUCCESS')
            for event in events
        ):
            raise ValueError('missing successful NOE inference trace')
        logits = data.get('logits')
        if (not isinstance(logits, list) or len(logits) != len(STAGES)
                or any(type(value) not in (int, float) for value in logits)
                or STAGES[max(range(len(STAGES)), key=logits.__getitem__)] != e.predicted_stage):
            raise ValueError('invalid stage logits')
        return self


class EdgeCompletion(UTCModel):
    lease: AwareDatetime
    outcome: EdgeOutcome


class EdgeTaskStatus(AnalysisTask):
    analysis_type: Literal['sleep_stage_inference'] = STAGE_SKILL


class EdgeTaskDetail(EdgeTaskStatus):
    outcome: EdgeOutcome | None = None
