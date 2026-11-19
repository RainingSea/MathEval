#!/usr/bin/env python3
"""Assemble Docker outputs for the repository's existing RLMEval agent evaluator.

Scoring and reports are produced by the unmodified rlm_agent.cli score and
scripts/summarize_codex_run.py. This adapter only arranges data and log paths.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def rows(path):
    with Path(path).open(encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def dump_rows(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(value, ensure_ascii=False) + '\n' for value in values), encoding='utf-8')


def discover(inputs):
    files = set()
    for entry in inputs:
        path = Path(entry).resolve(strict=True)
        if path.is_file():
            files.add(path)
        else:
            files.update(path.rglob('generations.jsonl'))
            files.update(p for p in path.rglob('generation.json') if not p.with_name('generations.jsonl').exists())
    if not files:
        raise ValueError('No generation.json or generations.jsonl found in the input paths')
    return sorted(files)


def read_generations(inputs):
    records, seen = [], {}
    for path in discover(inputs):
        values = rows(path) if path.suffix == '.jsonl' else [json.loads(path.read_text())]
        for line, record in enumerate(values, 1):
            if not isinstance(record, dict):
                raise ValueError(f'{path}:{line}: expected an agent generation object')
            item_id = record.get('item_index', record.get('item', {}).get('index'))
            if not isinstance(item_id, int):
                raise ValueError(f'{path}:{line}: missing integer item_index')
            identity = (str(path.parent), record.get('run_id'), item_id, record.get('sample_id', 0))
            if identity in seen:
                if record != seen[identity]:
                    raise ValueError(f'{path}:{line}: conflicting duplicate run/item/sample')
                continue
            seen[identity] = record
            records.append((record, {'file': str(path), 'line': line}))
    if not records:
        raise ValueError('The input contains no generations')
    return records


def host_path(value, record, source):
    if not value:
        return None
    path = Path(value)
    source_dir = Path(source['file']).parent
    # A container's /outputs/result corresponds to the generation file's host directory.
    workspace = record.get('workspace')
    if workspace:
        container_root = Path(workspace).parent
        if path.is_relative_to(container_root):
            candidate = source_dir / path.relative_to(container_root)
            if candidate.exists():
                return candidate.resolve()
    if not path.is_absolute():
        candidate = source_dir / path
        if candidate.exists():
            return candidate.resolve()
    return path.resolve() if path.exists() else None


def transport_logs(record, source, output, number):
    """Relocate event paths so the existing report can find turn.failed messages."""
    path = host_path(record.get('events_path'), record, source)
    if path is None:
        candidate = Path(source['file']).parent / 'workspace/.archon/logs'
        if candidate.is_dir():
            path = candidate
    if path is None:
        detail = record.get('error_detail')
        if detail and record.get('status') not in {'ok', 'completed'}:
            target = output / 'logs' / f'{number:06d}.events.jsonl'
            event = {'type': 'turn.failed', 'error': {'message': str(detail)},
                     'source': 'generation.error_detail'}
            dump_rows(target, [event])
            return str(target), [{'file': source['file'], 'field': 'error_detail', 'event': event}], []
        return None, [], []
    files = [path] if path.is_file() else sorted(path.rglob('*.jsonl'))
    if path.is_dir():
        raw_files = [file for file in files if file.name.endswith('.codex-raw.jsonl')]
        files = raw_files or files
    target = output / 'logs' / f'{number:06d}.events.jsonl'
    target.parent.mkdir(parents=True, exist_ok=True)
    failures, invalid_lines = [], []
    with target.open('w', encoding='utf-8') as handle:
        for file in files:
            with file.open(errors='replace') as source_handle:
                for line_number, line in enumerate(source_handle, 1):
                    handle.write(line if line.endswith('\n') else line + '\n')
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        invalid_lines.append({'file': str(file), 'line': line_number})
                        continue
                    if not isinstance(event, dict):
                        continue
                    if (event.get('type') in {'turn.failed', 'error'}
                            or event.get('is_error') is True
                            or event.get('event') == 'error'):
                        failures.append({'file': str(file), 'line': line_number, 'event': event})
    return str(target), failures, invalid_lines


def assemble_candidates(directory, dataset, output, projects_root=None):
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise ValueError('--candidates must be a directory')
    dataset_rows = rows(dataset)
    items = {row['index']: row for row in dataset_rows}
    if not items or len(items) != len(dataset_rows):
        raise ValueError('Dataset must have at least one item and unique indices')

    candidates = {}
    metadata = {}
    visited = set()
    for root, directories, files in os.walk(directory, followlinks=True):
        folder = Path(root)
        resolved = folder.resolve()
        if resolved in visited:
            directories.clear()
            continue
        visited.add(resolved)
        # Docker archives may contain the original input Candidate.lean and dependency trees.
        directories[:] = sorted(name for name in directories if name != 'input' and not name.startswith('.'))
        # Per-task metadata is more specific than a batch-level export.
        metadata_name = next((name for name in ('generation-metadata.json', 'generation.json', 'generations.jsonl') if name in files), None)
        if metadata_name:
            metadata_path = folder / metadata_name
            values = rows(metadata_path) if metadata_path.suffix == '.jsonl' else [json.loads(metadata_path.read_text())]
            for value in values:
                index = value.get('item_index', value.get('item', {}).get('index'))
                identity = (index, value.get('sample_id', 0))
                if index not in items:
                    continue
                if identity in metadata and len(folder.parts) <= len(metadata[identity][1].parent.parts):
                    continue
                metadata[identity] = (value, metadata_path)
        for filename in sorted(files):
            path = folder / filename
            if filename.lower() != 'candidate.lean' or not path.is_file():
                continue
            parts = path.relative_to(directory).parts[:-1]
            matches = (re.fullmatch(r'(?:task-)?([0-9]+)(?:-([0-9]+))?', name) for name in reversed(parts))
            match = next((entry for entry in matches if entry), None)
            if match is None:
                raise ValueError(f'Cannot identify item for {path}; use <item_index>, task-<item_index>, '
                                 'or <item_index>-<sample_id> as a parent directory')
            identity = (int(match[1]), int(match[2] or 0))
            if identity[0] not in items:
                raise ValueError(f'Unknown dataset index {identity[0]} in {path}')
            text = path.read_text(encoding='utf-8')
            if identity in candidates and candidates[identity][1] != text:
                raise ValueError(f'Conflicting candidates for {identity}: {candidates[identity][0]} and {path}')
            candidates.setdefault(identity, (path, text))
    if not candidates and not metadata:
        raise ValueError(f'No Candidate.lean files or generation metadata found in {directory}')

    records, mappings = [], []
    found_items = {index for index, _ in candidates}
    known_samples = candidates.keys() | metadata.keys()
    known_items = {index for index, _ in known_samples}
    identities = sorted(known_samples | {(index, 0) for index in items.keys() - known_items})
    for index, sample in identities:
        item = items[index]
        path, text = candidates.get((index, sample), (None, ''))
        if projects_root is not None:
            item['project_dir'] = str((projects_root / item['repo'].lower()).resolve())
        source_record, source_path = metadata.get((index, sample), ({}, None))
        events = None
        if source_path is not None:
            events = host_path(source_record.get('events_path'), source_record, {'file': str(source_path)})
            if events is None:
                local_logs = source_path.parent / 'workspace/.archon/logs'
                if local_logs.is_dir():
                    events = local_logs
        records.append({
            'run_id': output.name,
            'source_run_id': source_record.get('run_id'),
            'track': 'candidate_directory',
            'item_index': index,
            'item_name': item['label'],
            'item_repo': item['repo'],
            'sample_id': sample,
            'status': source_record.get('status') or ('unknown' if path else 'missing_candidate'),
            'model': source_record.get('model'),
            'exit_code': source_record.get('exit_code'),
            'termination_reason': source_record.get('termination_reason'),
            'error_detail': source_record.get('error_detail'),
            'extraction_error': source_record.get('extraction_error'),
            'error_events': source_record.get('error_events') or [],
            'events_path': str(events) if events else '',
            'candidate_present': path is not None,
            'workspace': str(path.parent) if path else None,
            'raw_text': text,
        })
        mappings.append({'item_index': index, 'sample_id': sample, 'candidate': str(path) if path else None})

    output.mkdir(parents=True, exist_ok=False)
    dump_rows(output / 'items.jsonl', (items[index] for index in sorted(items)))
    dump_rows(output / 'generations.jsonl', records)
    dump_rows(output / 'sample_sources.jsonl', mappings)
    manifest = {
        'dataset': str(dataset.resolve()),
        'candidate_directory': str(directory),
        'item_count': len(items),
        'prediction_count': len(records),
        'candidate_count': len(candidates),
        'missing_items': sorted(items.keys() - found_items),
    }
    dump(output / 'assembly.json', manifest)
    return manifest


def assemble(inputs, dataset, output, projects_root=None):
    dataset_rows = rows(dataset)
    items = {row['index']: row for row in dataset_rows}
    if len(items) != len(dataset_rows):
        raise ValueError('Dataset indices must be unique')
    records = read_generations(inputs)
    output.mkdir(parents=True, exist_ok=False)
    dump_rows(output / 'source_generations.jsonl', (record for record, _ in records))
    normalized, selected, mappings, diagnostics = [], {}, [], []
    for number, (record, source) in enumerate(records):
        index = record.get('item_index', record.get('item', {}).get('index'))
        if index not in items:
            raise ValueError(f'Unknown dataset index {index} in {source}')
        item = dict(items[index])
        if not item.get('informal_proof', '').strip():
            raise ValueError(f'Item {index} is excluded: no informal proof')
        if record.get('mode', 'easy') != 'easy' or record.get('task', 'proof') != 'proof':
            raise ValueError('This entry supports easy proof generations')
        if record.get('item_repo', item['repo']) != item['repo']:
            raise ValueError(f'Item {index}: repository metadata mismatch')
        if projects_root is not None:
            item['project_dir'] = str((projects_root / item['repo'].lower()).resolve())
        selected[index] = item
        row = dict(record)
        row.update(item_index=index, item_name=record.get('item_name', item['label']),
                   item_repo=item['repo'], sample_id=record.get('sample_id', 0))
        text = row.get('raw_text') or ''
        if not isinstance(text, str):
            raise ValueError(f'{source}: raw_text must be a string')
        prefix = item['context'] + '\n'
        if text.startswith(prefix):
            row['raw_text'] = text[len(prefix):]
        elif row.get('source_format') == 'proof':
            body = text.lstrip()
            if body.startswith('by\n') or body.startswith('by '):
                body = body[2:].lstrip('\n ')
            row['raw_text'] = item['decl'] + '\n' + body
        # Keep full declarations (including sorry/changed signatures) verbatim.
        # Existing RepoScorer, rather than this adapter, assigns all score statuses.
        path, failures, invalid_lines = transport_logs(record, source, output, number)
        if path:
            row['events_path'] = path
        workspace = host_path(record.get('workspace'), record, source)
        if workspace:
            row['workspace'] = str(workspace)
        normalized.append(row)
        mappings.append({'sample_index': number, 'item_index': index, 'source': source,
                         'source_run_id': record.get('run_id'), 'source_events_path': record.get('events_path'),
                         'events_path': row.get('events_path'), 'workspace': row.get('workspace')})
        diagnostics.append({'sample_index': number, 'item_index': index, 'sample_id': row['sample_id'],
                            'source': source, 'generation_status': row.get('status'),
                            'exit_code': row.get('exit_code'), 'termination_reason': row.get('termination_reason'),
                            'error_detail': row.get('error_detail'), 'error_events': failures,
                            'invalid_event_lines': invalid_lines})
    dump_rows(output / 'items.jsonl', (selected[index] for index in sorted(selected)))
    dump_rows(output / 'generations.jsonl', normalized)
    dump_rows(output / 'sample_sources.jsonl', mappings)
    dump_rows(output / 'generation_diagnostics.jsonl', diagnostics)
    manifest = {'dataset': str(dataset.resolve()), 'item_count': len(selected),
                'prediction_count': len(normalized), 'sample_counts': dict(Counter(row['item_index'] for row in normalized)),
                'generation_statuses': dict(Counter(row.get('status', 'missing') for row in normalized)),
                'error_event_count': sum(len(row['error_events']) for row in diagnostics),
                'format': 'existing RlmItem items.jsonl and rlm_agent generations.jsonl',
                'scorer': 'rlm_agent.cli score --task proof',
                'report': 'scripts/summarize_codex_run.py'}
    dump(output / 'assembly.json', manifest)
    return manifest


def evaluate(output, elan_home, task="proof"):
    environment = os.environ.copy()
    environment['ELAN_HOME'] = str(elan_home)
    environment['PATH'] = str(elan_home / 'bin') + os.pathsep + environment.get('PATH', '')
    environment['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + environment.get('PYTHONPATH', '')
    commands = [
        [sys.executable, '-m', 'rlm_agent.cli', 'score', '--task', task,
         '--dataset', str(output / 'items.jsonl'), '--input', str(output / 'generations.jsonl'),
         '--output', str(output / 'scores.jsonl')],
        [sys.executable, str(ROOT / 'scripts/summarize_codex_run.py'),
         '--run-id', output.name, '--artifacts-dir', str(output.parent),
         '--dataset', str(output / 'items.jsonl'), '--output-dir', str(output)],
    ]
    source_paths = [ROOT / 'src/rlm_agent/scoring.py', ROOT / 'scripts/summarize_codex_run.py']
    dump(output / 'evaluator.json', {'commands': commands, 'python': sys.executable,
                                    'sources': {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                                for path in source_paths}})
    for command in commands:
        print('Running existing RLMEval ' + ('score' if 'score' in command else 'report') + ' entrypoint', flush=True)
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
    return json.loads((output / 'summary.json').read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='*', type=Path, help='Mounted output directories or generation JSON/JSONL files')
    parser.add_argument('--candidates', type=Path, help='Read Candidate.lean files from this directory')
    parser.add_argument('--task', choices=('proof', 'proof-normal', 'statement'), default='proof')
    parser.add_argument('--output', required=True, type=Path, help='New host evaluation directory outside input mounts')
    parser.add_argument('--dataset', type=Path, default=ROOT / 'data/rlm25_pfr_tlb.jsonl')
    parser.add_argument('--projects-root', type=Path, help='Optional root containing <repo> projects; otherwise use dataset paths')
    parser.add_argument('--elan-home', type=Path)
    parser.add_argument('--assemble-only', action='store_true')
    args = parser.parse_args()
    if bool(args.inputs) == bool(args.candidates):
        parser.error('provide either --candidates or generation inputs')
    if args.inputs and args.task != 'proof':
        parser.error('generation inputs support only --task proof')
    output = args.output.resolve()
    if output.exists():
        parser.error('--output already exists; choose a new evaluation directory')
    for source in ([args.candidates] if args.candidates else args.inputs):
        if source.is_dir() and output.is_relative_to(source.resolve()):
            parser.error('--output must be outside all input directories')
    try:
        if args.candidates:
            manifest = assemble_candidates(args.candidates, args.dataset, output, args.projects_root)
        else:
            projects_root = args.projects_root or ROOT.parent / 'rlm-env'
            manifest = assemble(args.inputs, args.dataset, output, projects_root)
        print(f"Assembled {manifest['item_count']} item(s), {manifest['prediction_count']} prediction(s).", flush=True)
        if not args.assemble_only:
            elan_home = args.elan_home or (ROOT.parent / 'elan' if args.candidates else Path('/data/elan'))
            evaluate(output, elan_home, args.task)
        print(f'Results: {output}', flush=True)
        return 0
    except Exception as error:
        if output.is_dir():
            dump(output / 'evaluation_error.json', {'error': f'{type(error).__name__}: {error}'})
        raise


if __name__ == '__main__':
    raise SystemExit(main())
