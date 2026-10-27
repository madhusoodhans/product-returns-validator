#!/bin/bash
# Starts one Qwen vLLM server (ROCm, Docker) for the returns validator demo. Usage: deploy/serve_qwen.sh <qwen27b|qwen9b>
#
#   qwen27b  Qwen/Qwen3.8-27B  127.0.0.1:8102  hard turns (disputes, several issues); image input on, so
#                                              deploy/measure.sh can run the all-27B baseline on the sample photos
#   qwen9b   Qwen/Qwen3.5-9B   127.0.0.1:8103  claim extraction and normal replies (text only)
#
# Thinking is off by default. The container joins the vllm-sr-network Docker network when it exists, so the Router
# reaches it by container name (prv-qwen27b / prv-qwen9b, as in deploy/router.yaml). Models are read from the Hugging
# Face cache, offline (run deploy/download_models.sh first).
#
# Environment:
#   DOCKER     docker command (default "docker"; set DOCKER="sudo docker" where Docker needs sudo)
#   HF_HOME    Hugging Face cache root (default $HOME/.cache/huggingface)
#   GPU_UTIL   vLLM --gpu-memory-utilization, a share of the whole GPU (default 0.62 for qwen27b, 0.13 for qwen9b)
#   GPU        which GPU to use (HIP_VISIBLE_DEVICES inside the container, default 0)
#   AITER      AMD AITER kernels, 1 or 0 (default 1)
#   CPUSET     optional --cpuset-cpus for the container, e.g. 0-7
#   IMAGE      vLLM image (default vllm/vllm-openai-rocm:v0.29.0, the version the demo was measured with)
set -euo pipefail
DOCKER="${DOCKER:-docker}"
HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
IMAGE="${IMAGE:-vllm/vllm-openai-rocm:v0.29.0}"
NETWORK=vllm-sr-network

case "${1:-}" in
  qwen27b) MODEL=Qwen/Qwen3.8-27B; PORT=8102; UTIL="${GPU_UTIL:-0.62}"; TEXT_ONLY=() ;;
  qwen9b)  MODEL=Qwen/Qwen3.5-9B;  PORT=8103; UTIL="${GPU_UTIL:-0.13}"; TEXT_ONLY=(--language-model-only) ;;
  *) echo "usage: $0 <qwen27b|qwen9b>" >&2; exit 2 ;;
esac
NAME="prv-$1"

[ -d "$HF_HOME/hub" ] || { echo "no Hugging Face cache at $HF_HOME/hub; run deploy/download_models.sh first" >&2; exit 1; }
NET=()
if $DOCKER network inspect "$NETWORK" >/dev/null 2>&1; then NET=(--network "$NETWORK"); fi
CPU=()
if [ -n "${CPUSET:-}" ]; then CPU=(--cpuset-cpus "$CPUSET"); fi

$DOCKER rm -f "$NAME" >/dev/null 2>&1 || true
$DOCKER run -d --name "$NAME" \
  --device /dev/kfd --device /dev/dri --group-add video --ipc host \
  --security-opt seccomp=unconfined --security-opt label=disable \
  "${CPU[@]}" "${NET[@]}" \
  -e HF_HUB_OFFLINE=1 -e VLLM_ROCM_USE_AITER="${AITER:-1}" -e HIP_VISIBLE_DEVICES="${GPU:-0}" \
  -v "$HF_HOME:/root/.cache/huggingface:ro" \
  -p "127.0.0.1:$PORT:8000" \
  "$IMAGE" "$MODEL" \
  --served-model-name "$1" --max-model-len 32768 --max-num-seqs 32 \
  --gpu-memory-utilization "$UTIL" --seed 0 "${TEXT_ONLY[@]}" \
  --reasoning-parser qwen3 \
  --default-chat-template-kwargs '{"enable_thinking": false}' >/dev/null
echo "started $NAME ($MODEL) on 127.0.0.1:$PORT, gpu-memory-utilization $UTIL${NET:+, on $NETWORK}"
