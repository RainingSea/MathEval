#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
deps=${1:-"$here/../../dependencies"}
mkdir -p "$deps/projects"
deps=$(cd "$deps" && pwd)
export ELAN_HOME="$deps/elan"
mkdir -p "$ELAN_HOME/bin"
# Install elan first; copy its multicall launcher so toolchains live in this dependency root.
for cmd in elan lean lake; do
  [[ -x "$ELAN_HOME/bin/$cmd" ]] || cp -L "$(command -v elan)" "$ELAN_HOME/bin/$cmd"
done
export PATH="$ELAN_HOME/bin:$PATH"
for repo in pfr tlb; do
  project="$deps/projects/$repo"
  if [[ -e "$project" ]]; then echo "Use a fresh dependency directory: $project" >&2; exit 2; fi
  cp -a "$here/sources/$repo" "$project"
  (cd "$project" && elan toolchain install "$(cat lean-toolchain)" && lake exe cache get && lake build)
done
