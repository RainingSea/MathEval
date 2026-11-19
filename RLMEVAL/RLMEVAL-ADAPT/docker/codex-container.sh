#!/usr/bin/env bash
set -euo pipefail

args=("$@")
for ((index = 1; index + 1 < ${#args[@]}; index++)); do
  if [[ "${args[index]}" == --sandbox && "${args[index+1]}" == workspace-write ]]; then
    # The unprivileged task container supplies the filesystem and network boundary.
    args[index+1]=danger-full-access
    exec "${CODEX_BIN:-/usr/local/bin/codex}" exec \
      --config 'model_provider="rlmeval"' \
      --config 'model_providers.rlmeval.name="RLMEval Responses"' \
      --config "model_providers.rlmeval.base_url=\"${OPENAI_BASE_URL%/}\"" \
      --config 'model_providers.rlmeval.env_key="OPENAI_API_KEY"' \
      --config 'model_providers.rlmeval.wire_api="responses"' \
      --config 'model_providers.rlmeval.requires_openai_auth=false' \
      --config 'shell_environment_policy.set.PATH="/usr/local/bin:/usr/bin:/bin"' \
      --disable plugins --disable remote_plugin --disable skill_mcp_dependency_install \
      "${args[@]:1}"
  fi
done
echo "Expected RLMEval's Codex workspace sandbox argument" >&2
exit 2
