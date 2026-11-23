"""Opt-in frozen SLP posture inference over the existing board-local worker."""

import asyncio
import hashlib
import json
import math
import os
import re
import signal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from shared.schemas.events import ContractModel

DEPTH_SKILL = 'depth_posture_analysis'
DATASET = 'slp_cover1_frozen_v03'
MODEL = 'depth_posture_v03'
MODEL_SHA = 'ad33423c78e738a9f5f14c1a4e4195725f41c4eab7bd76323fde6a73a10a7478'
INTERFACE_SHA = '38bb9b4751fdfa8ef146e62f58189314a1450987149f9588afa921ff8d769499'
REFERENCE_SHA = '856a9a497bf7f96a238beb76125bae55be45aca0eb9d6e07b1a990dc27020e6b'
RAW_SHA = 'c467b9b557e73ccbb34ed18ebc1b0f4b5682e631fe7875db2e9383c998a47683'
CLASSES = ('supine', 'left', 'right')
SHA = r'^[0-9a-f]{64}$'


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


class DepthPostureRequest(ContractModel):
    dataset_id: Literal['slp_cover1_frozen_v03']
    subject_id: int = Field(strict=True, ge=1, le=102)
    pose_id: int = Field(strict=True, ge=1, le=45)
    model_version: Literal['depth_posture_v03']

    @property
    def sample_id(self) -> str:
        return f's{self.subject_id:03d}_p{self.pose_id:02d}'


class DepthPostureOutput(DepthPostureRequest):
    status: Literal['success', 'unavailable', 'timeout', 'failed', 'cancelled']
    predicted_posture: Literal['supine', 'left', 'right'] | None = None
    class_labels: tuple[Literal['supine'], Literal['left'], Literal['right']] = CLASSES
    dequantized_logits: tuple[float, float, float] | None = None
    inference_device: str | None = Field(default=None, pattern=r'^CIX P1 NPU on [\w.-]+$')
    input_sha256: str | None = Field(default=None, pattern=SHA)
    model_sha256: Literal['ad33423c78e738a9f5f14c1a4e4195725f41c4eab7bd76323fde6a73a10a7478'] = MODEL_SHA
    evidence_reference: str
    execution_id: UUID
    runtime_ms: float = Field(ge=0)
    reason: str | None = None

    @model_validator(mode='after')
    def state_is_consistent(self):
        if self.evidence_reference != f'{self.execution_id}/result.json':
            raise ValueError('invalid execution evidence reference')
        if self.status == 'success':
            if (self.reason is not None or not self.input_sha256 or not self.inference_device
                    or self.dequantized_logits is None
                    or self.predicted_posture != CLASSES[max(range(3), key=self.dequantized_logits.__getitem__)]):
                raise ValueError('invalid successful posture output')
        elif (not self.reason or self.predicted_posture is not None or self.dequantized_logits is not None
              or self.inference_device is not None):
            raise ValueError('failed posture output cannot include a prediction')
        return self


@dataclass(frozen=True)
class AuthorizedDepthSample:
    request: DepthPostureRequest
    input_sha256: str
    preprocessed_sha256: str


class DepthAccessDenied(ValueError):
    pass


def display_posture(data: dict, call_id: UUID) -> str:
    return (f"受试者 {data['subject_id']} 的第 {data['pose_id']} 张深度帧，"
            f"由 {data['inference_device']} 预测为 {data['predicted_posture']}。"
            f"依据为已校验的模型及输入哈希、执行记录和反量化 logits。"
            f"该结论限于静态帧。 [调用 {call_id}]")


