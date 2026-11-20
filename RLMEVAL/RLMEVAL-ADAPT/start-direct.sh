#!/usr/bin/env bash
set -euo pipefail
adapt=$(cd "$(dirname "$0")" && pwd)
repo="$adapt/../RLMEval"

# 宿主机裸模型，支持非空同名环境变量覆盖。
declare -A defaults=(
  [MODEL]="deepseek-v4.1-flash"
  [OPENAI_BASE_URL]="https://token.pjlab.org.cn/v1"
  [OPENAI_API_KEY]=""
  [INDICES]="all"
  [SAMPLES]=1                 # 每题采样次数 k
  [CONCURRENCY]=1             # 同时生成的样本数
  [TMO]=21600                 # 单次 API 请求的超时秒数
  [MAX_OUTPUT_TOKENS]=131072
  [MAX_CONTEXT_TOKENS]=1048576
  [REASONING_EFFORT]=""
  [RLMEVAL_DEPS]="$adapt/../dependencies"
  [RUNS_DIR]="$adapt/runs"
)
for setting in "${!defaults[@]}"; do
  value=${!setting-}
  export "$setting=${value:-${defaults[$setting]}}"
done

python=${PYTHON_BIN:-python3}
if [[ -z ${PYTHON_BIN:-} && -z ${VIRTUAL_ENV:-} && -z ${CONDA_PREFIX:-} && -x /root/miniconda3/envs/rlmeval/bin/python ]]; then
  python=/root/miniconda3/envs/rlmeval/bin/python
fi
dataset=${DATASET:-"$RLMEVAL_DEPS/dataset.jsonl"}
export ELAN_HOME=${ELAN_HOME:-"$RLMEVAL_DEPS/elan"}
export PATH="$ELAN_HOME/bin:$PATH"
export RLMEVAL_PYTHON="$python"
: "${OPENAI_API_KEY:?请设置 OPENAI_API_KEY}"
# 和 Docker 评测使用同一组可用题号（仅包含发布的 proof 题）。
indices=$(PYTHONPATH="$repo/src" "$python" - "$adapt/build/manifests" "$INDICES" <<'PY'
from pathlib import Path
import sys
from rlm_agent.items import parse_indices
available = [int(p.parent.name) for p in Path(sys.argv[1]).glob("*/tasks/*/*/dependencies.json")]
chosen = parse_indices(sys.argv[2], available)
if not chosen:
    sys.exit("Select at least one task")
print(",".join(map(str, chosen)))
PY
)
mkdir -p "$RUNS_DIR"
if [[ -n ${RUN_ID:-} ]]; then
  [[ "$RUN_ID" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || { echo "Invalid RUN_ID" >&2; exit 2; }
  run="$RUNS_DIR/$RUN_ID"
  mkdir "$run"
else
  run=$(mktemp -d "$RUNS_DIR/direct-XXXXXXXX")
fi
echo "Run directory $run"
"$repo/rlmv-eval" run-direct --dataset "$dataset" --indices "$indices" --task proof \
  --model "$MODEL" --base-url "$OPENAI_BASE_URL" --api-key-env OPENAI_API_KEY \
  --samples "$SAMPLES" --concurrency "$CONCURRENCY" --timeout-seconds "$TMO" \
  --max-tokens "$MAX_OUTPUT_TOKENS" --max-context-tokens "$MAX_CONTEXT_TOKENS" \
  --reasoning-effort "$REASONING_EFFORT" --run-id "${run##*/}" \
  --artifacts-dir "$RUNS_DIR" --workspaces-dir "$RUNS_DIR"
"$repo/rlmv-eval" score --dataset "$dataset" --task proof \
  --input "$run/generations.jsonl" --output "$run/scores.jsonl"
"$python" "$repo/scripts/summarize_codex_run.py" --dataset "$dataset" \
  --artifacts-dir "$RUNS_DIR" --run-id "${run##*/}" --expected-samples "$SAMPLES" --output-dir "$run"
