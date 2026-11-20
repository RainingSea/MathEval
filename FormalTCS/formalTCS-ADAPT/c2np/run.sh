#!/usr/bin/env bash
set -euo pipefail

# 默认配置：编辑右侧的值即可；同名非空环境变量优先。密钥请从宿主机环境传入。
declare -A defaults=(
    # 生成模型：目前只支持 Claude Code，接口必须兼容 Anthropic Messages。
    [BACKEND]="claude"
    [MODEL]="deepseek-v4.1-flash-1017"
    [ANTHROPIC_BASE_URL]="https://discovery-api.intern-ai.org.cn"
    [ANTHROPIC_AUTH_TOKEN]=""
    [CLAUDE_CODE_MAX_OUTPUT_TOKENS]=""

    # 题号从 1 开始，使用题库原始顺序：例如 1,3-5 或 all。也可用 ITEMS 指定实际题目 ID。
    [INDICES]="1"
    [ITEMS]=""
    [SAMPLES]="1"
    [CONCURRENCY]="1"
    [TMO]="43200"

    # 裁判：接口必须兼容 OpenAI Responses，密钥可与生成侧不同。
    [JUDGE_MODEL]="gpt-6.1-sol"
    [JUDGE_EFFORT]="high"
    [JUDGE_BASE_URL]="https://api.tensoris.ai/v1"
    [JUDGE_API_KEY]=""

    [IMAGE]="formaltcs-c2np:claude-2.1.323"
    [FORMALTCS_PYTHON]="/usr/bin/python3"
)

for setting in "${!defaults[@]}"; do
    value=${!setting-}
    export "$setting=${value:-${defaults[$setting]}}"
done
unset setting value defaults
if [[ -z "$CLAUDE_CODE_MAX_OUTPUT_TOKENS" ]]; then unset CLAUDE_CODE_MAX_OUTPUT_TOKENS; fi

ADAPT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

if (( $# != 0 )); then
    printf 'Usage: bash %s\n' "${0##*/}" >&2
    exit 2
fi

[[ "$BACKEND" == claude ]] || { printf 'BACKEND must be claude; Codex is not implemented.\n' >&2; exit 2; }
if ! docker image inspect "$IMAGE" >/dev/null; then
    printf 'Cannot use image %s. To build it, run: IMAGE=%q bash %q\n' "$IMAGE" "$IMAGE" "$ADAPT/build-image.sh" >&2
    exit 1
fi
: "${ANTHROPIC_AUTH_TOKEN:?Set ANTHROPIC_AUTH_TOKEN in the host environment}"
: "${JUDGE_API_KEY:?Set JUDGE_API_KEY in the host environment}"

args=(--image "$IMAGE" --model "$MODEL" --workers "$CONCURRENCY" --samples "$SAMPLES" --timeout "$TMO"
      --judge-model "$JUDGE_MODEL" --judge-effort "$JUDGE_EFFORT")
if [[ -n "$ITEMS" ]]; then args+=(--items "$ITEMS"); else args+=(--indices "$INDICES"); fi
batch_dir="$("$FORMALTCS_PYTHON" -B "$ADAPT/batch.py" generate "${args[@]}")"
exec "$FORMALTCS_PYTHON" -B "$ADAPT/batch.py" grade "$batch_dir"
