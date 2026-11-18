"""Authorized, read-only AAUWSS epoch inference on the local CIX NOE runtime."""

import hashlib
import platform
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from shared.schemas.events import ContractModel

STAGE_SKILL = 'sleep_stage_inference'
STAGES = ['Wake', 'N1', 'N2', 'N3', 'REM']
MODEL_HASH = 'f24597fe2ec04170e0684e9c4266b7b05802dde3901833ed5264456c8ef92854'
MODEL_LIMITATION = 'experimental_low_accuracy_model'


class SleepStageRequest(ContractModel):
    dataset_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    subject_id: int = Field(ge=1, le=13, strict=True)
    epoch_id: int = Field(ge=1, strict=True)


class SleepStageOutput(SleepStageRequest):
    status: Literal['ok', 'unavailable']
    reason: str | None = None
    predicted_stage: Literal['Wake', 'N1', 'N2', 'N3', 'REM'] | None = None
    logits: list[float] | None = Field(default=None, min_length=5, max_length=5)
    backend: Literal['CIX NOE NPU'] | None = None
    device: str | None = None
    model_id: str = 'aauwss_ppg_cnn_int8_26q2'
    source: dict = Field(default_factory=dict)
    execution: dict = Field(default_factory=dict)
    human_annotation: str | None = None
    limitations: list[str] = Field(default_factory=lambda: [MODEL_LIMITATION])
    test_accuracy: float = 0.216290212183436
    test_macro_f1: float = 0.2015906574483773


@dataclass(frozen=True)
class AuthorizedEpoch:
    request: SleepStageRequest
    input_path: Path
    input_sha256: str
    source_pickle_sha256: str
    source_row: int
    human_annotation: str

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.request.dataset_id, self.request.subject_id, self.request.epoch_id)


class StageAccessDenied(ValueError):
    pass


@dataclass(frozen=True)
class SleepStageRuntime:
    model_path: Path
    model_sha256: str = MODEL_HASH

    def infer(self, request: SleepStageRequest, authorized: Mapping[tuple[str, int, int], AuthorizedEpoch]):
        key = (request.dataset_id, request.subject_id, request.epoch_id)
        asset = authorized.get(key)
        if asset is None:
            raise StageAccessDenied('sleep_stage_not_authorized')
        fields = request.model_dump()
        try:
            import io
            import numpy as np
            content = asset.input_path.read_bytes()
            if (hashlib.sha256(content).hexdigest() != asset.input_sha256
                    or hashlib.sha256(self.model_path.read_bytes()).hexdigest() != self.model_sha256):
                return SleepStageOutput(**fields, status='unavailable', reason='sleep_stage_asset_hash_mismatch')
            x = np.load(io.BytesIO(content), allow_pickle=False)
            if x.shape != (1, 1, 1920) or x.dtype != np.float32 or not np.isfinite(x).all():
                return SleepStageOutput(**fields, status='unavailable', reason='sleep_stage_invalid_input')
            logits, execution = run_noe(self.model_path, x)
        except ImportError:
            return SleepStageOutput(**fields, status='unavailable', reason='noe_runtime_unavailable')
        except (OSError, RuntimeError, ValueError) as exc:
            return SleepStageOutput(**fields, status='unavailable', reason='noe_inference_failed',
                                    execution={'error': str(exc)})
        return SleepStageOutput(
            **fields, status='ok', predicted_stage=STAGES[int(np.argmax(logits))], logits=logits,
            backend='CIX NOE NPU', device=platform.node(), human_annotation=asset.human_annotation,
            source={'input_sha256': asset.input_sha256, 'source_pickle_sha256': asset.source_pickle_sha256,
                    'source_row': asset.source_row, 'model_sha256': self.model_sha256,
                    'input_shape': [1, 1, 1920], 'preprocessing': 'raw float32; per-epoch z-score inside compiled graph',
                    'label_order': STAGES}, execution=execution,
        )


def run_noe(model: Path, x: 'np.ndarray') -> tuple[list[float], dict]:
    import numpy as np
    from libnoe import NPU, NOE_TENSOR_TYPE_INPUT, NOE_TENSOR_TYPE_OUTPUT, noe_create_job_cfg_t, noe_data_type_t

    events = []

    def checked(status, name):
        events.append({'api': name, 'status': str(status)})
        if status != 0:
            raise RuntimeError(f'{name}: {status}')

    npu = NPU()
    graph = job = None
    checked(npu.noe_init_context(), 'noe_init_context')
    try:
        status, value = npu.noe_load_graph(str(model))
        checked(status, 'noe_load_graph')
        graph = value
        for kind in (NOE_TENSOR_TYPE_INPUT, NOE_TENSOR_TYPE_OUTPUT):
            status, count = npu.noe_get_tensor_count(graph, kind)
            checked(status, 'noe_get_tensor_count')
            if count != 1:
                raise RuntimeError('expected one input and output')
        inp = npu.noe_get_tensor_descriptor(graph, NOE_TENSOR_TYPE_INPUT, 0)
        out = npu.noe_get_tensor_descriptor(graph, NOE_TENSOR_TYPE_OUTPUT, 0)
        if (inp.data_type != noe_data_type_t.NOE_DATA_TYPE_S8
                or out.data_type != noe_data_type_t.NOE_DATA_TYPE_S8 or inp.size != 1920 or out.size != 5):
            raise RuntimeError('compiled model tensor contract mismatch')
        raw_input = np.round(x.astype(np.float64) * inp.scale - inp.zero_point)
        encoded = np.clip(raw_input, -128, 127).astype(np.int8)
        status, value = npu.noe_create_job(graph, noe_create_job_cfg_t())
        checked(status, 'noe_create_job')
        job = value
        checked(npu.noe_load_tensor(job, 0, encoded.tobytes()), 'noe_load_tensor')
        start = time.perf_counter()
        checked(npu.noe_job_infer_sync(job, 5000), 'noe_job_infer_sync')
        elapsed = time.perf_counter() - start
        status, data = npu.noe_get_tensor(job, NOE_TENSOR_TYPE_OUTPUT, 0)
        checked(status, 'noe_get_tensor')
        raw = np.frombuffer(data, dtype=np.int8)
        logits = (raw.astype(np.float32) + out.zero_point) / out.scale
        if logits.shape != (5,) or not np.isfinite(logits).all():
            raise RuntimeError('invalid NPU output')
        execution = {'api_events': events, 'inference_ms': elapsed * 1000, 'raw_output': raw.tolist(),
                     'input_runtime_dtype': 'int8', 'output_runtime_dtype': 'int8',
                     'input_scale': inp.scale, 'output_scale': out.scale,
                     'input_zero_point': inp.zero_point, 'output_zero_point': out.zero_point,
                     'clipped_input_elements': int(((raw_input < -128) | (raw_input > 127)).sum())}
    finally:
        if job is not None:
            checked(npu.noe_clean_job(job), 'noe_clean_job')
        if graph is not None:
            checked(npu.noe_unload_graph(graph), 'noe_unload_graph')
        checked(npu.noe_deinit_context(), 'noe_deinit_context')
    return logits.tolist(), execution
