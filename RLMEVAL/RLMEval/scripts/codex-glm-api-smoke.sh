#!/usr/bin/env bash
# harness 轨道三题 smoke:codex agent + glm-5.3 @ 内部中转(默认 api-glm53)。
# 也是 codex-gpt-api-smoke.sh 的执行体(经环境变量覆盖模型/wrapper)。
set -euo pipefail
cd "$(dirname "$0")/.."
export HOME=/data/projects/math-harness-eval/rlm-env/home
export XDG_CACHE_HOME=$HOME/.cache
export ELAN_HOME=${ELAN_HOME:-/data/projects/math-harness-eval/elan}
export PATH="$ELAN_HOME/bin:$PATH"
# 模型/端点/凭据统一走 profile(见 scripts/model-env.sh);覆盖示例见 codex-gpt-api-smoke.sh
export MODEL_PROFILE=${MODEL_PROFILE:-api-glm53}
. scripts/model-env.sh
RUN_ID=${RUN_ID:-glm53-codex-smoke-$(date +%Y%m%d-%H%M%S)}
TASK=${TASK:-statement}
MODEL=${MODEL:-glm-5.3}
CODEX_WRAPPER=${CODEX_WRAPPER:-run-logs/codex-gpt-custom}
INDICES=${INDICES:-1,2,3}
REASONING_EFFORT=${REASONING_EFFORT:-xhigh}
CONCURRENCY=${CONCURRENCY:-3}
SAMPLES=${SAMPLES:-1}

./rlmv-eval items \
  --config configs/benchmark/pfr_tlb.yaml \
  --out data/rlm25_pfr_tlb.jsonl \
  --project-map "PFR=/data/projects/math-harness-eval/rlm-env/pfr,TLB=/data/projects/math-harness-eval/rlm-env/tlb"

echo "[$(date -Is)] generation start: $RUN_ID task=$TASK indices=$INDICES"
./rlmv-eval run-codex \
  --model "$MODEL" \
  --dataset data/rlm25_pfr_tlb.jsonl \
  --indices "$INDICES" \
  --samples "$SAMPLES" --concurrency "$CONCURRENCY" --timeout-seconds ${TMO:-1500} --max-compile-attempts ${MAX_COMPILE_ATTEMPTS:-0} \
  --reasoning-effort "$REASONING_EFFORT" \
  --task "$TASK" \
  --artifacts-dir artifacts \
  --workspaces-dir agent_workspaces \
  --codex-bin "$CODEX_WRAPPER" \
  --elan-home "$ELAN_HOME" \
  --cache-dir .rlm-cache \
  --run-id "$RUN_ID"
echo "[$(date -Is)] scoring start"
./rlmv-eval score \
  --dataset data/rlm25_pfr_tlb.jsonl \
  --input "artifacts/$RUN_ID/generations.jsonl" \
  --output "artifacts/$RUN_ID/scores.jsonl" \
  --task "$TASK"
echo "[$(date -Is)] smoke complete: artifacts/$RUN_ID"
