"""Board-local experimental tool. The caller supplies IDs, and the operator supplies paths."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
from uuid import uuid4

import numpy as np

DATASET = "slp_cover1_frozen_v03"
MODEL = "depth_posture_v03"
CLASSES = ("supine", "left", "right")
FRAME_SHAPE = (128, 54)
INPUT_SHAPE = (1, 1, *FRAME_SHAPE)
SCORE_KIND = "dequantized_logits"
ASSETS = {
    "depth_posture_v03.cix": "ad33423c78e738a9f5f14c1a4e4195725f41c4eab7bd76323fde6a73a10a7478",
    "run_npu.py": "6d4d8edbc0e7a2fde722a0bafcb01f8fc3b3a4313dfb206c872009ef53546cac",
    "frozen/reference.json": "856a9a497bf7f96a238beb76125bae55be45aca0eb9d6e07b1a990dc27020e6b",
    "frozen/raw.npy": "c467b9b557e73ccbb34ed18ebc1b0f4b5682e631fe7875db2e9383c998a47683",
    "frozen/inputs.npy": "8b7e08a5ee143c4406d14c1b7dca4977d111eaa240b061637d028d29f864d52d",
    "frozen/preprocess_v02.py": "81b6738cbbe18b0314a361c2ff0d8ce6b9f973d3c47c6f1a30f6a52f1a27e395",
}


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tensor_sha(tensor):
    return hashlib.sha256(tensor.tobytes(order="C")).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class InferenceError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Runtime:
    assets: Path
    evidence: Path
    timeout_s: float = 5.0

    def __post_init__(self):
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")


def validate_request(request):
    if (not isinstance(request, dict)
            or set(request) != {"dataset_id", "subject_id", "pose_id", "model_version"}
            or type(request["dataset_id"]) is not str or type(request["model_version"]) is not str
            or type(request["subject_id"]) is not int or type(request["pose_id"]) is not int
            or not 1 <= request["subject_id"] <= 102 or not 1 <= request["pose_id"] <= 45):
        raise InferenceError("unavailable", "invalid_request")
    if request["dataset_id"] != DATASET:
        raise InferenceError("unavailable", "unknown_dataset")
    if request["model_version"] != MODEL:
        raise InferenceError("unavailable", "unknown_model")


def resolve_frame(assets, identity):
    for name, expected in ASSETS.items():
        if file_sha(assets / name) != expected:
            raise InferenceError("failed", "asset_integrity_error")
    folder = assets / "frozen"
    reference = read_json(folder / "reference.json")
    for i, sample in enumerate(reference["samples"]):
        if sample["id"] == identity:
            raw = np.load(folder / "raw.npy", mmap_mode="r", allow_pickle=False)
            tensor = np.load(folder / "inputs.npy", mmap_mode="r", allow_pickle=False)[i]
            return raw[i], tensor, sample
    raise InferenceError("unavailable", "sample_not_in_frozen_dataset")


def run_worker(runtime, call_dir, identity, process):
    args = [
        sys.executable, "-B", "-u", str(runtime.assets.resolve() / "run_npu.py"),
        "--model", str(runtime.assets.resolve() / "depth_posture_v03.cix"),
        "--model-sha256", ASSETS["depth_posture_v03.cix"],
        "--frozen", str(runtime.assets.resolve() / "frozen"),
        "--sample-id", identity, "--warmups", "0", "--out", str(call_dir / "worker.json"),
    ]
    deadline = time.monotonic() + runtime.timeout_s
    # Serialize this experiment's requests even when multiple wrappers share its evidence root.
    with (runtime.evidence / ".npu.lock").open("ab") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InferenceError("timeout", "npu_queue_timeout")
                time.sleep(min(0.01, remaining))
        if time.monotonic() >= deadline:
            raise InferenceError("timeout", "npu_queue_timeout")
        with (call_dir / "stdout.log").open("xb") as stdout, (call_dir / "stderr.log").open("xb") as stderr:
            start = time.perf_counter_ns()
            child = subprocess.Popen(args, stdout=stdout, stderr=stderr, start_new_session=True)
            process.update(pid=child.pid, timeout_s=runtime.timeout_s)
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                process["timed_out"] = True
                raise InferenceError("timeout", "npu_worker_timeout") from None
            finally:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                process.update(exit_code=child.returncode, worker_ms=(time.perf_counter_ns() - start) / 1e6)
        if child.returncode != 0:
            raise InferenceError("failed", "npu_worker_failed")


def validate_worker(result, identity, normalized, normalized_sha):
    try:
        row, = result["samples"]
        quantized = row["raw_output"]
        scores = np.asarray(row["npu_logits"], dtype=np.float64)
        inp, out = result["input_descriptor"], result["output_descriptor"]
        detected = (
            result["backend"] == row["backend"] == "CIX NOE NPU"
            and result["execution_status"] == row["execution_status"] == "success"
            and "/dev/aipu" in row["open_aipu_fds"].values()
            and result["device"] == row["device"] == platform.node()
        )
        if (not detected or result["model_version"] != MODEL or row["model_version"] != MODEL
                or result["model_sha256"] != ASSETS["depth_posture_v03.cix"]
                or result["reference_sha256"] != ASSETS["frozen/reference.json"]
                or result["classes"] != list(CLASSES) or row["id"] != identity
                or row["input_sha256"] != normalized_sha
                or result["preprocessed_shape"] != list(INPUT_SHAPE)
                or result["preprocessed_dtype"] != "float32"
                or (inp["shape_logical"], inp["dtype"], inp["bytes"]) != (list(INPUT_SHAPE), "uint8", 6912)
                or (out["shape_logical"], out["dtype"], out["bytes"]) != ([1, 3], "int8", 3)
                or (inp["scale"], inp["zero_point"]) != (483.9969177246094, 0)
                or (out["scale"], out["zero_point"]) != (21.09007453918457, 0)
                or row["npu_output_shape"] != [1, 3] or scores.shape != (3,) or not np.isfinite(scores).all()
                or len(quantized) != 3 or any(type(x) is not int or not -128 <= x <= 127 for x in quantized)
                or not math.isfinite(out["scale"]) or out["scale"] <= 0
                or not np.allclose(scores, (np.asarray(quantized) + out["zero_point"]) / out["scale"],
                                   rtol=0, atol=1e-6)):
            raise ValueError("provenance or tensor mismatch")
        encoded = np.clip(np.rint(normalized.astype(np.float64) * inp["scale"]), 0, 255).astype(np.uint8)
        if row["runtime_input_sha256"] != tensor_sha(encoded):
            raise ValueError("Runtime input digest mismatch")
        predicted = CLASSES[int(scores.argmax())]
        if row["npu_predicted"] != predicted:
            raise ValueError("argmax mismatch")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise InferenceError("failed", "invalid_npu_result") from exc
    return predicted, dict(zip(CLASSES, scores.tolist(), strict=True)), result["device"]


def depth_posture_infer(request, runtime):
    """Return a fail-closed tool result and publish a unique evidence file."""
    start = time.perf_counter_ns()
    issued = datetime.now(timezone.utc).isoformat()
    identity = uuid4().hex
    call_dir = runtime.evidence.resolve() / identity
    call_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {
        "predicted_posture": None, "class_scores": None, "class_score_kind": SCORE_KIND,
        "inference_device": None, "model_version": None, "input_sha256": None,
        "execution_status": "failed", "error_code": None,
        "evidence_reference": f"{identity}/result.json",
    }
    process = {"pid": None, "exit_code": None, "worker_ms": None, "timed_out": False}
    evidence = {
        "issued_utc": issued, "request": None, "sample_id": None, "process": process,
        "model_sha256": ASSETS["depth_posture_v03.cix"], "verified_model_sha256": None,
        "preprocessed_sha256": None,
    }
    try:
        validate_request(request)
        evidence["request"] = request
        result["model_version"] = MODEL
        sample_id = f"s{request['subject_id']:03d}_p{request['pose_id']:02d}"
        evidence["sample_id"] = sample_id
        frame, normalized, sample = resolve_frame(runtime.assets, sample_id)
        evidence["verified_model_sha256"] = ASSETS["depth_posture_v03.cix"]
        if frame.shape != FRAME_SHAPE or frame.dtype != np.dtype("<f4"):
            raise InferenceError("unavailable", "invalid_depth_shape_or_dtype")
        result["input_sha256"] = tensor_sha(frame)
        valid_count = int(np.count_nonzero(np.isfinite(frame) & (frame > 0)))
        evidence["valid_pixels"] = valid_count
        if valid_count == 0:
            raise InferenceError("unavailable", "no_valid_depth")
        if (normalized.shape != INPUT_SHAPE or normalized.dtype != np.dtype("<f4")
                or not np.isfinite(normalized).all() or normalized.min() < 0 or normalized.max() > 1
                or tensor_sha(normalized) != sample["input_sha256"]):
            raise InferenceError("failed", "invalid_preprocessed_input")
        evidence["preprocessed_sha256"] = sample["input_sha256"]
        run_worker(runtime, call_dir, sample_id, process)
        worker = read_json(call_dir / "worker.json")
        predicted, scores, device = validate_worker(worker, sample_id, normalized, sample["input_sha256"])
        result.update(predicted_posture=predicted, class_scores=scores, inference_device=f"CIX P1 NPU on {device}",
                      execution_status="success")
    except InferenceError as exc:
        result.update(execution_status=exc.status, error_code=exc.code)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        result["error_code"] = "inference_io_or_format_error"
        evidence["exception"] = f"{type(exc).__name__}: {exc}"
    result["runtime_ms"] = (time.perf_counter_ns() - start) / 1e6
    evidence["result"] = result
    evidence["files"] = {name: file_sha(call_dir / name) for name in ("worker.json", "stdout.log", "stderr.log")
                         if (call_dir / name).is_file()}
    temporary = call_dir / "result.tmp"
    temporary.write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(call_dir / "result.json")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=5.0)
    args = parser.parse_args()
    result = depth_posture_infer(json.load(sys.stdin), Runtime(args.assets, args.evidence, args.timeout_s))
    print(json.dumps(result, allow_nan=False))
    return 0 if result["execution_status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
