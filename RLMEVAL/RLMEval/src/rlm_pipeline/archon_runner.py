from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import time

from .processes import stop_group
from .credentials import resolve_api
from .inputs import export_item, metadata, write_json
from .task_bundle import load_task, toolchain_directory


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_lean(project: Path, elan_home: Path, environment: Path | None = None) -> tuple[str, str, str]:
    toolchain = (project / 'lean-toolchain').read_text().strip()
    directory = toolchain_directory(toolchain)
    binary = elan_home / 'toolchains' / directory / 'bin/lean'
    if not binary.is_file():
        raise FileNotFoundError(f'Lean binary missing: {binary}')
    libraries = [project / 'lib']
    if not libraries[0].is_dir():
        raise FileNotFoundError(f'Precompiled project library missing: {libraries[0]}')
    if environment is not None:
        if (environment / 'lean-toolchain').read_text().strip() != toolchain:
            raise ValueError('Image environment does not match the task Lean toolchain')
        if not (environment / 'lib').is_dir():
            raise FileNotFoundError(f'Shared image library missing: {environment}')
        libraries.append(environment / 'lib')
    return str(binary.resolve()), os.pathsep.join(str(path.resolve()) for path in libraries), toolchain


def contract() -> str:
    return """# RLMEval easy proof task

Read TASK.md and Context.lean. Task.lean contains the benchmark context followed
by one target theorem. Edit its proof, preserving the context and theorem signature.
The informal statement and informal proof in TASK.md are the benchmark inputs.

You may run `lean Task.lean` as often as needed. Lean and the required precompiled
libraries are configured in PATH and LEAN_PATH. Compilation has no attempt limit.
Save the final proof in Task.lean, rather than only in a chat message or report.
The container exports your generated answer without scoring it. The host runs
RLMEval's proof evaluation afterward.

This is one benchmark item. Plan only Task.lean and dispatch the prover promptly.
Do not create a blueprint, run a full project build, or plan other objectives.
You may update Archon state and reports.

For an informal proof sketch you may call:
`python3 .claude/tools/archon-informal-agent.py --provider main "your question"`.
This helper uses only the current main model, API endpoint, and key. Do not
request GPT or another provider/model. No separate external-model key is needed;
ARCHON_MAIN_MODEL indicates the configured model. Failures never fall back.
"""


def prepare_archon(item: dict, output: Path, args) -> tuple[Path, dict]:
    import archon

    workspace = output / 'workspace'
    manifest = export_item(item, output / 'input')
    workspace.mkdir()
    (workspace / 'Context.lean').write_text(item['context'], encoding='utf-8')
    (workspace / 'Task.lean').write_text(item['context'] + '\n' + item['decl'] + '\n  sorry\n', encoding='utf-8')
    instructions = contract()
    (workspace / 'TASK.md').write_text(
        instructions + '\n## Informal statement\n\n' + item['informal_stmt']
        + '\n\n## Informal proof\n\n' + item['informal_proof'] + '\n', encoding='utf-8')
    (workspace / 'AGENTS.md').write_text(instructions, encoding='utf-8')
    (workspace / 'USER_HINTS.md').write_text(instructions, encoding='utf-8')
    assets = Path(archon.__file__).parent / '.archon-src'
    tools = workspace / '.claude' / 'tools'
    tools.mkdir(parents=True)
    (tools / 'archon-informal-agent.py').symlink_to(assets / 'tools/informal_agent.py')
    state = workspace / '.archon'
    shutil.copytree(assets / 'archon-template', state)
    for name in ('prompts', 'prover-modes', 'subagents'):
        shutil.copytree(assets / name, state / name)
    (state / 'VERSION').write_text(archon.__version__ + '\n')
    (state / 'USER_HINTS.md').write_text(instructions)
    (state / 'PROGRESS.md').write_text(
        '# Project Progress\n\n## Current Stage\n\nprover\n\n## Current Objectives\n\n'
        '### 1. **`Task.lean`** [prover-mode: prove]\n'
        f"- Prove only `{item['name']}` at the end; preserve the entire prefix.\n"
        '- Read TASK.md; use lean Task.lean for compiler feedback.\n')
    for relative in ('prompts/plan.md', 'prover-modes/prove.md'):
        path = state / relative
        path.write_text(instructions + '\n\n' + path.read_text())
    harness = getattr(args, 'harness', 'codex')
    binary_name = args.codex_bin if harness == 'codex' else 'claude'
    binary = shutil.which(binary_name)
    if not binary:
        raise FileNotFoundError(f'{harness} executable missing: {binary_name}')
    configuration = {
        'loop': {'harness': harness, 'model': args.model, 'max_iterations': args.max_iterations,
                 'parallel': False, 'max_parallel': 1, 'no_review': not args.review,
                 'axiom_sweep': False, 'verbose_logs': True},
        'harnesses': {'codex': {'runner': 'codex', 'bin': binary, 'model': args.model,
                              'sandbox': args.sandbox, 'effort': args.reasoning_effort,
                              'prompt_variant': 'codex', 'base_url_env': 'ARCHON_API_BASE',
                              'key_env': 'ARCHON_API_KEY', 'wire_api': 'responses',
                              'extra_args': '-c features.plugins=false -c features.responses_websockets=false '
                                            '-c features.responses_websockets_v2=false'}},
        'subagents': {'enabled': []}, 'multilane': {'enabled': False},
    }
    if harness == 'claude-code':
        configuration['harnesses'] = {'claude-code': {'runner': 'claude-code', 'model': args.model}}
    write_json(state / 'config.json', configuration)
    _, _, toolchain = resolve_lean(Path(args.input).resolve(), Path(args.elan_home).resolve(),
                                               Path(args.envs_root).resolve() / item['repo'].lower())
    (workspace / 'lean-toolchain').write_text(toolchain + '\n')
    (workspace / 'archon-protected.yaml').write_text('files:\n  - Context.lean\n  - TASK.md\n')
    (workspace / '.gitignore').write_text('.archon/\n')
    return workspace, manifest


