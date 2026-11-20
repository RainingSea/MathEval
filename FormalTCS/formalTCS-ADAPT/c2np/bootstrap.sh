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
if [[ ! -e "$upstream" && ! -L "$upstream" ]]; then
  checkout="$(mktemp -d "$root/.upstream-XXXXXX")"
  trap 'rm -rf -- "$checkout"' EXIT
  git clone --no-checkout https://github.com/zirui-HIT/FormalTCS.git "$checkout/FormalTCS"
  git -C "$checkout/FormalTCS" checkout --detach "$expected"
  mv -T -- "$checkout/FormalTCS" "$upstream"
fi
[[ -e "$upstream/.git" ]] || die 'Expected an upstream FormalTCS checkout; see README.md.'
actual="$(git -C "$upstream" rev-parse HEAD)"
[[ "$actual" == "$expected" ]] || die "Expected upstream $expected, got $actual; see README.md."
changes="$(git -C "$upstream" status --porcelain --untracked-files=normal)"
[[ -z "$changes" ]] || die 'Upstream checkout is not clean; the bootstrap will not modify it.'

mkdir -p -- "$local_root/tmp"
export TMPDIR="$local_root/tmp"
if [[ ! -x "$venv/bin/python" ]]; then
  "$python" -I -B -m venv --without-pip "$venv"
fi
"$venv/bin/python" -I -B "$adapt/smoke_c2np.py" "$upstream" "$local_root" "$actual"
