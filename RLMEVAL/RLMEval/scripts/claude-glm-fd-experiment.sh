#!/usr/bin/env bash
set -u

# Claude Code harness × GLM-5.3 @ 反代平台(lhcodex.zeabur.app,Anthropic /v1/messages 原生)
# 与 claude-ds4pro-api-experiment.sh 同协议同评分,但模型侧不走本地 a2r:
# 反代平台原生兼容 Anthropic 协议(也天然支持 Responses 接口),claude CLI 直连,
# 真 key 经 --anthropic-api-key 传入;外网域名,claude 子进程保留环境代理(claude_runner 分流)。
cd "$(dirname "$0")/.."

: "${RUN_ID:?Set RUN_ID to a unique experiment identifier}"

MODEL=${MODEL:-glm-5.3}
TASK=${TASK:-proof}
INDICES=${INDICES:-168}
SAMPLES=${SAMPLES:-1}
CONCURRENCY=${CONCURRENCY:-4}
REASONING_EFFORT=${REASONING_EFFORT-high}   # 仅记录:claude CLI 无此 flag,GLM 自带思考
TMO=${TMO:-3600}
MAX_COMPILE_ATTEMPTS=${MAX_COMPILE_ATTEMPTS:-0}   # 0 = 不限次数(>0 才设闸)
DATASET=${DATASET:-data/rlm25_pfr_tlb.jsonl}
CLAUDE_BIN=${CLAUDE_BIN:-/root/.nvm/versions/node/v24.22.0/bin/claude}
SECRET_FILE=${SECRET_FILE:-secret.txt}
SECRET_SECTION=${SECRET_SECTION:-反代平台}
# 单响应输出上限:claude CLI 客户端默认 32000,GLM 长思考回合会被掐(tlb104 首批 9 题
# agent_error 全是它)。2026-10-31 用户拍板 131072(=65536×2,循 GLM 标准设置);
# 经 env 透传给 claude 子进程,不进 claude_runner(改动只影响本脚本)。
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-131072}
SUMMARY_DIR=${SUMMARY_DIR:-run-logs/$RUN_ID}
REBUILD_DATASET=${REBUILD_DATASET:-0}

export HARNESS_HOME=${HARNESS_HOME:-/data/projects/math-harness-eval/rlm-env/home}
export HOME="$HARNESS_HOME"
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-$HARNESS_HOME/.cache}
export ELAN_HOME=${ELAN_HOME:-/data/projects/math-harness-eval/elan}
export PATH="$ELAN_HOME/bin:$PATH"

# ---- 凭据:secret.txt 指定段(export OPENAI_API_KEY/BASE_URL='...'),全程不回显 ----
if [[ ! -f "$SECRET_FILE" ]]; then
  echo "Secret file not found: $SECRET_FILE" >&2; exit 2
fi
_picked=$(awk -v sec="$SECRET_SECTION" '
  $0 ~ "^## ?" sec { found = 1; next }
  /^## / { found = 0 }
  found { print }
' "$SECRET_FILE")
FD_API_KEY=$(sed -n "s/^export OPENAI_API_KEY='\(.*\)'/\1/p" <<<"$_picked")
_fd_url=$(sed -n "s/^export OPENAI_BASE_URL='\(.*\)'/\1/p" <<<"$_picked")
if [[ -z "$FD_API_KEY" || -z "$_fd_url" ]]; then
  echo "Section '$SECRET_SECTION' in $SECRET_FILE lacks OPENAI_API_KEY/OPENAI_BASE_URL" >&2; exit 2
fi
# 段里 URL 带 /v1(chat/responses 用);claude CLI 的 base 不带(它自己拼 /v1/messages)。
# 注意:宿主环境可能 export 过自己的 ANTHROPIC_BASE_URL(如 bigmodel 接线),
# 因此这里不用该名字做变量,一律以段内 URL 为准,防环境值抢占 default-assign。
FD_BASE_URL=${FD_BASE_URL:-${_fd_url%/v1}}
FD_API_KEY_EXPORT=$FD_API_KEY
_key_fp=$(printf %s "$FD_API_KEY" | md5sum | cut -c1-8)

# ---- 预检:直连端点打一发最小请求,401/404/模型名错当场暴露,别等 1 小时后 ----
_http=$(curl -s -m 60 -o /tmp/fd-preflight-$$.json -w '%{http_code}' \
  "$FD_BASE_URL/v1/messages" \
  -H "x-api-key: $FD_API_KEY_EXPORT" -H "anthropic-version: 2023-06-01" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: ok\"}]}")
if [[ "$_http" != 2* ]]; then
  echo "Preflight FAILED: POST $FD_BASE_URL/v1/messages -> HTTP $_http" >&2
  head -c 400 /tmp/fd-preflight-$$.json >&2; echo >&2
  rm -f /tmp/fd-preflight-$$.json
  exit 5
fi
rm -f /tmp/fd-preflight-$$.json
echo "preflight ok: $FD_BASE_URL/v1/messages model=$MODEL key(md5:$_key_fp)"

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
HARNESS=claude-code-fd
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
ANTHROPIC_BASE_URL=$FD_BASE_URL
SECRET_SECTION=$SECRET_SECTION
ANTHROPIC_API_KEY_MD5=$_key_fp
CLAUDE_CODE_MAX_OUTPUT_TOKENS=$CLAUDE_CODE_MAX_OUTPUT_TOKENS
EOF

echo "[$(date -Is)] generation start (claude-code harness, 反代直连)"
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
  --anthropic-base-url "$FD_BASE_URL" \
  --anthropic-api-key "$FD_API_KEY_EXPORT" \
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