def collect_generation(output: Path) -> dict:
    run = json.loads((output / 'run.json').read_text())
    item = run['item']
    workspace = output / 'workspace'
    record = {
        'run_id': run['run_id'], 'track': 'archon', 'task': 'proof', 'mode': 'easy',
        'item_index': item['index'], 'item_name': item['label'], 'item_repo': item['repo'],
        'item': item, 'sample_id': run['sample_id'], 'model': run['model'], 'harness': run.get('harness', 'codex'),
        'status': run.get('status', 'agent_error'), 'exit_code': run.get('exit_code'),
        'termination_reason': run.get('termination_reason'), 'error_detail': run.get('error_detail'),
        'started_at': run['started_at'], 'finished_at': run.get('finished_at'),
        'wall_ms': run.get('wall_ms'), 'workspace': str(workspace),
        'events_path': str(workspace / '.archon/logs'),
        'output_truncated': False, 'raw_text': '', 'agent_final_message': None,
        'extraction_status': 'not_attempted', 'evaluation_status': 'not_evaluated',
    }
    try:
        task = (workspace / 'Task.lean').read_text(encoding='utf-8')
        context = (output / 'input/Context.lean').read_text(encoding='utf-8')
        prefix = context + '\n'
        record['context_prefix_preserved'] = task.startswith(prefix)
        candidate = task[len(prefix):] if task.startswith(prefix) else task
        record['raw_text'] = candidate
        record['source_format'] = 'declaration' if task.startswith(prefix) else 'task'
        record['extraction_status'] = 'ok'
        (output / 'Task.lean').write_text(task, encoding='utf-8')
        (output / 'Candidate.lean').write_text(candidate, encoding='utf-8')
    except OSError as error:
        record['extraction_status'] = 'failed'
        record['extraction_error'] = str(error)
    logs = workspace / '.archon/logs'
    for path in sorted(logs.glob('iter-*/*.jsonl')):
        if 'raw' in path.name:
            continue
        with path.open(errors='replace') as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('event') == 'session_end' and event.get('summary'):
                    record['agent_final_message'] = event['summary']
    write_json(output / 'generation.json', record)
    (output / 'generations.jsonl').write_text(json.dumps(record, ensure_ascii=False) + '\n', encoding='utf-8')
    return record


