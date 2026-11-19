#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
adapt="$(dirname "$here")"
deps=$(cd "${RLMEVAL_DEPS:-$adapt/../dependencies}" && pwd)
ctx="$(mktemp -d "${TMPDIR:-$deps}/rlmeval-image.XXXXXX")"
trap 'rm -rf -- "$ctx"' EXIT
vendor="${CODEX_VENDOR:-/usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl}"
claude="${CLAUDE_BIN:-/root/.nvm/versions/node/v24.22.0/bin/claude}"
test -x "$vendor/bin/codex"
test -x "$claude"
mkdir -p "$ctx/toolchains"
for repo in pfr tlb; do
  project="$deps/projects/$repo"
  toolchain=$(cat "$project/lean-toolchain")
  version=${toolchain//\//--}
  version=${version//:/---}
  toolchain_dir=$(realpath "$deps/elan/toolchains/$version")
  mkdir -p "$ctx/toolchains/$version"
  rsync -a --link-dest="$toolchain_dir" "$toolchain_dir/" "$ctx/toolchains/$version/"
  shared="$ctx/envs/$repo"
  mkdir -p "$shared/lib"
  cp "$project/lean-toolchain" "$shared/"
  for library in "$project"/.lake/packages/*/.lake/build/lib; do
    [[ -d "$library" ]] || continue
    rsync -aL --link-dest="$library" --include='*/' --include='*.olean' --exclude='*' "$library/" "$shared/lib/"
  done
done
mkdir -p "$ctx/wheels" "$ctx/codex"
if [[ -n "${WHEEL_DIR:-}" ]]; then
  cp "$WHEEL_DIR/"*.whl "$ctx/wheels/"
else
  "${PYTHON_BIN:-python3}" -m pip download \
    --python-version 3.12 --only-binary=:all: --dest "$ctx/wheels" \
    jsonlines PyYAML lean-interact==0.5.3 nltk rich
fi
cp -a "$vendor/bin" "$vendor/codex-path" "$ctx/codex/"
cp -L "$claude" "$ctx/claude"
cp "$here/Dockerfile" "$here/run-task.py" "$here/codex-container.sh" "$ctx/"
rsync -a --exclude=__pycache__ "$adapt/../RLMEval/src" "$ctx/"
docker build --network=none --progress=plain \
  -t "${RLMEVAL_DOCKER_IMAGE:-rlmeval-native:portable}" "$ctx"
