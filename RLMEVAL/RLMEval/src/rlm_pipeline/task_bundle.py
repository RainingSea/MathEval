"""Host-side export and runtime validation of a single-task input mount."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .inputs import digest, export_item, mask_lean_literals, write_json

INPUT_FILES = {'item.json', 'Context.lean', 'Candidate.lean',
               'informal_statement.md', 'informal_proof.md', 'lean-toolchain', 'dependencies.json'}


def file_digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def toolchain_directory(toolchain: str) -> str:
    if not re.fullmatch(r'leanprover/lean4:v[0-9A-Za-z.+-]+', toolchain):
        raise ValueError(f'Unsupported Lean toolchain: {toolchain!r}')
    return toolchain.replace('/', '--').replace(':', '---')


def prepare_task(item: dict, output: Path, projects_root: Path, elan_home: Path) -> dict:
    if output.exists():
        raise FileExistsError(f'Task export destination already exists: {output}')
    if item['repo'] not in {'PFR', 'TLB'}:
        raise ValueError('Only PFR and TLB environments are supported')
    if not item.get('informal_proof', '').strip():
        raise ValueError(f"Task {item['index']} has no informal proof and is excluded")
    project = projects_root.resolve() / item['repo'].lower()
    toolchain = (project / 'lean-toolchain').read_text().strip()
    toolchain_root = (elan_home / 'toolchains' / toolchain_directory(toolchain)).resolve()
    libraries = [project / '.lake/build/lib']
    libraries.extend(p / '.lake/build/lib' for p in sorted((project / '.lake/packages').iterdir())
                     if (p / '.lake/build/lib').is_dir())
    imports = []
    for line in mask_lean_literals(item['context']).splitlines():
        match = re.match(r'^\s*import\s+(.+)', line)
        if match:
            imports.extend(match[1].split())
    if any(not re.fullmatch(r'[\w\u0080-\uffff]+(?:\.[\w\u0080-\uffff]+)*', name) for name in imports):
        raise ValueError('Unsupported import syntax in task context')
    env = os.environ.copy()
    env['LEAN_PATH'] = os.pathsep.join(map(str, libraries))
    result = subprocess.run([str(toolchain_root / 'bin/lean'), '--run',
                             str(Path(__file__).with_name('module_deps.lean')), *imports],
                            env=env, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise ValueError(f'Cannot resolve task imports: {result.stdout[-2000:]} {result.stderr[-2000:]}')
    modules = {}
    target_module = str(Path(item['file']).with_suffix('')).replace('/', '.')
    for line in result.stdout.splitlines():
        module, filename = line.split('\t', 1)
        source = Path(filename).resolve()
        if module == target_module:
            raise ValueError(f'Context imports target source module {module}; refusing to expose its compiled answer')
        if source.is_relative_to(toolchain_root):
            continue
        if not any(source.is_relative_to(p.resolve()) for p in libraries):
            raise ValueError(f'Dependency escaped the selected environment: {source}')
        if not source.is_relative_to((project / '.lake/build/lib').resolve()):
            continue  # Mathlib and package dependencies live once in the image.
        relative = 'lib/' + module.replace('.', '/') + '.olean'
        if '..' in Path(relative).parts:
            raise ValueError('Invalid dependency module name')
        modules[relative] = source
    manifest = export_item(item, output)
    (output / 'lib').mkdir()
    hashes = {}
    for relative, source in sorted(modules.items()):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Independent copies: making the exported tree read-only must never chmod
        # or allow edits to the host's shared compiled environment via hardlinks.
        shutil.copyfile(source, target)
        hashes[relative] = file_digest(target)
    (output / 'lean-toolchain').write_text(toolchain + '\n')
    write_json(output / 'dependencies.json', {'schema_version': 1, 'item_index': item['index'],
                                             'toolchain': toolchain, 'files': hashes})
    return manifest


def load_task(directory: Path) -> dict:
    """Reject dataset mounts, sibling tasks, injected source files and changed inputs."""
    directory = directory.resolve()
    manifest = json.loads((directory / 'item.json').read_text())
    if set(manifest) != {'schema_version', 'mode', 'task', 'item', 'decl', 'context_sha256',
                         'initial_candidate_sha256', 'informal_stmt', 'informal_proof'}:
        raise ValueError('Unexpected fields in single-task manifest')
    if manifest.get('schema_version') != 1 or manifest.get('mode') != 'easy' or manifest.get('task') != 'proof':
        raise ValueError('Expected one exported RLMEval easy proof task')
    item = dict(manifest['item'])
    if set(item) != {'index', 'repo', 'name', 'label', 'file'} or item['repo'] not in {'PFR', 'TLB'}:
        raise ValueError('Invalid single-task metadata')
    if not isinstance(item['index'], int) or item['index'] <= 0:
        raise ValueError('Invalid single-task index')
    for field in ('decl', 'informal_stmt', 'informal_proof'):
        item[field] = manifest[field]
    if not all(isinstance(item[field], str) for field in ('repo', 'name', 'label', 'file', 'decl', 'informal_stmt', 'informal_proof')):
        raise ValueError('Task fields must be strings')
    if not re.search(r':=\s*by\s*$', item['decl']):
        raise ValueError('Expected an unsolved tactic-proof declaration')
    item['context'] = (directory / 'Context.lean').read_text(encoding='utf-8')
    if not item['informal_proof'].strip():
        raise ValueError('Task has no informal proof and is excluded')
    if digest(item['context']) != manifest['context_sha256']:
        raise ValueError('Task context checksum mismatch')
    if (directory / 'Candidate.lean').read_text() != item['decl'] + '\n  sorry\n':
        raise ValueError('Input Candidate.lean must be an unsolved template')
    if digest((directory / 'Candidate.lean').read_text()) != manifest['initial_candidate_sha256']:
        raise ValueError('Initial candidate checksum mismatch')
    if (directory / 'informal_statement.md').read_text() != item['informal_stmt'] or (directory / 'informal_proof.md').read_text() != item['informal_proof']:
        raise ValueError('Informal inputs differ from task manifest')
    dependencies = json.loads((directory / 'dependencies.json').read_text())
    toolchain = (directory / 'lean-toolchain').read_text().strip()
    toolchain_directory(toolchain)
    if dependencies.get('schema_version') != 1 or dependencies['item_index'] != item['index'] or dependencies['toolchain'] != toolchain:
        raise ValueError('Dependency manifest does not match the task')
    expected = INPUT_FILES | set(dependencies['files'])
    actual = set()
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'Symlinks are not allowed in task inputs: {path.name}')
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
        elif not path.is_dir():
            raise ValueError(f'Unexpected task input file type: {path.name}')
    if actual != expected:
        raise ValueError(f'Input must contain exactly one task; unexpected/missing files: {sorted(actual ^ expected)[:10]}')
    target_library = 'lib/' + str(Path(item['file']).with_suffix('.olean'))
    if target_library in dependencies['files']:
        raise ValueError('Input includes the compiled target source module')
    for relative, checksum in dependencies['files'].items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or path.parts[0] != 'lib' or path.suffix != '.olean':
            raise ValueError(f'Invalid dependency path: {relative}')
        if file_digest(directory / path) != checksum:
            raise ValueError(f'Dependency checksum mismatch: {relative}')
    return item
