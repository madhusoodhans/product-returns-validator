# Shared settings for deploy/*.sh (sourced, not run).
DOCKER="${DOCKER:-docker}"
VLLM_SR="${VLLM_SR:-.venv-sr/bin/vllm-sr}"       # the vllm-sr CLI, pinned version, see README.md
case "$VLLM_SR" in /*) ;; */*) VLLM_SR="$PWD/$VLLM_SR" ;; esac   # absolute, since the Router is started from deploy/
GPU="${GPU:-0}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8602}"
ROUTER_URL="${ROUTER_URL:-http://localhost:8899}"
ROUTER_ADMIN_URL="${ROUTER_ADMIN_URL:-http://localhost:8080}"
export HOST PORT ROUTER_URL ROUTER_ADMIN_URL
STATE_FILE=deploy/.state                          # what up.sh started, so down.sh stops only that
ROUTER_CONTAINER=vllm-sr-router-container         # fixed by vllm-sr: one Router stack per host
# vllm-sr drives Docker itself, so where Docker needs sudo the CLI runs under sudo too.
SR=("$VLLM_SR")
case "$DOCKER" in sudo\ *) SR=(sudo -H env "VLLM_SR_AMD_ROUTER_VISIBLE_DEVICES=$GPU" ${HF_TOKEN:+"HF_TOKEN=$HF_TOKEN"} "$VLLM_SR") ;; esac
export VLLM_SR_AMD_ROUTER_VISIBLE_DEVICES="$GPU"

say() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
port_busy() { ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"; }
container_running() { [ "$($DOCKER inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
container_exists() { $DOCKER inspect "$1" >/dev/null 2>&1; }
# gpu_mem: "<total bytes> <free bytes>" of GPU $GPU, from rocm-smi.
gpu_mem() {
  rocm-smi -d "$GPU" --showmeminfo vram 2>/dev/null |
    awk -F: '/Total Memory/{t=$NF} /Total Used Memory/{u=$NF} END{gsub(/ /,"",t); gsub(/ /,"",u); if (t) print t, t-u}'
}
# need_free <bytes> <what>: stop with a clear message unless GPU $GPU has that much free memory now.
need_free() {
  local total free
  read -r total free < <(gpu_mem) || die "could not read GPU $GPU memory from rocm-smi"
  echo "GPU $GPU: $((free >> 30)) GiB free of $((total >> 30)) GiB; $2 needs about $(($1 >> 30)) GiB"
  [ "$free" -ge "$1" ] || die "not enough free GPU memory for $2. Stop other GPU work (rocm-smi shows what holds memory), \
or give the models smaller shares with GPU_UTIL_27B / GPU_UTIL_9B (less KV cache)."
}
# why_stopped <container> <util variable>: the first error lines of a container that died, plus advice for the known
# causes (the end of its log is usually a Python traceback that hides the cause).
why_stopped() {
  local box=$1 var=${2:-} log
  log=$($DOCKER logs "$box" 2>&1)
  echo "  $box stopped. First error lines of its log (full log: $DOCKER logs $box):"
  grep -E "Error|ERROR|error:|out of memory|not enough|less than desired" <<<"$log" | grep -v 'File "' |
    grep -v -E "^\s*\^" | head -8 | sed 's/^/    /'
  local hint=""
  if grep -qiE "No available memory for the cache blocks|available KV cache memory|max_model_len.*KV cache" <<<"$log"; then
    hint="its memory share is too small for the model plus its KV cache. Give it more, e.g. ${var:-GPU_UTIL}=<higher share>, or lower --max-model-len"
  elif grep -qiE "less than desired GPU memory utilization|Free memory on device" <<<"$log"; then
    hint="the GPU did not have the free memory this share asks for at start (another process holds it, or another model was still loading). Free GPU memory or lower ${var:-GPU_UTIL}"
  elif grep -qiE "out of memory|OutOfMemoryError|hipErrorOutOfMemory" <<<"$log"; then
    hint="the GPU ran out of memory. Free GPU memory (rocm-smi) or lower ${var:-GPU_UTIL}"
  elif grep -qiE "LocalEntryNotFoundError|Repository Not Found|does not appear to have a file|HF_HUB_OFFLINE" <<<"$log"; then
    hint="the model files are not in the Hugging Face cache the container reads. Run deploy/download_models.sh with the same HF_HOME"
  elif grep -qiE "No HIP GPUs|hipErrorNoDevice|/dev/kfd|Permission denied.*dri" <<<"$log"; then
    hint="the container cannot use the GPU. Check that Docker can pass /dev/kfd and /dev/dri (user in the video and render groups)"
  fi
  [ -n "$hint" ] && echo "  Likely cause: $hint."
  return 0
}
# wait_http <url> <seconds> [container] [util variable]: until the URL answers 200; gives up early if the container stops.
wait_http() {
  local url=$1 limit=$2 box=${3:-} var=${4:-} t=0
  until curl -sf -m 3 "$url" >/dev/null; do
    if [ -n "$box" ] && ! container_running "$box"; then
      why_stopped "$box" "$var"; return 1
    fi
    [ "$t" -ge "$limit" ] && { echo "  no answer from $url after $limit s"; return 1; }
    sleep 5; t=$((t + 5))
  done
}
