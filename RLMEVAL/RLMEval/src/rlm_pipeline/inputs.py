from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def load_item(dataset: Path, item_id: int) -> dict:
    matches = []
    with dataset.open(encoding='utf-8') as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if row.get('index') == item_id:
                    matches.append(row)
    if len(matches) != 1:
        raise ValueError(f'Expected one item with index={item_id}, found {len(matches)}')
    item = matches[0]
    for field in ('repo', 'name', 'label', 'file', 'context', 'decl', 'informal_stmt', 'informal_proof'):
        if not isinstance(item.get(field), str):
            raise ValueError(f'Item {item_id}: missing string field {field}')
    if not re.search(r':=\s*by\s*$', item['decl']):
        raise ValueError(f'Item {item_id}: expected a tactic-proof declaration ending in := by')
    return item


def metadata(item: dict) -> dict:
    return {field: item[field] for field in ('index', 'repo', 'name', 'label', 'file')}


def export_item(item: dict, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    context = item['context']
    candidate = item['decl'] + '\n  sorry\n'
    (destination / 'Context.lean').write_text(context, encoding='utf-8')
    (destination / 'Candidate.lean').write_text(candidate, encoding='utf-8')
    (destination / 'informal_statement.md').write_text(item['informal_stmt'], encoding='utf-8')
    (destination / 'informal_proof.md').write_text(item['informal_proof'], encoding='utf-8')
    manifest = {
        'schema_version': 1, 'mode': 'easy', 'task': 'proof', 'item': metadata(item),
        'decl': item['decl'], 'context_sha256': digest(context),
        'initial_candidate_sha256': digest(candidate),
        'informal_stmt': item['informal_stmt'], 'informal_proof': item['informal_proof'],
    }
    write_json(destination / 'item.json', manifest)
    return manifest


def mask_lean_literals(text: str) -> str:
    output = list(text)
    position = 0
    depth = 0
    quoted = False
    line_comment = False
    while position < len(text):
        pair = text[position:position + 2]
        character = text[position]
        if line_comment:
            if character == '\n':
                line_comment = False
            else:
                output[position] = ' '
        elif depth:
            if pair == '/-':
                depth += 1
                output[position:position + 2] = '  '
                position += 1
            elif pair == '-/':
                depth -= 1
                output[position:position + 2] = '  '
                position += 1
            elif character != '\n':
                output[position] = ' '
        elif quoted:
            if character != '\n':
                output[position] = ' '
            if character == '\\' and position + 1 < len(text):
                position += 1
                output[position] = ' '
            elif character == '"':
                quoted = False
        elif pair == '--':
            line_comment = True
            output[position:position + 2] = '  '
            position += 1
        elif pair == '/-':
            depth = 1
            output[position:position + 2] = '  '
            position += 1
        elif character == '"':
            quoted = True
            output[position] = ' '
        position += 1
    if depth or quoted:
        raise ValueError('Unterminated Lean comment or string')
    return ''.join(output)
