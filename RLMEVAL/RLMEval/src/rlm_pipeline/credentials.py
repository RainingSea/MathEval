from __future__ import annotations

import json
import os
from pathlib import Path

OFFICIAL_API_BASE = 'https://open.bigmodel.cn/api/v1'
API_PROFILES = ('env', 'zhipu', 'zhipu-2', 'external', 'internal', 'platform', 'reverse')


def resolve_api(args) -> tuple[str, str]:
    profile = getattr(args, 'api_profile', 'env')
    cc = getattr(args, 'harness', 'codex') in ('cc', 'claude-code')
    cc_base = getattr(args, 'api_base', None) or os.environ.get('ANTHROPIC_BASE_URL')
    if cc and not cc_base:
        raise ValueError('CC requires an explicit Anthropic Messages base URL: set ANTHROPIC_BASE_URL or --api-base. Codex Responses base URLs are not interchangeable.')
    if profile == 'env':
        key = os.environ.get(args.api_key_env, '')
        if cc and not key:
            key = os.environ.get('ANTHROPIC_AUTH_TOKEN') or os.environ.get('ANTHROPIC_API_KEY', '')
        base = os.environ.get('ARCHON_API_BASE') or OFFICIAL_API_BASE
        if not key:
            raise ValueError(f'Missing API key environment variable: {args.api_key_env}')
    else:
        path = Path(os.environ.get('RLM_API_PROFILES', '/opt/rlm/api-profiles.json'))
        profiles = json.loads(path.read_text())
        if profile not in profiles:
            raise ValueError(f'API profile is not included in this image: {profile}')
        entry = profiles[profile]
        key = entry.get('api_key', '')
        base = entry.get('base_url', '')
        if not key or not base:
            raise ValueError(f'Incomplete API profile: {profile}')
    return key, cc_base if cc else getattr(args, 'api_base', None) or base