def run(args) -> int:
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o755)
    started = time.monotonic()
    run_state = {
        'run_id': args.run_id or output.name, 'item': {'index': None, 'repo': '', 'name': '', 'label': '', 'file': ''},
        'sample_id': args.sample_id, 'model': args.model, 'harness': getattr(args, 'harness', 'codex'), 'informal_policy': 'main-model-only', 'mode': 'easy', 'task': 'proof',
        'started_at': utc_now(), 'status': 'preparing', 'evaluation_status': 'not_evaluated',
        'max_iterations': args.max_iterations, 'timeout_seconds': args.timeout,
    }
    write_json(output / 'run.json', run_state)
    process = None
    old_handlers = {}

    def interrupted(signum, frame):
        raise InterruptedError(f'Received signal {signum}')

    try:
        item = load_task(Path(args.input))
        run_state['item'] = metadata(item)
        workspace, _ = prepare_archon(item, output, args)
        if args.prepare_only:
            run_state.update(status='prepared', exit_code=0)
            return 0
        api_key, api_base = resolve_api(args)
        run_state.update(api_profile=args.api_profile, api_base=api_base)
        print(f"Harness: {getattr(args, 'harness', 'codex')}; base URL: {api_base}. CC uses Anthropic Messages; Codex uses Responses.", flush=True)
        binary = shutil.which(args.archon_bin)
        if not binary:
            raise FileNotFoundError(f'Archon executable missing: {args.archon_bin}')
        command = [binary, 'loop', str(workspace), '--stage', 'prover', '--serial',
                   '--max-iterations', str(args.max_iterations), '--max-objectives', '1',
                   '--no-dashboard', '--no-finalize', '--review' if args.review else '--no-review']
        if args.agent_user:
            account = pwd.getpwnam(args.agent_user)
            for path in [workspace, *workspace.rglob('*')]:
                if not path.is_symlink():
                    os.chown(path, account.pw_uid, account.pw_gid)
            command = ['runuser', '-u', args.agent_user, '--', *command]
        environment = os.environ.copy()
        lean_bin, lean_path, _ = resolve_lean(Path(args.input).resolve(), Path(args.elan_home).resolve(),
                                              Path(args.envs_root).resolve() / item['repo'].lower())
        environment.update(ARCHON_MAIN_MODEL=args.model, ARCHON_INFORMAL_MAIN_ONLY='1',
                           ARCHON_MAIN_WIRE_API='messages' if getattr(args, 'harness', 'codex') == 'claude-code' else 'responses',
                           LEAN_PATH=lean_path, ELAN_HOME=str(args.elan_home),
                           PATH=str(Path(lean_bin).parent) + os.pathsep + environment.get('PATH', ''),
                           ARCHON_API_BASE=api_base, ARCHON_API_KEY=api_key,
                           PYTHONUNBUFFERED='1', GIT_CONFIG_NOSYSTEM='1')
        if getattr(args, 'harness', 'codex') == 'claude-code':
            environment.update(ANTHROPIC_BASE_URL=api_base, ANTHROPIC_AUTH_TOKEN=api_key,
                               ANTHROPIC_API_KEY='', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        run_state.update(status='running', command=command)
        write_json(output / 'run.json', run_state)
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, interrupted)
        with (output / 'archon.log').open('w') as log:
            process = subprocess.Popen(command, cwd=workspace, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            run_state['pid'] = process.pid
            write_json(output / 'run.json', run_state)
            code = process.wait(timeout=args.timeout)
        run_state.update(status='ok' if code == 0 else 'agent_error', exit_code=code)
    except subprocess.TimeoutExpired:
        run_state.update(status='timeout', exit_code=124, termination_reason='agent_timeout')
    except InterruptedError as error:
        run_state.update(status='cancelled', exit_code=130, termination_reason='signal', error_detail=str(error))
    except Exception as error:
        run_state.update(status='setup_error', exit_code=1, error_detail=f'{type(error).__name__}: {error}')
    finally:
        if process is not None:
            stop_group(process)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        run_state.update(finished_at=utc_now(), wall_ms=int((time.monotonic() - started) * 1000))
        write_json(output / 'run.json', run_state)
        record = collect_generation(output)
        print(json.dumps({'generation': str(output / 'generation.json'), 'status': record['status'],
                          'evaluation_status': record['evaluation_status']}, ensure_ascii=False), flush=True)
    return int(run_state.get('exit_code') or 0)
