#!/usr/bin/env bash
set -euo pipefail
if (( $# < 2 )); then
  echo "Usage: $0 TASK_INPUT_DIR TASK_OUTPUT_DIR [run options...]" >&2
  exit 2
fi
input_dir=$(realpath -e -- "$1")
output_arg=$2
shift 2
if [[ ! -f "$input_dir/item.json" || ! -f "$input_dir/dependencies.json" ]]; then
  echo 'Input must be a single exported task directory.' >&2
  exit 2
fi
# A fresh leaf output mount prevents a task from seeing any previous results.
mkdir -- "$output_arg"
output_dir=$(realpath -e -- "$output_arg")
exec docker run --rm --init \
  --network "${ARCHON_DOCKER_NETWORK:-bridge}" \
  -e ARCHON_API_KEY -e ARCHON_API_BASE -e ANTHROPIC_BASE_URL -e ANTHROPIC_AUTH_TOKEN -e ANTHROPIC_API_KEY -e http_proxy -e https_proxy -e no_proxy \
  --mount "type=bind,src=$input_dir,dst=/input,readonly" \
  --mount "type=bind,src=$output_dir,dst=/outputs" \
  "${ARCHON_IMAGE:-rlmeval-archon:pfr}" run \
  --api-profile zhipu --max-iterations 3 --timeout 21600 \
  "$@" --input /input --output /outputs/result
