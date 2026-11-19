#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

from prepare_credentials import read_profiles


def link_or_copy(source, destination):
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description='Stage a task-free image with shared Lean and Mathlib; mount one task per container')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archon-source', type=Path, required=True)
    parser.add_argument('--projects-root', type=Path, required=True)
    parser.add_argument('--elan-home', type=Path, required=True)
    parser.add_argument('--codex-vendor', type=Path, required=True, help='Codex vendor/x86_64-unknown-linux-musl directory')
    parser.add_argument('--repos', default='PFR')
    parser.add_argument('--secrets-file', type=Path, help='Embed the Key/URL profiles from secret.txt')
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    args.output.mkdir(parents=True, exist_ok=False)
    profiles = read_profiles(args.secrets_file) if args.secrets_file else {}
    (args.output / 'api-profiles.json').write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + '\n')
    shutil.copy2(Path(__file__).with_name('Dockerfile'), args.output / 'Dockerfile')
    shutil.copy2(Path(__file__).with_name('install_claude.py'), args.output / 'install_claude.py')
    shutil.copytree(repository / 'src/rlm_pipeline', args.output / 'rlm_pipeline', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(args.archon_source, args.output / 'archon', ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache'))
    shutil.copytree(args.codex_vendor, args.output / 'codex')
    selected = {name.strip().upper() for name in args.repos.split(',')}
    if not selected <= {'PFR', 'TLB'}:
        raise ValueError('Only PFR and TLB environments are supported')
    manifest = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'input_contract': 'single-task-shared-image-environment-v2',
        'contains_dataset': False,
        'harnesses': ['codex', 'claude-code'],
        'informal_policy': 'main-model-only',
        'claude_version': '2.1.322',
        'contains_benchmark_libraries': False,
        'contains_shared_mathlib': True,
        'api_profiles': sorted(profiles),
        'archon_commit': subprocess.check_output(['git', '-C', str(args.archon_source), 'rev-parse', 'HEAD'], text=True).strip(),
        'archon_dirty': bool(subprocess.check_output(['git', '-C', str(args.archon_source), 'status', '--porcelain'], text=True).strip()),
        'codex_version': subprocess.check_output([str(args.codex_vendor / 'bin/codex'), '--version'], text=True).strip(),
        'projects': {},
    }
    for name in sorted(selected):
        source = args.projects_root / name.lower()
        toolchain = (source / 'lean-toolchain').read_text().strip()
        environment = args.output / 'environments' / name.lower()
        (environment / 'lib').mkdir(parents=True)
        (environment / 'lean-toolchain').write_text(toolchain + '\n')
        modules = 0
        for package in sorted((source / '.lake/packages').iterdir()):
            library = package / '.lake/build/lib'
            if not library.is_dir():
                continue
            for module in library.rglob('*.olean'):
                destination = environment / 'lib' / module.relative_to(library)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError(f'Duplicate shared module: {destination}')
                link_or_copy(module, destination)
                modules += 1
        directory = toolchain.replace('/', '--').replace(':', '---')
        destination = args.output / 'toolchains' / directory
        if not destination.exists():
            shutil.copytree(args.elan_home / 'toolchains' / directory, destination, copy_function=link_or_copy)
        manifest['projects'][name] = {'toolchain': toolchain, 'shared_modules': modules}
        print(f'{name}: staged toolchain and {modules} shared dependency modules', flush=True)
    (args.output / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Build with: docker build -t rlmeval-archon:local {args.output}', flush=True)


if __name__ == '__main__':
    main()
