"""Shared evidence policy for native v2 and Group v4; legacy records stay unchanged.

This module observes files only. It never executes project code or edits evidence.
Hashes and raw locators establish consistency, not proof of human/agent identity.
The host must supply a real independent reviewer and a real user approval.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path, PurePosixPath

NEW_CONTRACTS = {'megin-repo-quality-contract/v2', 'megin-quality-contract/v4'}
SCAN_EXCLUSIONS = ('docs/work', '.megin', '.agents/skills')


def is_link(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0)
                                        & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
    except FileNotFoundError:
        return False


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'duplicate JSON key: {key}')
        result[key] = value
    return result


def read_json(path):
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    require(isinstance(value, dict), 'evidence must be a JSON object')
    return value


def path_inside(root, relative):
    require(isinstance(relative, str) and relative and '\\' not in relative,
            'evidence path must be canonical and relative')
    pure = PurePosixPath(relative)
    require(not pure.is_absolute() and pure.as_posix() == relative
            and not any(p in ('.', '..') or ':' in p for p in pure.parts), 'unsafe evidence path')
    path = root / relative
    require(path.resolve().is_relative_to(root.resolve()), 'evidence escapes selected root')
    for item in (path, *path.parents):
        if item == root:
            break
        require(not is_link(item), 'evidence cannot traverse links')
    return path


def reference(root, work_id, records, value):
    require(isinstance(value, dict), 'missing hashed evidence reference')
    relative = value.get('path')
    require(relative in records and isinstance(relative, str)
            and relative.startswith(f'docs/work/{work_id}/evidence/'), 'undeclared evidence reference')
    path = path_inside(root, relative)
    require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == value.get('sha256'),
            f'evidence missing or digest changed: {relative}')
    return path


def raw_claims(root, work_id, records, value, expected):
    path = reference(root, work_id, records, value)
    lines = path.read_text(encoding='utf-8').splitlines()
    claims = value.get('claims')
    require(isinstance(claims, dict), 'raw claim locators missing')
    for key, text in expected.items():
        locator = claims.get(key, {})
        number = locator.get('line')
        require(type(number) is int and 0 < number <= len(lines)
                and locator.get('text') == text and lines[number - 1] == text,
                f'raw {key} claim differs from structured evidence')
    return lines


def validate_contract(root, work_id, fields, contract, records, *, live=False, historical=False):
    if contract.get('schema') not in NEW_CONTRACTS:
        return
    from functional_inventory import scan, validate_report
    revision = contract.get('requirements_revision')
    require(isinstance(revision, str) and re.fullmatch(r'req-[a-z0-9-]+', revision)
            and fields.get('requirements_revision') == revision, 'requirements revision mismatch')
    require(contract.get('max_corrections') == 3, 'new work requires exactly three correction attempts')
    require(isinstance(contract.get('scenario_ids'), list) and contract['scenario_ids']
            and isinstance(contract.get('behavior_trace'), list) and contract['behavior_trace'],
            'new work requires scenario traceability')
    for row in contract['behavior_trace']:
        require(all(isinstance(row.get(k), str) and row[k].strip() for k in ('given', 'when', 'then')),
                'BDD scenario needs Given, When and Then')
        require(isinstance(row.get('requirement_ids'), list) and row['requirement_ids']
                and all(isinstance(x, str) and x.startswith('REQ-') for x in row['requirement_ids']),
                'scenario needs requirement IDs')
    requirements_path = f'docs/work/{work_id}/requirements.md'
    require(contract.get('requirements_ref') == requirements_path
            and hashlib.sha256(path_inside(root, requirements_path).read_bytes()).hexdigest() == contract.get('requirements_sha256'),
            'approved requirements content changed')
    review_records = contract.get('review_records')
    expected_records = [f'docs/work/{work_id}/evidence/review-attempt-{i}.json' for i in range(4)]
    require(review_records == expected_records and all(x in records for x in review_records),
            'predeclare initial review and three append-only correction records')
    discovery_ref = contract.get('discovery_ref')
    require(isinstance(discovery_ref, dict) and discovery_ref.get('sha256') == contract.get('discovery_sha256'),
            'discovery digest must be frozen in the contract')
    discovery = read_json(reference(root, work_id, records, discovery_ref))
    require((discovery.get('schema'), discovery.get('work_id'), discovery.get('requirements_revision')) ==
            ('megin-discovery/v1', work_id, revision), 'discovery identity mismatch')
    for key in ('problem', 'audience', 'outcome', 'acceptance'):
        require(isinstance(discovery.get(key), str) and discovery[key].strip(), f'discovery {key} missing')
    for key in ('facts', 'assumptions', 'requirements', 'technical_suggestions', 'decisions', 'blockers'):
        require(isinstance(discovery.get(key), list), f'discovery {key} must be separate')
    require(not discovery['blockers'], 'unresolved discovery blockers')
    require(all(isinstance(x, dict) and x.get('status') in ('confirmed', 'nonblocking')
                and isinstance(x.get('reason'), str) and x['reason'].strip()
                for x in discovery['assumptions'] + discovery['decisions']),
            'discovery assumptions and decisions need explicit disposition')
    reqs = discovery['requirements']
    ids = [x.get('id') for x in reqs if isinstance(x, dict)]
    require(len(ids) == len(reqs) and ids and len(set(ids)) == len(ids)
            and all(isinstance(x, str) and x.startswith('REQ-') for x in ids), 'requirements missing or duplicate')
    require({x for row in contract['behavior_trace'] for x in row['requirement_ids']} == set(ids),
            'every discovered requirement must have a BDD scenario')
    rows = discovery.get('repositories')
    require(isinstance(rows, list) and all(isinstance(x, dict) for x in rows), 'discovery repositories missing')
    native = contract['schema'] == 'megin-repo-quality-contract/v2'
    paths = [x.get('repo_path') for x in rows]
    require(all(isinstance(x, str) and x for x in paths) and len(set(paths)) == len(paths),
            'discovery repository paths missing or duplicate')
    require(not (live and historical), 'historical discovery cannot authorize live writes')
    if not native:
        require(all('/' not in x and '\\' not in x and ':' not in x and x not in ('.', '..') for x in paths),
                'Group discovery paths must be direct-child repository names')
        require({x['repo_path'] for x in contract['repositories']} <= set(paths),
                'discovery must include every approved delivery repository')
    # A completed receipt verifies the approved discovery, not unrelated Repos
    # created or removed later. Claim and current gates still enumerate the root.
    expected = {'.'} if native else set(paths) if historical else {
        p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()
        and not is_link(p) and (p / '.git').exists()
    }
    require(set(paths) == expected, 'discovery must cover every selected-root repository')
    for row in rows:
        repo = root if row['repo_path'] == '.' else path_inside(root, row['repo_path'])
        inventory = read_json(reference(root, work_id, records, row.get('scan_ref')))
        result = validate_report(inventory, row.get('functional_report'))
        require(result['coverage_complete'], f"functional discovery incomplete: {row['repo_path']}")
        require(all(isinstance(g, dict) and g.get('status') == 'nonblocking'
                    and isinstance(g.get('reason'), str) and g['reason'].strip() for g in result['gaps']),
                'functional gaps need resolution or explicit nonblocking disposition')
        if live:
            current = scan(repo, exclude_paths=SCAN_EXCLUSIONS)
            require(current['snapshot_id'] == inventory['snapshot_id'], 'discovery source snapshot is stale')
    approval = read_json(reference(root, work_id, records, {'path': contract.get('approval_ref'),
                         'sha256': hashlib.sha256(path_inside(root, contract.get('approval_ref')).read_bytes()).hexdigest()}))
    expected_plan = f'docs/work/{work_id}/{contract["plan_version"]}/plan.md'
    plan = path_inside(root, expected_plan)
    require(approval.get('schema') == 'megin-plan-approval/v1'
            and approval.get('actor_kind') == 'human' and approval.get('verdict') == 'APPROVED'
            and approval.get('work_id') == work_id and approval.get('plan_version') == contract['plan_version']
            and approval.get('requirements_revision') == revision
            and approval.get('plan_sha256') == hashlib.sha256(plan.read_bytes()).hexdigest()
            and approval.get('contract_sha256') == canonical_digest(contract), 'explicit current-plan human approval missing')
    require(isinstance(approval.get('response'), str) and approval['response'].strip(), 'literal user approval missing')
    raw_claims(root, work_id, records, approval.get('source'), {
        'work_id': f'- work_id: {work_id}', 'plan_version': f'- plan_version: {contract["plan_version"]}',
        'verdict': '- verdict: APPROVED', 'response': '- response: ' + json.dumps(approval['response'], ensure_ascii=False),
        'plan_sha256': f'- plan_sha256: {approval["plan_sha256"]}',
        'contract_sha256': f'- contract_sha256: {approval["contract_sha256"]}',
    })


def validate_tdd(root, work_id, contract, records, evidence, *, test_reader=None):
    cycles = evidence.get('tdd_cycles')
    require(isinstance(cycles, list) and all(isinstance(x, dict) for x in cycles), 'TDD cycles missing')
    ids = [x.get('scenario_id') for x in cycles]
    require(len(set(ids)) == len(ids) and set(ids) == set(contract['scenario_ids']), 'every BDD scenario needs one red/green cycle')
    traces = {x['scenario_id']: x for x in contract['behavior_trace']}
    checks = {x['id']: x for x in contract['checks']}
    for cycle in cycles:
        trace = traces[cycle['scenario_id']]
        check = checks[trace['check_id']]
        require(check['kind'] == 'test' and cycle.get('check_id') == check['id']
                and cycle.get('assertion') == trace['assertion'], 'TDD assertion/check differs from scenario')
        red, green = cycle.get('red'), cycle.get('green')
        require(isinstance(red, dict) and isinstance(green, dict), 'raw red and green records required')
        require(type(red.get('sequence')) is int and type(green.get('sequence')) is int
                and red['sequence'] < green['sequence'], 'red must precede green')
        require(red.get('test_sha256') == green.get('test_sha256') and isinstance(red.get('test_sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', red['test_sha256']), 'same tests must run in red and green')
        require(red.get('test_path') == green.get('test_path'), 'red/green test paths differ')
        relative = green.get('test_path')
        require(isinstance(relative, str) and '\\' not in relative and relative
                and not PurePosixPath(relative).is_absolute() and PurePosixPath(relative).as_posix() == relative
                and not any(p in ('.', '..') or ':' in p for p in PurePosixPath(relative).parts), 'unsafe TDD test path')
        content = test_reader(relative) if test_reader else path_inside(root, relative).read_bytes()
        digests = {hashlib.sha256(content).hexdigest()}
        if test_reader and b'\r' not in content and b'\0' not in content:
            # Git may store LF after tests ran with Windows CRLF. Compare only
            # those two standard representations of the delivered regular blob.
            digests.add(hashlib.sha256(content.replace(b'\n', b'\r\n')).hexdigest())
        require(green['test_sha256'] in digests,
                'TDD test evidence is stale')
        for phase, run in (('red', red), ('green', green)):
            require(all(type(run.get(k)) is int and run[k] >= 0 for k in ('executed', 'failed', 'skipped'))
                    and run['executed'] > 0 and run['skipped'] == 0 and type(run.get('exit_code')) is int,
                    'TDD needs executed tests, no skips and an exit code')
            require(run.get('command') == check['command'] and run.get('cwd') == check.get('cwd', '.'),
                    'TDD command/cwd differs from approved check')
            expected = {'command': f'Command: {run["command"]}', 'cwd': f'Working directory: {run["cwd"]}',
                        'exit_code': f'Exit code: {run["exit_code"]}',
                        'counts': f'Tests: {run["executed"]} executed, {run["failed"]} failed, {run["skipped"]} skipped',
                        'test_sha256': f'Test SHA-256: {run["test_sha256"]}',
                        'sequence': f'Sequence: {run["sequence"]}'}
            if phase == 'red':
                require(run['exit_code'] != 0 and run['failed'] > 0 and run.get('failure_kind') == 'assertion',
                        'red must fail the intended assertion, not setup/import/environment')
                expected['assertion'] = trace['assertion']
            else:
                require(run['exit_code'] == 0 and run['failed'] == 0, 'green did not pass')
            raw_claims(root, work_id, records, run.get('output'), expected)


def validate_reviews(root, work_id, contract, records, evidence, snapshot, gate, *, pending=None, allow_exhausted=False):
    history = evidence.get('review_history')
    require(isinstance(history, list) and len(history) <= 4, 'review history must contain initial review plus at most three corrections')
    on_disk = [p for p in contract['review_records'] if path_inside(root, p).is_file()
               or pending is not None and p == pending[0]['path']]
    require(on_disk == contract['review_records'][:len(on_disk)]
            and [x.get('path') for x in history if isinstance(x, dict)] == on_disk,
            'review history cannot discard or skip recorded attempts on resume')
    previous, contexts, latest = None, set(), None
    writer = evidence.get('writer', {}).get('context')
    for number, ref in enumerate(history):
        attempt = pending[1] if pending is not None and ref == pending[0] else read_json(reference(root, work_id, records, ref))
        require(attempt.get('schema') == 'megin-review-attempt/v1' and attempt.get('round') == number
                and attempt.get('previous_sha256') == previous and attempt.get('work_id') == work_id
                and isinstance(attempt.get('requirements_revision'), str), 'review history chain changed')
        require(latest is None or latest['verdict'] != 'APPROVED' or latest['snapshot'] != attempt.get('snapshot'),
                'an approved snapshot does not need another review')
        context = attempt.get('context')
        require(isinstance(context, str) and context and context != writer and context not in contexts
                and attempt.get('reviewer_kind') == 'independent_readonly'
                and isinstance(attempt.get('invocation_id'), str) and attempt['invocation_id'] == context,
                'each attempt needs a fresh independent read-only reviewer invocation')
        require(attempt.get('verdict') in ('APPROVED', 'CHANGES_REQUESTED')
                and isinstance(attempt.get('snapshot'), str) and re.fullmatch(r'[0-9a-f]{64}', attempt['snapshot']),
                'review verdict/snapshot invalid')
        if number:
            reference(root, work_id, records, attempt.get('correction_ref'))
        raw_claims(root, work_id, records, attempt.get('source'), {
            'context': f'- context: {context}', 'verdict': f'- verdict: {attempt["verdict"]}',
            'snapshot': f'- snapshot: {attempt["snapshot"]}', 'round': f'- round: {number}',
            'previous_sha256': f'- previous_sha256: {previous}',
        })
        previous, latest = ref['sha256'], attempt
        contexts.add(context)
    if latest is not None:
        require(all(evidence.get('review', {}).get(k) == latest[k] for k in ('context', 'verdict', 'snapshot', 'source')),
                'latest review summary differs from append-only history')
        require(allow_exhausted or not (len(history) == 4 and latest['verdict'] != 'APPROVED'),
                'three corrections exhausted; preserve evidence and report incomplete')
    if gate in ('acceptance', 'delivery', 'completion'):
        require(latest is not None and latest['verdict'] == 'APPROVED' and latest['snapshot'] == snapshot
                and latest['requirements_revision'] == contract['requirements_revision'],
                'current snapshot needs an approved independent review')


def validate_evidence(root, work_id, fields, contract, records, evidence, snapshot, gate, reasons, *, test_reader=None):
    if contract.get('schema') not in NEW_CONTRACTS:
        return
    try:
        require(test_reader is None or gate == 'completion', 'historical test reader is only valid for completion')
        validate_contract(root, work_id, fields, contract, records, historical=test_reader is not None)
        require(evidence.get('requirements_revision') == contract['requirements_revision'], 'evidence requirements revision is stale')
        validate_tdd(root, work_id, contract, records, evidence, test_reader=test_reader)
        validate_reviews(root, work_id, contract, records, evidence, snapshot, gate)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        reasons.append(f'v2/v4 evidence policy: {exc}')


def evidence_paths(value):
    """Collect every nested raw reference for immutable delivery export."""
    result = set()
    if isinstance(value, dict):
        if isinstance(value.get('path'), str) and isinstance(value.get('sha256'), str):
            result.add(value['path'])
        for child in value.values():
            result.update(evidence_paths(child))
    elif isinstance(value, list):
        for child in value:
            result.update(evidence_paths(child))
    return result
