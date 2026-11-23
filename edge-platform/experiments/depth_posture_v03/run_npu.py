"""Run immutable V0.3 test inputs on the existing Orange Pi libnoe runtime."""

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys
from time import perf_counter_ns

import numpy as np


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked(stage, status):
    print(f"{stage}: {status}", flush=True)
    if status != 0:
        raise RuntimeError(f"{stage} failed with NOE status {status}")


def aipu_fds():
    result = {}
    for fd in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        if target == "/dev/aipu":
            result[fd.name] = target
    return result


def descriptor(info, shape, get_data_info):
    dtype, low, high, _ = get_data_info(info.data_type)
    dtype = np.dtype(dtype)
    if (dtype.kind not in "iu" or info.size != int(np.prod(shape)) * dtype.itemsize
            or not np.isfinite(info.scale) or info.scale <= 0):
        raise RuntimeError(f"Unexpected quantized tensor: dtype={dtype}, bytes={info.size}")
    return {
        "shape_logical": list(shape), "dtype": dtype.name, "bytes": int(info.size),
        "scale": float(info.scale), "zero_point": int(info.zero_point),
        "min": int(low), "max": int(high),
    }


def execute(npu, job, output_kind, tensor, inp, out, sample_id):
    start = perf_counter_ns()
    unbounded = np.rint(tensor.astype(np.float64) * inp["scale"] - inp["zero_point"])
    encoded = np.clip(unbounded, inp["min"], inp["max"]).astype(inp["dtype"])
    payload = encoded.tobytes()
    checked(f"{sample_id} noe_load_tensor", npu.noe_load_tensor(job, 0, payload))
    sync_start = perf_counter_ns()
    status = npu.noe_job_infer_sync(job, 5000)
    sync_ns = perf_counter_ns() - sync_start
    checked(f"{sample_id} noe_job_infer_sync", status)
    status, data = npu.noe_get_tensor(job, output_kind, 0)
    checked(f"{sample_id} noe_get_tensor", status)
    raw = np.frombuffer(data, dtype=out["dtype"])
    logits = ((raw.astype(np.float32) + out["zero_point"]) / out["scale"]).reshape(out["shape_logical"])
    io_ns = perf_counter_ns() - start
    if not np.isfinite(logits).all():
        raise ValueError(f"{sample_id} returned invalid logits")
    reconstructed = (encoded.astype(np.float64) + inp["zero_point"]) / inp["scale"]
    return {
        "npu_logits": logits[0].tolist(), "npu_output_shape": list(logits.shape),
        "raw_output": raw.tolist(), "sync_inference_ms": sync_ns / 1e6,
        "inference_with_tensor_io_ms": io_ns / 1e6,
        "input_quantization_max_abs_error": float(np.abs(reconstructed - tensor).max()),
        "input_clipped_elements": int(((unbounded < inp["min"]) | (unbounded > inp["max"])).sum()),
        "runtime_input_sha256": hashlib.sha256(payload).hexdigest(),
    }


