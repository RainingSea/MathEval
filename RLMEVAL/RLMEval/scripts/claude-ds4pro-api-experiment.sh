#!/usr/bin/env bash
set -u

# Claude Code harness × DS(deepseek-v4-pro-0813 @ tensoris via apiAdapt/a2r.py)
# 与 codex 轨道同协议同评分:TASK.md/Context.lean/Candidate.lean + check_candidate.sh,
# 只是 agent 后端换成 claude CLI(headless -p),模型侧经本地 a2r(Anthropic↔Chat)中转。
cd "$(dirname "$0")/.."

: "${RUN_ID:?Set RUN_ID to a unique experiment identifier}"

MODEL=${MODEL:-deepseek-v4-pro-0813}
TASK=${TASK:-proof}
INDICES=${INDICES:-168}
SAMPLES=${SAMPLES:-1}
CONCURRENCY=${CONCURRENCY:-4}
REASONING_EFFORT=${REASONING_EFFORT-high}
TMO=${TMO:-3600}
MAX_COMPILE_ATTEMPTS=${MAX_COMPILE_ATTEMPTS:-0}   # 0 = 不限次数(>0 才设闸)
DATASET=${DATASET:-data/rlm25_pfr_tlb.jsonl}
CLAUDE_BIN=${CLAUDE_BIN:-/root/.nvm/versions/node/v24.22.0/bin/claude}
A2R_BASE_URL=${A2R_BASE_URL:-http://127.0.0.1:8102}
A2R_HOME=${A2R_HOME:-/data/projects/math-harness-eval/apiAdapt}
SUMMARY_DIR=${SUMMARY_DIR:-run-logs/$RUN_ID}
REBUILD_DATASET=${REBUILD_DATASET:-0}

export HARNESS_HOME=${HARNESS_HOME:-/data/projects/math-harness-eval/rlm-env/home}
export HOME="$HARNESS_HOME"
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-$HARNESS_HOME/.cache}
export ELAN_HOME=${ELAN_HOME:-/data/projects/math-harness-eval/elan}
export PATH="$ELAN_HOME/bin:$PATH"

# ---- a2r 活体检查;不在则自动拉起(与 c2r 同款脱离式启动) ----
if ! curl -s --noproxy '*' -m 5 -o /dev/null "$A2R_BASE_URL/health"; then
  port=$(printf '%s' "$A2R_BASE_URL" | sed 's|.*:\([0-9]*\)$|\1|')
  echo "a2r not alive on $A2R_BASE_URL; starting apiAdapt/a2r.py on :$port"
  ( cd "$A2R_HOME/.." && setsid --fork nohup python3 apiAdapt/a2r.py \
      --host 127.0.0.1 --port "$port" --model "$MODEL" \
      </dev/null >>/tmp/a2r-$port.log 2>&1 )
  sleep 1.5
  curl -s --noproxy '*' -m 5 "$A2R_BASE_URL/health" || { echo "a2r failed to start (see /tmp/a2r-$port.log)" >&2; exit 5; }
fi

if [[ "$REBUILD_DATASET" == "1" || ! -f "$DATASET" ]]; then
  ./rlmv-eval items \
    --config configs/benchmark/pfr_tlb.yaml \
    --out "$DATASET" \
    --project-map "PFR=/data/projects/math-harness-eval/rlm-env/pfr,TLB=/data/projects/math-harness-eval/rlm-env/tlb"
fi

if [[ -e "artifacts/$RUN_ID/generations.jsonl" ]]; then
  echo "Run ID already exists: artifacts/$RUN_ID/generations.jsonl" >&2
  exit 3
fi

mkdir -p "$SUMMARY_DIR"
cat > "$SUMMARY_DIR/config.txt" <<EOF
RUN_ID=$RUN_ID
HARNESS=claude-code
MODEL=$MODEL
TASK=$TASK
INDICES=$INDICES
SAMPLES=$SAMPLES
CONCURRENCY=$CONCURRENCY
REASONING_EFFORT=$REASONING_EFFORT
TMO=$TMO
MAX_COMPILE_ATTEMPTS=$MAX_COMPILE_ATTEMPTS
DATASET=$DATASET
CLAUDE_BIN=$CLAUDE_BIN
A2R_BASE_URL=$A2R_BASE_URL
EOF

echo "[$(date -Is)] generation start (claude-code harness)"
echo "run_id=$RUN_ID model=$MODEL task=$TASK samples=$SAMPLES concurrency=$CONCURRENCY"
echo "indices=$INDICES"

generation_status=0
./rlmv-eval run-claude \
  --model "$MODEL" \
  --dataset "$DATASET" \
  --indices "$INDICES" \
  --samples "$SAMPLES" \
  --concurrency "$CONCURRENCY" \
  --timeout-seconds "$TMO" \
  --max-compile-attempts "$MAX_COMPILE_ATTEMPTS" \
  --reasoning-effort "$REASONING_EFFORT" \
  --task "$TASK" \
  --artifacts-dir artifacts \
  --workspaces-dir agent_workspaces \
  --claude-bin "$CLAUDE_BIN" \
  --anthropic-base-url "$A2R_BASE_URL" \
  --elan-home "$ELAN_HOME" \
  --cache-dir .rlm-cache \
  --run-id "$RUN_ID" || generation_status=$?

score_status=0
if [[ -f "artifacts/$RUN_ID/generations.jsonl" ]]; then
  echo "[$(date -Is)] scoring start"
  ./rlmv-eval score \
    --dataset "$DATASET" \
    --input "artifacts/$RUN_ID/generations.jsonl" \
    --output "artifacts/$RUN_ID/scores.jsonl" \
    --task "$TASK" || score_status=$?
else
  score_status=4
  echo "No generations.jsonl was produced; scoring skipped" >&2
fi

summary_status=0
/root/miniconda3/envs/rlmeval/bin/python scripts/summarize_codex_run.py \
  --run-id "$RUN_ID" \
  --dataset "$DATASET" \
  --output-dir "$SUMMARY_DIR" \
  --expected-samples "$SAMPLES" || summary_status=$?

# ---- 2026-10-31 隔离策略:完整性扫描 + 产物归档出评测树 ----
# 参考答案源码与历史产物已物理隔离到 /data/projects/exp_results(见 EXPERIMENT_GUIDE §18)。
# 跑完立即扫残留通道(Context.lean 被改 / #print / #eval)并把产物挪出,
# 避免下一场运行读到本场答案。此步失败只告警,不影响本次 run 的退出码。
if [[ -f "artifacts/$RUN_ID/generations.jsonl" ]]; then
  /root/miniconda3/envs/rlmeval/bin/python scripts/scan_integrity.py \
    --run-id "$RUN_ID" \
    --dataset "$DATASET" \
    --artifacts-dir artifacts \
    --workspaces-dir agent_workspaces \
    --output "$SUMMARY_DIR/integrity.json" || echo "WARN: integrity scan failed (continuing)" >&2
  bash scripts/archive_run.sh "$RUN_ID" || echo "WARN: archive failed; artifacts remain in tree - archive manually" >&2
else
  echo "no generations.jsonl; skip integrity scan & archive" >&2
fi

echo "[$(date -Is)] experiment complete generation=$generation_status score=$score_status summary=$summary_status"
if (( generation_status != 0 )); then
  exit "$generation_status"
fi
if (( score_status != 0 )); then
  exit "$score_status"
fi
exit "$summary_status"
