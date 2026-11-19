#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# 模型/端点/凭据统一走 profile(默认 api-gpt6astra,见 configs/profiles/)
export MODEL_PROFILE=${MODEL_PROFILE:-api-gpt6astra}
. scripts/model-env.sh
export MODEL
export CODEX_WRAPPER=${CODEX_WRAPPER:-run-logs/codex-gpt-custom}
export REASONING_EFFORT=${REASONING_EFFORT:-high}
export RUN_ID=${RUN_ID:-gpt-6astra-smoke-$(date +%Y%m%d-%H%M%S)}

exec bash scripts/codex-glm-api-smoke.sh
