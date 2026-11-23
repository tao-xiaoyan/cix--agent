#!/usr/bin/env bash
set -euo pipefail
umask 077

repo=$(cd -- "$(dirname -- "$0")/../.." && pwd)
cd -- "$repo"

: "${CIX_DEMO_STATE_DIR:?Set a private absolute state directory}"
: "${CIX_QWEN_MODEL:?Set the path to the verified Qwen3-4B GGUF}"
: "${EDGE_DEPTH_ASSETS:?Set the verified depth asset directory}"
: "${EDGE_DEPTH_NPU_PYTHON:?Set the ARM64 NOE Python interpreter}"
: "${CIX_NOE_SITE_PACKAGES:?Set the NOE venv site-packages directory}"
: "${CIX_NOE_HELPER_ROOT:?Set the directory containing utils/NOE_Engine.py}"

agent_python=${CIX_AGENT_PYTHON:-"$repo/.venv/bin/python"}
llama=${CIX_LLAMA_SERVER:-/usr/share/cix/bin/llama-server}
port=${CIX_EDGE_PORT:-18551}
qwen_url=http://127.0.0.1:18080

export EDGE_MOCK=0
export EDGE_UI_ORIGIN=${EDGE_UI_ORIGIN:-http://127.0.0.1:5173}
export EDGE_DEPTH_INTERFACE=${EDGE_DEPTH_INTERFACE:-"$repo/edge-platform/experiments/depth_posture_v031/depth_posture_infer.py"}
export EDGE_DB_PATH=${EDGE_DB_PATH:-"$CIX_DEMO_STATE_DIR/events.sqlite3"}
export EDGE_DEPTH_RUN_DIR=${EDGE_DEPTH_RUN_DIR:-"$CIX_DEMO_STATE_DIR/runs"}
export EDGE_DEPTH_PREVIEW_PATH=${EDGE_DEPTH_PREVIEW_PATH:-"$HOME/.local/share/cix-demo/orbbec/live/depth.bmp"}
export PYTHONPATH="$repo:$repo/edge-platform:$CIX_NOE_SITE_PACKAGES:$CIX_NOE_HELPER_ROOT${PYTHONPATH:+:$PYTHONPATH}"

if [[ -n ${EDGE_CLOUD_URL:-}${EDGE_CLOUD_TOKEN:-}${EDGE_SLEEP_STAGE_ASSETS:-} ]]; then
    : "${EDGE_CLOUD_URL:?Set the cloud API URL}"
    : "${EDGE_CLOUD_TOKEN:?Supply the cloud device credential privately}"
    : "${EDGE_SLEEP_STAGE_ASSETS:?Set the verified PPG asset directory}"
    : "${EDGE_DEVICE_ID:?Set the registered cloud device ID}"
fi

[[ $CIX_DEMO_STATE_DIR = /* && ! -L $CIX_DEMO_STATE_DIR ]]
[[ $port =~ ^[0-9]{1,5}$ ]] && ((10#$port >= 1024 && 10#$port <= 65535)) || {
    printf 'CIX_EDGE_PORT must be between 1024 and 65535\n' >&2
    exit 1
}
mkdir -p -- "$CIX_DEMO_STATE_DIR"
[[ -O $CIX_DEMO_STATE_DIR && $(stat -c %a -- "$CIX_DEMO_STATE_DIR") == 700 ]]
mkdir -p -- "$EDGE_DEPTH_RUN_DIR"
[[ -c /dev/aipu && -r /dev/aipu && -w /dev/aipu ]] || {
    printf 'Current user requires read/write access to /dev/aipu\n' >&2
    exit 1
}
"$EDGE_DEPTH_NPU_PYTHON" -B -c 'import libnoe, numpy; from utils.NOE_Engine import get_data_info'
"$agent_python" -B -c 'import fastapi, uvicorn, websockets'
verify() { "$agent_python" -B "$repo/edge-platform/deploy/verify-assets.py" "$@"; }
verify depth "$EDGE_DEPTH_ASSETS"
[[ $(basename -- "$EDGE_DEPTH_INTERFACE") == depth_posture_infer.py ]]
verify depth-interface "$(dirname -- "$EDGE_DEPTH_INTERFACE")"
verify noe-helper "$CIX_NOE_HELPER_ROOT"
if [[ -f /home/orangepi/cix_npu_test/vision/utils/NOE_Engine.py ]]; then
    verify noe-helper /home/orangepi/cix_npu_test/vision
fi
if [[ -n ${EDGE_SLEEP_STAGE_ASSETS:-} ]]; then
    verify ppg "$EDGE_SLEEP_STAGE_ASSETS"
fi

qwen_pid=
app_pid=
cleanup() {
    for pid in "$app_pid" "$qwen_pid"; do
        if [[ -n $pid ]]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

curl_local() { curl --noproxy '*' -fsS --max-time 3 "$1"; }
wait_http() {
    local pid=$1 url=$2 attempts=$3
    for ((attempt=0; attempt<attempts; attempt++)); do
        kill -0 "$pid" 2>/dev/null || break
        if curl_local "$url" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    printf 'Service did not become ready: %s\n' "$url" >&2
    return 1
}
if [[ -n $(ss -ltnH "sport = :$port") ]]; then
    printf 'Edge port is already in use: %s\n' "$port" >&2
    exit 1
fi

if [[ ${CIX_REUSE_QWEN:-0} != 1 ]]; then
    if [[ -n $(ss -ltnH 'sport = :18080') ]]; then
        printf 'Qwen port in use. Verify the running model before opting into reuse.\n' >&2
        exit 1
    fi
    [[ $(basename -- "$CIX_QWEN_MODEL") == Qwen3-4B-Q4_K_M.gguf ]]
    verify qwen "$(dirname -- "$CIX_QWEN_MODEL")"
    affinity=()
    if [[ -n ${CIX_QWEN_CPUSET:-} ]]; then affinity=(taskset -c "$CIX_QWEN_CPUSET"); fi
    "${affinity[@]}" "$llama" -m "$CIX_QWEN_MODEL" --host 127.0.0.1 --port 18080 \
        -c "${CIX_QWEN_CONTEXT:-2048}" -t "${CIX_QWEN_THREADS:-8}" \
        -tb "${CIX_QWEN_BATCH_THREADS:-8}" --reasoning-budget 0 \
        >>"$CIX_DEMO_STATE_DIR/qwen-server.log" 2>&1 &
    qwen_pid=$!
    wait_http "$qwen_pid" "$qwen_url/v1/models" 90
fi
curl_local "$qwen_url/v1/models" | "$agent_python" -B -c \
    'import json, sys; sys.exit(0 if any("qwen3" in m["id"].lower() for m in json.load(sys.stdin)["data"]) else "Expected a Qwen3 model")'

if [[ -z ${EDGE_OPERATOR_TOKEN:-} ]]; then
    token_file="$CIX_DEMO_STATE_DIR/operator.token"
    if [[ ! -e $token_file ]]; then
        (set -o noclobber; "$agent_python" -B -c 'import secrets; print(secrets.token_urlsafe(32))' >"$token_file")
    fi
    [[ -f $token_file && ! -L $token_file && $(stat -c %a -- "$token_file") == 600 ]]
    IFS= read -r EDGE_OPERATOR_TOKEN <"$token_file"
fi
: "${EDGE_OPERATOR_TOKEN:?Operator credential is empty}"
export EDGE_OPERATOR_TOKEN

"$agent_python" -B -m uvicorn backend.app:app --host 127.0.0.1 --port "$port" --workers 1 \
    >>"$CIX_DEMO_STATE_DIR/backend.log" 2>&1 &
app_pid=$!
wait_http "$app_pid" "http://127.0.0.1:$port/health" 30
curl_local "http://127.0.0.1:$port/openapi.json" | "$agent_python" -B -c \
    'import json, sys; paths=json.load(sys.stdin)["paths"]; sys.exit(0 if "/api/v1/agent/runs" in paths and "/api/v1/depth/status" in paths else "Expected workbench and preview routes")'
printf 'FastAPI listening on 127.0.0.1:%s\n' "$port"
wait "$app_pid"
