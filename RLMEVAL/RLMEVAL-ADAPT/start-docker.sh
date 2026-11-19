#!/usr/bin/env bash
set -euo pipefail

# 默认配置：直接编辑右侧的值。启动时传入的非空同名环境变量优先。
declare -A defaults=(
  # 模型配置：选择 codex 或 claude
  [BACKEND]="codex"
  [MODEL]="deepseek-v4-pro-0813"
  [OPENAI_BASE_URL]="https://api.tensoris.ai/v1"
  [OPENAI_API_KEY]=""
  [ANTHROPIC_BASE_URL]="https://api.tensoris.ai"
  [ANTHROPIC_AUTH_TOKEN]=""
  [ANTHROPIC_API_KEY]=""

  # 题号、每题采样数和并发题数，题号直接写all表示跑全部，否则就是1,2,3或者1-3
  [INDICES]="200"
  [SAMPLES]=1
  [CONCURRENCY]=1
  [TMO]=21600                         # 每个样本的超时秒数
  [CODEX_MAX_OUTPUT_TOKENS]=384000       # Codex 每次 Responses 请求的 max_output_tokens
  [CLAUDE_CODE_MAX_OUTPUT_TOKENS]=384000  # 仅 Claude Code，每次模型回复的 token 上限
)

for setting in "${!defaults[@]}"; do
  value=${!setting-}
  export "$setting=${value:-${defaults[$setting]}}"
done
unset value setting defaults
credential_file=${TENSORIS_CREDENTIAL_FILE:-/root/.config/archon/tensoris.env.json}

adapt_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
run_docker() {
  export RLMEVAL_DEPS="${RLMEVAL_DEPS:-$adapt_dir/../dependencies}"
  export DATASET="${DATASET:-$RLMEVAL_DEPS/dataset.jsonl}"
  export ELAN_HOME="${ELAN_HOME:-$RLMEVAL_DEPS/elan}"
  export PATH="$ELAN_HOME/bin:$PATH"
  export PYTHONPATH="$adapt_dir/../RLMEval/src"
  python_default=python3
  [[ ! -x /root/miniconda3/envs/rlmeval/bin/python ]] || python_default=/root/miniconda3/envs/rlmeval/bin/python
  exec "${PYTHON_BIN:-$python_default}" "$adapt_dir/docker/run-batch.py" "$@"
}

case "${1:-}" in
  --help|-h|--prepare-only) run_docker "$@" ;;
esac

read_tensoris_key() {
  if [[ "$1" != "$2" ]]; then
    echo "Set a credential for the configured API URL in the host environment." >&2
    return 2
  fi
  python3 - "$credential_file" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
try:
    key = json.loads(path.read_text(encoding="utf-8"))["OPENAI_API_KEY"]
    if not isinstance(key, str) or not key.strip():
        raise ValueError
except (OSError, ValueError, KeyError, TypeError):
    sys.exit(f"Cannot read OPENAI_API_KEY from {path}")
print(key)
PY
}

case "$BACKEND" in
  codex)
    OPENAI_API_KEY="${OPENAI_API_KEY:-$(read_tensoris_key "$OPENAI_BASE_URL" "https://api.tensoris.ai/v1")}"
    export OPENAI_API_KEY
    ;;
  claude)
    if [[ -z "${ANTHROPIC_AUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ]]; then
      ANTHROPIC_AUTH_TOKEN="$(read_tensoris_key "$ANTHROPIC_BASE_URL" "https://api.tensoris.ai")"
      export ANTHROPIC_AUTH_TOKEN
    fi
    ;;
  *)
    echo "BACKEND must be codex or claude" >&2
    exit 2
    ;;
esac

run_docker "$@"
