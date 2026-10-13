"""Read-only source inventory and coverage validation; no target code execution."""
from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path
from dataclasses import replace

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

SCHEMA = 'codebase-functional-exploration/v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def scan(root: Path, profile: str = 'target', exclude_paths: tuple[str, ...] = ()) -> dict:
    from project_scanner import cli_settings, scan_project
    root = root.resolve()
    require(root.is_dir(), 'source root must be a directory')
    settings = cli_settings(root, profile)
    if exclude_paths:
        settings = replace(settings, exclude_paths=settings.exclude_paths + exclude_paths)
    inventory = scan_project(root, settings, ())
    if exclude_paths:
        volatile = {'binary_or_generated', 'export_output', 'wiki_knowledge_layer'}
        def stable(rows):
            return [r for r in rows if r.get('reason') not in volatile
                    and not any(r['path'] == p or r['path'].startswith(p.rstrip('/') + '/') for p in exclude_paths)]
        material = {
            'schema': SCHEMA, 'scan_profile': profile, 'explicit_exclusions': sorted(exclude_paths),
            'included': inventory['included'], 'entrypoints': inventory['entrypoint_candidates'],
            'excluded': stable(inventory['excluded']), 'skipped': stable(inventory['skipped']),
            'excluded_roots': [{'path': r['path'], 'reason': r['reason']} for r in stable(inventory['excluded_roots'])],
            'read_issues': inventory['read_issues'],
        }
        inventory['snapshot_id'] = 'sha256:' + hashlib.sha256(json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        inventory['explicit_exclusions'] = list(exclude_paths)
    return inventory


def unique_rows(value, key):
    require(isinstance(value, list) and all(isinstance(row, dict) for row in value), f'{key} rows must be a list')
    ids = [row.get(key) for row in value]
    require(all(isinstance(item, str) and item for item in ids) and len(set(ids)) == len(ids), f'duplicate or missing {key}')
    return dict(zip(ids, value))


def validate_report(inventory: dict, report: dict) -> dict:
    require(isinstance(report, dict) and report.get('schema') == SCHEMA, 'unknown functional exploration schema')
    require(report.get('snapshot_id') == inventory.get('snapshot_id'), 'functional inventory snapshot changed')
    files = unique_rows(inventory['included'], 'path')
    functions = unique_rows(report.get('functions'), 'id')
    for identifier, function in functions.items():
        require(identifier.startswith('FUNC-'), 'function ID must start with FUNC-')
        for key in ('outcome', 'flow'):
            require(isinstance(function.get(key), str) and function[key].strip(), f'{identifier}: {key} missing')
        require(isinstance(function.get('dependencies'), list), f'{identifier}: dependencies missing')
        paths = function.get('paths')
        require(isinstance(paths, list) and paths and all(isinstance(p, str) and p in files for p in paths), f'{identifier}: source paths invalid')
    coverage = unique_rows(report.get('coverage'), 'path')
    require(set(coverage) == set(files), 'functional coverage must include every current source file')
    incomplete = bool(inventory.get('read_issues'))
    for path, row in coverage.items():
        require(row.get('sha256') == files[path]['sha256'], f'{path}: source digest changed')
        incomplete |= validate_disposition(row, functions, path)
        for identifier in row.get('function_ids', []):
            require(path in functions[identifier]['paths'], f'{path}: function does not cite this source')
    candidates = unique_rows(inventory.get('entrypoint_candidates', []), 'entrypoint_id')
    entries = unique_rows(report.get('entrypoints'), 'entrypoint_id')
    require(set(entries) == set(candidates), 'functional coverage must reconcile every entrypoint candidate')
    for identifier, row in entries.items():
        incomplete |= validate_disposition(row, functions, identifier)
        path = candidates[identifier]['path']
        for function_id in row.get('function_ids', []):
            require(path in functions[function_id]['paths'], f'{identifier}: entrypoint source absent from function')
    gaps = report.get('gaps')
    require(isinstance(gaps, list), 'functional gaps must be explicit')
    coverage_complete = not incomplete
    incomplete |= bool(gaps)
    return {'schema': SCHEMA, 'snapshot_id': inventory['snapshot_id'], 'complete': not incomplete, 'coverage_complete': coverage_complete,
            'function_count': len(functions), 'file_count': len(files), 'entrypoint_count': len(entries),
            'gaps': gaps, 'read_issues': inventory.get('read_issues', []),
            'excluded': inventory.get('excluded', []), 'excluded_roots': inventory.get('excluded_roots', [])}


def validate_disposition(row, functions, label):
    status = row.get('status')
    require(status in ('checked', 'partial', 'not-checked', 'not-applicable'), f'{label}: invalid coverage status')
    require(isinstance(row.get('reason'), str) and row['reason'].strip(), f'{label}: disposition reason missing')
    ids = row.get('function_ids')
    require(isinstance(ids, list) and all(isinstance(item, str) and item in functions for item in ids)
            and len(set(ids)) == len(ids), f'{label}: invalid function association')
    require(status != 'checked' or bool(ids), f'{label}: checked source needs a function association')
    return status in ('partial', 'not-checked')