def execute_fresh_job(npu, graph, cfg_type, output_kind, tensor, inp, out, sample_id):
    start = perf_counter_ns()
    status, job = npu.noe_create_job(graph, cfg_type())
    create_ns = perf_counter_ns() - start
    checked(f"{sample_id} noe_create_job", status)
    try:
        fds = aipu_fds()
        if not fds:
            raise RuntimeError("NOE process has no open /dev/aipu file descriptor")
        record = execute(npu, job, output_kind, tensor, inp, out, sample_id)
    finally:
        cleanup_start = perf_counter_ns()
        checked(f"{sample_id} noe_clean_job", npu.noe_clean_job(job))
        record_cleanup_ms = (perf_counter_ns() - cleanup_start) / 1e6
    record.update(job_create_ms=create_ns / 1e6, job_cleanup_ms=record_cleanup_ms, open_aipu_fds=fds)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sample-id", required=True, help="Frozen sample to run in this process")
    parser.add_argument("--warmups", type=int, default=10)
    args = parser.parse_args()
    if args.warmups < 0:
        parser.error("--warmups must be nonnegative")
    root = args.frozen.resolve()
    with (root / "reference.json").open(encoding="utf-8") as stream:
        reference = json.load(stream)
    for name in ("raw.npy", "inputs.npy", "preprocess_v02.py"):
        if sha256(root / name) != reference["files"][name]:
            raise ValueError(f"Frozen file integrity check failed: {name}")
    model_hash = sha256(args.model)
    if model_hash != args.model_sha256:
        raise ValueError("CIX SHA-256 mismatch")
    if not stat.S_ISCHR(os.stat("/dev/aipu").st_mode):
        raise RuntimeError("Expected /dev/aipu character device")

    sys.path.insert(0, str(root))
    from preprocess_v02 import preprocess
    sys.path.insert(0, "/home/orangepi/cix_npu_test/vision")
    import libnoe
    from libnoe import NPU, NOE_TENSOR_TYPE_INPUT, NOE_TENSOR_TYPE_OUTPUT, noe_create_job_cfg_t
    from utils.NOE_Engine import get_data_info

    raw = np.load(root / "raw.npy", allow_pickle=False)
    frozen = np.load(root / "inputs.npy", allow_pickle=False)
    samples = reference["samples"]
    input_shape = (1, 1, 128, 54)
    output_shape = (1, 3)
    classes = ["supine", "left", "right"]
    if (reference["classes"] != classes or reference["input_shape"] != list(input_shape)
            or reference["input_dtype"] != "float32" or reference["output_shape"] != list(output_shape)
            or raw.shape != (len(samples), 128, 54) or raw.dtype != np.float32
            or frozen.shape != (len(samples), *input_shape) or frozen.dtype != np.float32):
        raise ValueError("Frozen tensor contract mismatch")
    selected = [
        (sample, frame, expected) for sample, frame, expected in zip(samples, raw, frozen, strict=True)
        if sample["id"] == args.sample_id
    ]
    if not selected:
        parser.error("Unknown --sample-id")

    device = platform.node()
    print(f"device={device} libnoe={libnoe.__file__} model_sha256={model_hash}", flush=True)
    npu = NPU()
    results = []
    with ExitStack() as cleanup:
        checked("noe_init_context", npu.noe_init_context())
        cleanup.callback(lambda: checked("noe_deinit_context", npu.noe_deinit_context()))
        start = perf_counter_ns()
        status, graph = npu.noe_load_graph(str(args.model))
        model_load_ms = (perf_counter_ns() - start) / 1e6
        checked("noe_load_graph", status)
        cleanup.callback(lambda: checked("noe_unload_graph", npu.noe_unload_graph(graph)))
        for kind in (NOE_TENSOR_TYPE_INPUT, NOE_TENSOR_TYPE_OUTPUT):
            status, count = npu.noe_get_tensor_count(graph, kind)
            checked(f"noe_get_tensor_count {kind}", status)
            if count != 1:
                raise ValueError(f"Expected one tensor of kind {kind}")
        inp = descriptor(npu.noe_get_tensor_descriptor(graph, NOE_TENSOR_TYPE_INPUT, 0),
                         input_shape, get_data_info)
        out = descriptor(npu.noe_get_tensor_descriptor(graph, NOE_TENSOR_TYPE_OUTPUT, 0),
                         output_shape, get_data_info)
        print(f"tensor_descriptors input={inp} output={out}", flush=True)
        for index, (sample, frame, expected) in enumerate(selected):
            tensor = preprocess(frame)
            input_sha = hashlib.sha256(tensor.tobytes()).hexdigest()
            if not np.array_equal(tensor, expected) or input_sha != sample["input_sha256"]:
                raise ValueError(f"Preprocessed input differs for {sample['id']}")
            if index == 0:
                for warmup in range(args.warmups):
                    execute_fresh_job(npu, graph, noe_create_job_cfg_t, NOE_TENSOR_TYPE_OUTPUT,
                                      tensor, inp, out, f"warmup_{warmup}")
            record = execute_fresh_job(npu, graph, noe_create_job_cfg_t, NOE_TENSOR_TYPE_OUTPUT,
                                       tensor, inp, out, sample["id"])
            if index == 0:
                fds = record["open_aipu_fds"]
                print(f"open_aipu_fds={fds}", flush=True)
            record.update(
                id=sample["id"], input_sha256=input_sha, model_version="depth_posture_v03",
                device=device, execution_status="success", backend="CIX NOE NPU",
                npu_predicted=classes[int(np.argmax(record["npu_logits"]))],
            )
            results.append(record)

    args.out.write_text(json.dumps({
        "backend": "CIX NOE NPU", "model_version": "depth_posture_v03",
        "execution_status": "success", "device": device, "platform": platform.platform(),
        "runtime_module": libnoe.__file__, "open_aipu_fds": fds, "model_sha256": model_hash,
        "reference_sha256": sha256(root / "reference.json"), "input_descriptor": inp,
        "output_descriptor": out, "classes": classes, "preprocessed_dtype": "float32",
        "preprocessed_shape": input_shape, "model_load_ms": model_load_ms,
        "warmup_count": args.warmups, "job_strategy": "one fresh NOE job per inference", "samples": results,
    }, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
