#!/usr/bin/env bash
set -euo pipefail

adapt="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
root="$(cd -- "$adapt/../.." && pwd -P)"
upstream="$root/FormalTCS"
local_root="$adapt/.local"
venv="$local_root/venv"
python="${FORMALTCS_PYTHON:-python3}"
expected="$(<"$adapt/upstream.rev")"

die() {
  printf 'FormalTCS: %s\n' "$*" >&2
  exit 1
}

"$python" -I -B -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die 'Python 3.10+ is required.'
[[ -f "$upstream/benchmark/collection.json" && -f "$upstream/evaluate/prompts/c2np.md" ]] || \
  die 'Missing vendored FormalTCS files; restore them from the MathEval checkout.'

if [[ ! -x "$venv/bin/python" ]]; then
  "$python" -I -B -m venv --without-pip "$venv"
fi
printf 'FormalTCS source: %s (upstream snapshot %s)\n' "$upstream" "$expected"
printf 'Python environment: %s (%s)\n' "$venv" "$("$venv/bin/python" -I -B --version)"
