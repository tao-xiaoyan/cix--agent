"""Ports only: implementations belong to each platform. Never import hardware here."""
from typing import Protocol, Literal
from uuid import UUID
from pydantic import JsonValue
from .schemas.events import ContractModel, Event
from .schemas.api import AnalysisRequest, AnalysisTask


class ModelInput(ContractModel):
    request_id: UUID
    device_id: str
    modality: Literal['physiological', 'vision', 'audio', 'llm']
    data: dict[str, JsonValue]
    # Local opaque references may identify frames/audio. No binary/base64 upload implied.
    metadata: dict[str, JsonValue] = {}


class ModelOutput(ContractModel):
    request_id: UUID
    status: Literal['ok', 'unavailable', 'invalid_input', 'failed']
    events: list[Event] = []
    message: str | None = None


class ModelAdapter(Protocol):
    async def infer(self, request: ModelInput) -> ModelOutput: ...


class PhysiologicalAdapter(ModelAdapter, Protocol):
    """Accept modality=physiological; produce physiological_window events."""


class VisionAdapter(ModelAdapter, Protocol):
    """Accept modality=vision; produce pose_estimation / sleep_posture events."""


class AudioAdapter(ModelAdapter, Protocol):
    """Accept modality=audio; produce audio_event events."""


class LLMAdapter(ModelAdapter, Protocol):
    """Accept modality=llm; produce proposed agent_decision events."""


class ToolCall(ContractModel):
    call_id: UUID
    name: str
    arguments: dict[str, JsonValue]


class ToolResult(ContractModel):
    call_id: UUID
    status: Literal['succeeded', 'failed', 'denied']
    data: dict[str, JsonValue] = {}
    error: str | None = None


class RuleScheduler(Protocol):
    def evaluate(self, events: list[Event]) -> list[ToolCall]: ...


class ToolExecutor(Protocol):
    async def execute(self, call: ToolCall) -> ToolResult: ...


class CloudClient(Protocol):
    async def create_task(self, request: AnalysisRequest, idempotency_key: str) -> AnalysisTask: ...
    async def get_task(self, task_id: UUID) -> AnalysisTask: ...
