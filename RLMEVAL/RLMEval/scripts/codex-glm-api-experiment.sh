#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."

: "${RUN_ID:?Set RUN_ID to a unique experiment identifier}"

# 模型/端点/凭据统一走 profile(见 scripts/model-env.sh 与 configs/profiles/);
# MODEL 等仍可环境变量覆盖,profile 只提供默认值
export MODEL_PROFILE=${MODEL_PROFILE:-api-glm53}
. scripts/model-env.sh
MODEL=${MODEL:-glm-5.3}
TASK=${TASK:-proof}
INDICES=${INDICES:-168,41,38}
SAMPLES=${SAMPLES:-1}
CONCURRENCY=${CONCURRENCY:-4}
REASONING_EFFORT=${REASONING_EFFORT-high}
TMO=${TMO:-2400}
MAX_COMPILE_ATTEMPTS=${MAX_COMPILE_ATTEMPTS:-0}   # 0 = 不限次数(>0 才设闸)
DATASET=${DATASET:-data/rlm25_pfr_tlb.jsonl}
CODEX_WRAPPER=${CODEX_WRAPPER:-run-logs/codex-gpt-custom}
SECRET_FILE=${SECRET_FILE:-secret.txt}
SOURCE_SECRET=${SOURCE_SECRET:-1}
REBUILD_DATASET=${REBUILD_DATASET:-0}
SUMMARY_DIR=${SUMMARY_DIR:-run-logs/$RUN_ID}

export HARNESS_HOME=${HARNESS_HOME:-/data/projects/math-harness-eval/rlm-env/home}
export HOME="$HARNESS_HOME"
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-$HARNESS_HOME/.cache}
export ELAN_HOME=${ELAN_HOME:-/data/projects/math-harness-eval/elan}
export CODEX_BIN=${CODEX_BIN:-/usr/local/bin/codex}
export PATH="$ELAN_HOME/bin:$PATH"

# profile 已提供凭据(OPENAI_API_KEY 非空)时跳过整文件 source,
# 避免 secret.txt 两个中转段的 export 覆盖 profile 选定的段
if [[ "$SOURCE_SECRET" == "1" && -z "${OPENAI_API_KEY:-}" ]]; then
  if [[ ! -f "$SECRET_FILE" ]]; then
    echo "Secret file not found: $SECRET_FILE" >&2
    exit 2
  fi
  set -a
  source "$SECRET_FILE"
  set +a
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
MODEL=$MODEL
MODEL_PROFILE=$MODEL_PROFILE
TASK=$TASK
INDICES=$INDICES
SAMPLES=$SAMPLES
CONCURRENCY=$CONCURRENCY
REASONING_EFFORT=$REASONING_EFFORT
TMO=$TMO
MAX_COMPILE_ATTEMPTS=$MAX_COMPILE_ATTEMPTS
DATASET=$DATASET
CODEX_WRAPPER=$CODEX_WRAPPER
SECRET_FILE=$SECRET_FILE
EOF

echo "[$(date -Is)] generation start"
echo "run_id=$RUN_ID model=$MODEL task=$TASK samples=$SAMPLES concurrency=$CONCURRENCY"
echo "indices=$INDICES"

generation_status=0
./rlmv-eval run-codex \
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
  --codex-bin "$CODEX_WRAPPER" \
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
