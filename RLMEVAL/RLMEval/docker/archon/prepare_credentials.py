from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex


def read_profiles(path: Path) -> dict:
    names = {'外部中转': 'external', '内部中转': 'internal', '自己平台': 'platform', '反代平台': 'reverse'}
    profiles = {}
    section = ''
    codex_url = ''
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith('## '):
            heading = line[3:].strip()
            if heading.startswith('智谱API KEY'):
                section = 'zhipu-2' if '第二个' in heading else 'zhipu'
            else:
                section = names.get(heading, heading)
            continue
        if not line:
            continue
        if section == '智谱URL':
            match = re.match(r'codex\s*[:：]\s*(\S+)', line, re.IGNORECASE)
            if match:
                codex_url = match[1]
        elif section in names.values() and line.startswith('export '):
            assignment = shlex.split(line[len('export '):])
            if len(assignment) != 1 or '=' not in assignment[0]:
                raise ValueError(f'Invalid export in profile {section}')
            name, value = assignment[0].split('=', 1)
            key = {'OPENAI_API_KEY': 'api_key', 'OPENAI_BASE_URL': 'base_url'}.get(name)
            if key:
                profiles.setdefault(section, {})[key] = value
        elif section in ('zhipu', 'zhipu-2') and not line.startswith('#'):
            if section in profiles:
                raise ValueError(f'Multiple keys in profile {section}')
            profiles[section] = {'api_key': line.strip('\"\'')}
    for name in ('zhipu', 'zhipu-2'):
        profiles.setdefault(name, {})['base_url'] = codex_url
    for name in (*names.values(), 'zhipu', 'zhipu-2'):
        entry = profiles.get(name, {})
        if not entry.get('api_key') or not entry.get('base_url', '').startswith(('https://', 'http://')):
            raise ValueError(f'Missing Key/URL in profile {name}')
    return profiles


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Bake the Key/URL pairs from secret.txt into an image profile file')
    parser.add_argument('--secret-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    profiles = read_profiles(args.secret_file)
    args.output.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + '\n')
    print('Prepared API profiles: ' + ', '.join(sorted(profiles)))