class DepthPostureRuntime:
    def __init__(self, *, python: Path, interface: Path, assets: Path, evidence_root: Path, timeout: float = 5):
        if not math.isfinite(timeout) or not 0 < timeout <= 5:
            raise ValueError('invalid depth worker timeout')
        self.python = python.absolute()
        self.interface = interface.resolve()
        self.assets = assets.resolve()
        self.evidence_root = evidence_root.resolve()
        self.timeout = timeout
        self._slot = asyncio.Lock()

    def register(self, requests: Iterable[DepthPostureRequest]) -> tuple[AuthorizedDepthSample, ...]:
        """Resolve operator-selected IDs against the pinned V0.3 freeze."""
        import numpy as np

        folder = self.assets / 'frozen'
        if digest(folder / 'reference.json') != REFERENCE_SHA or digest(folder / 'raw.npy') != RAW_SHA:
            raise ValueError('frozen registry digest mismatch')
        samples = read(folder / 'reference.json')['samples']
        raw = np.load(folder / 'raw.npy', mmap_mode='r', allow_pickle=False)
        if raw.shape != (len(samples), 128, 54) or raw.dtype != np.dtype('<f4'):
            raise ValueError('frozen registry tensor contract mismatch')
        by_id = {row['id']: (index, row) for index, row in enumerate(samples)}
        grants = []
        for request in requests:
            index, row = by_id[request.sample_id]
            if (row['subject'], row['pose']) != (request.subject_id, request.pose_id):
                raise ValueError('frozen ID does not match sample index')
            raw_sha = hashlib.sha256(raw[index].tobytes(order='C')).hexdigest()
            grants.append(AuthorizedDepthSample(request.model_copy(deep=True), raw_sha, row['input_sha256']))
        return tuple(grants)

    async def _invoke(self, request: DepthPostureRequest, directory: Path, process: dict) -> dict:
        if digest(self.interface) != INTERFACE_SHA or digest(self.assets / f'{MODEL}.cix') != MODEL_SHA:
            raise ValueError('inference interface or CIX digest mismatch')
        with (directory / 'stdout.log').open('xb') as stdout, (directory / 'stderr.log').open('xb') as stderr:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                str(self.python), '-B', str(self.interface), '--assets', str(self.assets),
                '--evidence', str(self.evidence_root / 'board'), '--timeout-s', str(self.timeout),
                stdin=asyncio.subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True,
            ))
            child = None
            try:
                child = await asyncio.shield(spawn)
                process['pid'] = child.pid
                await child.communicate(request.model_dump_json().encode())
            except asyncio.CancelledError:
                child = child or await asyncio.shield(spawn)
                process['pid'] = child.pid
                if child.returncode is None:
                    try:
                        # Python SIGINT enters the V0.3.1 finally block, which kills/reaps its own NPU group.
                        child.send_signal(signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(child.wait(), timeout=self.timeout + 1)
                    except TimeoutError:
                        # Last resort for a hung wrapper. Its pinned runner opens a separate process group.
                        children = Path(f'/proc/{child.pid}/task/{child.pid}/children')
                        for pid in children.read_text().split() if children.is_file() else ():
                            try:
                                os.killpg(int(pid), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        if child.returncode is None:
                            child.kill()
                        await child.wait()
                raise
            finally:
                if child is not None:
                    process['exit_code'] = child.returncode
        if not 0 <= (directory / 'stdout.log').stat().st_size <= 16_384:
            raise ValueError('unexpected interface response size')
        result = read(directory / 'stdout.log')
        expected_code = 0 if result['execution_status'] == 'success' else 1
        if child.returncode != expected_code:
            raise ValueError('interface exit code and status differ')
        return result

    def _board_evidence(self, output: dict, grant: AuthorizedDepthSample) -> dict:
        reference = output['evidence_reference']
        if not re.fullmatch(r'[0-9a-f]{32}/result\.json', reference):
            raise ValueError('invalid board evidence reference')
        path = self.evidence_root / 'board' / reference
        evidence = read(path)
        if (evidence['result'] != output or evidence['request'] != grant.request.model_dump()
                or evidence['sample_id'] != grant.request.sample_id):
            raise ValueError('board evidence scope mismatch')
        if output['execution_status'] == 'success':
            if (output['input_sha256'] != grant.input_sha256 or output['model_version'] != MODEL
                    or output['class_score_kind'] != 'dequantized_logits'
                    or set(output['class_scores']) != set(CLASSES)
                    or evidence['verified_model_sha256'] != MODEL_SHA
                    or evidence['preprocessed_sha256'] != grant.preprocessed_sha256
                    or evidence['process']['exit_code'] != 0
                    or set(evidence['files']) != {'worker.json', 'stdout.log', 'stderr.log'}
                    or any(digest(path.with_name(name)) != checksum for name, checksum in evidence['files'].items())):
                raise ValueError('board record or pinned sample mismatch')
            worker = read(path.with_name('worker.json'))
            row, = worker['samples']
            if (worker['backend'] != 'CIX NOE NPU' or worker['execution_status'] != 'success'
                    or '/dev/aipu' not in worker['open_aipu_fds'].values()
                    or '/dev/aipu' not in row['open_aipu_fds'].values()
                    or worker['classes'] != list(CLASSES) or worker['model_sha256'] != MODEL_SHA
                    or worker['reference_sha256'] != REFERENCE_SHA or row['id'] != grant.request.sample_id
                    or row['input_sha256'] != grant.preprocessed_sha256 or row['npu_output_shape'] != [1, 3]
                    or row['npu_predicted'] != output['predicted_posture']
                    or output['inference_device'] != f"CIX P1 NPU on {worker['device']}"
                    or row['npu_logits'] != [output['class_scores'][label] for label in CLASSES]):
                raise ValueError('unverified NPU result')
            stdout = path.with_name('stdout.log').read_text(encoding='utf-8')
            for api in ('noe_load_tensor', 'noe_job_infer_sync', 'noe_get_tensor'):
                if f'{row["id"]} {api}: noe_status_t.NOE_STATUS_SUCCESS' not in stdout:
                    raise ValueError('missing successful NPU API evidence')
        return evidence

    async def infer(self, request: DepthPostureRequest, authorized: Mapping[str, AuthorizedDepthSample],
                    call_id: UUID) -> DepthPostureOutput:
        grant = authorized.get(request.sample_id)
        if grant is None or grant.request != request:
            raise DepthAccessDenied('depth_posture_not_authorized')
        start = asyncio.get_running_loop().time()
        directory = self.evidence_root / str(call_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        base = dict(**request.model_dump(), evidence_reference=f'{call_id}/result.json', execution_id=call_id)
        process: dict = {}
        output = None
        board = None
        details: dict = {}

        def publish(status, reason=None, **fields):
            result = DepthPostureOutput(
                **base, status=status, reason=reason, runtime_ms=(asyncio.get_running_loop().time() - start) * 1000,
                **fields,
            )
            final = directory / 'result.json'
            staging = directory / 'result.tmp'
            staging.write_text(json.dumps({
                'result': result.model_dump(mode='json'), 'interface_process': process,
                'board_output': output, 'worker_process': board['process'] if board else None, **details,
            }, allow_nan=False, indent=2), encoding='utf-8')
            staging.replace(final)
            return result

        try:
            async with asyncio.timeout(self.timeout + 2):
                async with self._slot:
                    output = await self._invoke(request, directory, process)
            board = self._board_evidence(output, grant)
            status = output['execution_status']
            if status != 'success':
                return publish(status, output['error_code'], input_sha256=grant.input_sha256)
            return publish('success', predicted_posture=output['predicted_posture'],
                           dequantized_logits=tuple(output['class_scores'][item] for item in CLASSES),
                           input_sha256=grant.input_sha256, inference_device=output['inference_device'])
        except asyncio.CancelledError:
            publish('cancelled', 'depth_call_cancelled', input_sha256=grant.input_sha256)
            raise
        except TimeoutError:
            return publish('timeout', 'depth_interface_timeout', input_sha256=grant.input_sha256)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            details['diagnostic'] = type(exc).__name__
            return publish('failed', 'depth_execution_or_evidence_invalid', input_sha256=grant.input_sha256)

    def verifies(self, result: DepthPostureOutput, grant: AuthorizedDepthSample) -> bool:
        if (result.status != 'success' or result.input_sha256 != grant.input_sha256
                or result.model_dump(include=set(DepthPostureRequest.model_fields)) != grant.request.model_dump()):
            return False
        try:
            evidence = read(self.evidence_root / result.evidence_reference)
            if evidence['result'] != result.model_dump(mode='json'):
                return False
            output = evidence['board_output']
            self._board_evidence(output, grant)
            return (output['execution_status'] == 'success'
                    and output['predicted_posture'] == result.predicted_posture
                    and output['inference_device'] == result.inference_device
                    and tuple(output['class_scores'][label] for label in CLASSES) == result.dequantized_logits)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return False
