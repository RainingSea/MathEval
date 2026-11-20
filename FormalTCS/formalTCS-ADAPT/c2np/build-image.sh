#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-formaltcs-c2np:claude-2.1.323}"

if (( $# != 0 )); then
    printf 'Usage: bash %s\n' "${0##*/}" >&2
    exit 2
fi

ADAPT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$ADAPT/../.." && pwd -P)"
export DOCKER_BUILDKIT=1
exec docker build --file "$ADAPT/Dockerfile" --tag "$IMAGE" "$ROOT"
