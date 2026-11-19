from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .inputs import export_item, load_item
from .credentials import API_PROFILES


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('Must be nonnegative')
    return number


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('Must be positive')
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description='RLMEval easy inputs and Archon generation pipeline')
    subcommands = parser.add_subparsers(dest='command', required=True)
    export = subcommands.add_parser('export', help='Export method-independent easy context and informal proof by dataset index')
    run = subcommands.add_parser('run', help='Run one Archon item and write generation.json')
    for command in (export, run):
        command.add_argument('--output', required=True, help='New output directory; existing directories are refused')
        command.add_argument('--mode', choices=['easy'], default='easy')
    export.add_argument('--dataset', type=Path, required=True)
    export.add_argument('--id', type=positive, required=True, help='The dataset index (not the line number)')
    export.add_argument('--projects-root', type=Path, required=True, help='Host compiled benchmark environments')
    export.add_argument('--elan-home', type=Path, default=Path(os.environ.get('ELAN_HOME', '/data/elan')))
    run.add_argument('--input', type=Path, required=True, help='Read-only bundle containing exactly one exported task')
    run.add_argument('--envs-root', default=os.environ.get('RLM_ENVS_ROOT', '/opt/rlm/envs'))
    run.add_argument('--harness', choices=['codex', 'claude-code', 'cc'], default='codex',
                     help='Agent engine: codex uses Responses; claude-code/cc uses Anthropic Messages (different base URL)')
    run.add_argument('--model', default='glm-5.3')
    run.add_argument('--api-profile', choices=API_PROFILES, default='env',
                     help='Select a built-in Key/URL pair; env reads the host-provided key')
    run.add_argument('--api-base', help='Override the selected profile URL')
    run.add_argument('--api-key-env', default='ARCHON_API_KEY')
    run.add_argument('--elan-home', default=os.environ.get('ELAN_HOME', '/opt/elan'))
    run.add_argument('--archon-bin', default='archon')
    run.add_argument('--codex-bin', default='codex')
    run.add_argument('--timeout', type=positive, default=21600)
    run.add_argument('--max-iterations', type=positive, default=3)
    run.add_argument('--review', action='store_true')
    run.add_argument('--reasoning-effort', default='high')
    run.add_argument('--sandbox', choices=['workspace-write', 'danger-full-access'], default='danger-full-access')
    run.add_argument('--agent-user', default=os.environ.get('RLM_AGENT_USER', ''))
    run.add_argument('--run-id')
    run.add_argument('--sample-id', type=nonnegative, default=0)
    run.add_argument('--prepare-only', action='store_true')
    extract = subcommands.add_parser('extract', help='Re-extract generation.json from an existing output directory')
    extract.add_argument('--output', required=True)
    args = parser.parse_args()
    if getattr(args, 'harness', None) == 'cc':
        args.harness = 'claude-code'
    if args.command == 'export':
        from .task_bundle import prepare_task
        manifest = prepare_task(load_item(args.dataset, args.id), Path(args.output),
                                args.projects_root, args.elan_home)
        print(json.dumps(manifest['item'], ensure_ascii=False))
        return 0
    if args.command == 'run':
        from .archon_runner import run as run_archon
        return run_archon(args)
    if args.command == 'extract':
        from .archon_runner import collect_generation
        record = collect_generation(Path(args.output).resolve())
        print(json.dumps({'item_index': record['item_index'], 'status': record['status'],
                          'extraction_status': record['extraction_status']}, ensure_ascii=False))
        return 0 if record['extraction_status'] == 'ok' else 1
    raise AssertionError(args.command)


if __name__ == '__main__':
    raise SystemExit(main())
