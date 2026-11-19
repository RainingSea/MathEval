#!/usr/bin/env bash
set -euo pipefail

# claude-code 轨道的后台启动器,与 codex-glm-api-background.sh 同款:
# setsid+nohup 脱离终端,PID/日志/配置落 run-logs/<RUN_ID>/。
cd "$(dirname "$0")/.."

: "${RUN_ID:?Set RUN_ID before starting a background experiment}"

LOG_DIR=${SUMMARY_DIR:-run-logs/$RUN_ID}
mkdir -p "$LOG_DIR"

if [[ -f "$LOG_DIR/background.pid" ]]; then
  old_pid=$(cat "$LOG_DIR/background.pid")
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "A background process is already running for $RUN_ID: PID $old_pid" >&2
    exit 2
  fi
fi
if [[ -e "artifacts/$RUN_ID/generations.jsonl" ]]; then
  echo "Run ID already exists: artifacts/$RUN_ID/generations.jsonl" >&2
  exit 3
fi

export RUN_ID
export MODEL=${MODEL:-deepseek-v4-pro-0813}
export TASK=${TASK:-proof}
export INDICES=${INDICES:-168}
export SAMPLES=${SAMPLES:-1}
export CONCURRENCY=${CONCURRENCY:-4}
export REASONING_EFFORT=${REASONING_EFFORT-high}
export TMO=${TMO:-3600}
export MAX_COMPILE_ATTEMPTS=${MAX_COMPILE_ATTEMPTS:-0}
export DATASET=${DATASET:-data/rlm25_pfr_tlb.jsonl}
export CLAUDE_BIN=${CLAUDE_BIN:-/root/.nvm/versions/node/v24.22.0/bin/claude}
export A2R_BASE_URL=${A2R_BASE_URL:-http://127.0.0.1:8102}
export SUMMARY_DIR=$LOG_DIR

setsid nohup bash scripts/claude-ds4pro-api-experiment.sh > "$LOG_DIR/background.log" 2>&1 < /dev/null &
bg_pid=$!
echo "$bg_pid" > "$LOG_DIR/background.pid"
echo "background_pid=$bg_pid"
echo "log=$LOG_DIR/background.log"
echo "summary=$LOG_DIR/summary.md"
