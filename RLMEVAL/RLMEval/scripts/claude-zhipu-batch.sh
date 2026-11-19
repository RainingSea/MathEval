#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
: "${RUN_ID:?}" "${INDICES:?}" "${SECRET_SECTION:?}"
PYTHON=/root/miniconda3/envs/rlmeval/bin/python
LOG_DIR="run-logs/$RUN_ID"
extra=()
if [[ "${REVERSE_ORDER:-0}" == 1 ]]; then
  extra+=(--reverse-order)
fi
echo "[$(date -Is)] Starting run=$RUN_ID model=glm-5.3 concurrency=4 timeout=43200 compile_attempts=unlimited"
"$PYTHON" -u scripts/claude_zhipu_experiment.py \
  --secret-section "$SECRET_SECTION" --run --model glm-5.3 --run-id "$RUN_ID" \
  --indices "$INDICES" --concurrency 4 --timeout-seconds 43200 --smoke-timeout 90 "${extra[@]}"
run_status=$?
summary_status=0
integrity_status=0
if [[ -f "artifacts/$RUN_ID/generations.jsonl" ]]; then
  "$PYTHON" scripts/summarize_codex_run.py --run-id "$RUN_ID" \
    --dataset data/rlm25_pfr_tlb.jsonl --output-dir "$LOG_DIR" \
    --expected-samples 1 || summary_status=$?
  "$PYTHON" scripts/scan_integrity.py --run-id "$RUN_ID" \
    --dataset data/rlm25_pfr_tlb.jsonl --output "$LOG_DIR/integrity.json" || integrity_status=$?
fi
printf 'run_status=%s\nsummary_status=%s\nintegrity_status=%s\nfinished_at=%s\n' \
  "$run_status" "$summary_status" "$integrity_status" "$(date -Is)" > "$LOG_DIR/exit-status.txt"
echo "[$(date -Is)] Finished: run=$run_status summary=$summary_status integrity=$integrity_status"
if [[ -f "artifacts/$RUN_ID/generations.jsonl" ]]; then
  bash scripts/archive_run.sh "$RUN_ID" || echo 'WARNING: archive failed' >&2
fi
exit "$run_status"
