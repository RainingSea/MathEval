#!/usr/bin/env bash
set -euo pipefail

# 2026-10-31 隔离策略:run 结束后把产物挪出评测树,agent 在之后的运行中
# 无法读到上一轮的 generations/Candidate.lean(= 现成答案)。
# 用法: archive_run.sh RUN_ID [DEST_ROOT]
#   DEST_ROOT 默认 /data/projects/exp_results
# 产物去向: $DEST_ROOT/runs/RUN_ID/{artifacts,agent_workspaces,run-logs}

RUN_ID=${1:?usage: archive_run.sh RUN_ID [DEST_ROOT]}
DEST_ROOT=${2:-/data/projects/exp_results}
cd "$(dirname "$0")/.."

dest="$DEST_ROOT/runs/$RUN_ID"
mkdir -p "$dest"

moved=()
for name in artifacts agent_workspaces run-logs; do
  if [ -e "$name/$RUN_ID" ]; then
    if [ -e "$dest/$name" ]; then
      echo "WARNING: $dest/$name already exists; left $name/$RUN_ID in place" >&2
    else
      mv "$name/$RUN_ID" "$dest/$name"
      moved+=("$name")
    fi
  fi
done

if (( ${#moved[@]} == 0 )); then
  echo "nothing to archive for RUN_ID=$RUN_ID" >&2
else
  echo "archived RUN_ID=$RUN_ID -> $dest (${moved[*]})"
fi
