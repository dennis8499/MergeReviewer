"""Repo v3 / Group v6: approved remote-master work and feature-only delivery.

Gates only inspect evidence. prepare/deliver/claim/release are explicit mutations.
Human approval and reviewer independence must come from real host contexts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True

import quality_gate as q
import quality_policy as p
import workspace_context as context
import workspace_lock as locks
from git_snapshot import group_index_entries, group_repo_entries, validate_handoff_contract

PHASES = {'requirements', 'architecture', 'planning', 'approval', 'implementation',
          'review', 'verification', 'acceptance', 'delivery'}
STATUSES = {'active', 'awaiting_user', 'awaiting_review', 'blocked', 'needs_revision', 'complete'}
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def schemas(group, legacy=False):
    version = 5 if legacy else 6
    return ((f'megin-skills-workflow/v{version}', f'megin-quality-contract/v{version}', f'megin-quality-evidence/v{version}')
            if group else ('megin-repo-workflow/v3', 'megin-repo-quality-contract/v3', 'megin-repo-quality-evidence/v3'))


def load_contract(root, work_id, group=False):
    root = context.group_root(root) if group else context.repository(root)
    p.require(isinstance(work_id, str) and q.WORK_ID_PATTERN.fullmatch(work_id), 'invalid Work ID')
    work = p.path_inside(root, f'docs/work/{work_id}')
    fields = q.workflow_fields(work / 'workflow.md')
    version = fields.get('plan_version', '')
    p.require(q.PLAN_VERSION_PATTERN.fullmatch(version), 'invalid plan version')
    contract = p.read_json(p.path_inside(root, f'docs/work/{work_id}/{version}/quality-contract.json'))
    legacy = group and fields.get('schema') == 'megin-skills-workflow/v5' and fields.get('status') == 'complete'
    workflow_schema, contract_schema, _ = schemas(group, legacy)
    p.require(fields.get('schema') == workflow_schema and contract.get('schema') == contract_schema,
              'workflow and quality contract versions must match')
    p.require(fields.get('work_id') == work_id and contract.get('work_id') == work_id
              and contract.get('plan_version') == version, 'workflow/contract identity mismatch')
    p.require(fields.get('phase') in PHASES and fields.get('status') in STATUSES, 'invalid workflow phase/status')
    p.require(fields.get('delivery_mode') == contract.get('delivery_mode') == 'feature_handoff',
              'new work uses feature_handoff delivery')
    records_list = contract.get('process_records')
    p.require(isinstance(records_list, list) and records_list and all(isinstance(x, str) for x in records_list),
              'process records must be predeclared')
    records = set(records_list)
    p.require(len(records) == len(records_list), 'duplicate process records')
    for path in records:
        q.evidence_file(root, work_id, path)
        p.path_inside(root, path)
    for key in ('quality_ref', 'delivery_ref'):
        p.require(contract.get(key) in records and fields.get(key) == contract[key], 'workflow evidence reference mismatch')
    p.require(isinstance(contract.get('skills_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', contract['skills_sha256']),
              'approved Skills fingerprint missing')
    available = context.repositories(root) if group else {'.': root}
    rows = contract.get('repositories') if group else [{**contract, 'repo_path': '.'}]
    p.require(isinstance(rows, list) and rows and all(isinstance(x, dict) for x in rows), 'selected repositories missing')
    repos, names = [], set()
    for row in rows:
        name = row.get('repo_path')
        p.require(isinstance(name, str) and name in available and name not in names, 'invalid or duplicate selected repository')
        names.add(name)
        p.require(row.get('base_branch') == 'master' and isinstance(row.get('base_commit'), str)
                  and SHA.fullmatch(row['base_commit']), 'new work requires a pinned remote master')
        p.require(isinstance(row.get('remote_name'), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', row['remote_name'])
                  and isinstance(row.get('remote_url'), str) and row['remote_url']
                  and context.remote_url(row['remote_url']) == row['remote_url'], 'credential-free remote identity required')
        p.require(row.get('feature_branch') == f'feature/{work_id}', 'feature branch must match Work ID')
        paths = row.get('allowed_paths')
        p.require(isinstance(paths, list) and paths and all(q.valid_allowed_path(x) for x in paths), 'approved paths missing or unsafe')
        repos.append({**row, 'repo': available[name]})
    if not group:
        for key in ('base_branch', 'base_commit', 'feature_branch', 'remote_name', 'remote_url'):
            p.require(fields.get(key) == contract.get(key), f'workflow {key} differs from contract')
    else:
        p.require(fields.get('repositories') == ','.join(row['repo_path'] for row in rows), 'workflow selected Repo list differs')
    verification = verification_repositories(root, contract, repos, group)
    check_repos = names | {x['repo_path'] for x in verification}
    checks = contract.get('checks')
    p.require(isinstance(checks, list) and checks and all(isinstance(x, dict) for x in checks), 'approved checks missing')
    ids = [x.get('id') for x in checks]
    p.require(all(isinstance(x, str) and x for x in ids) and len(set(ids)) == len(ids), 'duplicate or invalid check IDs')
    for item in checks:
        p.require(item.get('kind') in ('test', 'command') and isinstance(item.get('command'), str) and item['command']
                  and item.get('cwd') in ({'.'} | check_repos), 'invalid approved check or cwd')
    for row in verification:
        p.require(set(row['check_ids']) <= set(ids), 'verification Repo references unknown check IDs')
    validate_handoff_contract(contract, list(names), set(ids))
    from behavior_trace import validate_trace
    validate_trace(contract)
    for probe in contract.get('runner_preflight', []):
        p.require(probe['cwd'] in ({'.'} | check_repos), 'runner cwd must identify the workspace or a selected/verification Repo')
    for trace in contract.get('behavior_trace', []):
        for relative in trace['implementation_paths']:
            q.canonical_relative(relative)
            p.require(not group or relative.split('/')[0] in names, 'scenario path must belong to a selected Repo')
    fixed_inputs(root, contract, repos, group)
    return fields, contract, repos, records


def verification_repositories(root, contract, repos, group):
    if not group or contract.get('schema') != 'megin-quality-contract/v6':
        return []
    rows = contract.get('verification_repositories')
    p.require(isinstance(rows, list) and all(isinstance(x, dict) for x in rows), 'verification_repositories must be explicit')
    available, selected, seen, result = context.repositories(root), {x['repo_path'] for x in repos}, set(), []
    for row in rows:
        name = row.get('repo_path')
        p.require(isinstance(name, str) and name in available and name not in selected and name not in seen,
                  'verification Repo must be a distinct unchanged Group repository')
        seen.add(name)
        p.require(isinstance(row.get('base_commit'), str) and SHA.fullmatch(row['base_commit']), 'verification Repo fixed commit missing')
        ids = row.get('check_ids')
        p.require(isinstance(ids, list) and ids and all(isinstance(x, str) and x for x in ids)
                  and len(ids) == len(set(ids)), 'verification Repo needs explicit compatibility check IDs')
        p.require(not any(k in row for k in ('feature_branch', 'allowed_paths', 'remote_name', 'remote_url')),
                  'verification Repo must not receive delivery or write authority')
        result.append({**row, 'repo': available[name]})
    return result


def fixed_inputs(root, contract, repos, group):
    from verification_inputs import validate_inputs
    inputs = contract.get('verification_inputs')
    ids = {x['id'] for x in contract['checks']}
    if not group:
        return validate_inputs(root, inputs, ids)
    if inputs is None:
        return []
    p.require(isinstance(inputs, list), 'verification inputs must be an array')
    available = {x['repo_path']: x['repo'] for x in repos + verification_repositories(root, contract, repos, group)}
    result, seen = [], set()
    for item in inputs:
        p.require(isinstance(item, dict) and item.get('repo_path') in available, 'fixed input Repo is not selected or approved for verification')
        repo_path = item['repo_path']
        value = {k: v for k, v in item.items() if k != 'repo_path'}
        for normalized in validate_inputs(available[repo_path], [value], ids):
            for file in normalized['files']:
                key = repo_path, normalized['commit'], file['path']
                p.require(key not in seen, 'duplicate fixed input')
                seen.add(key)
            result.append({'repo_path': repo_path, **normalized})
    return result


def input_entries(inputs):
    from verification_inputs import snapshot_entries
    entries = []
    for item in inputs:
        values = snapshot_entries([item])
        if 'repo_path' in item:
            values = [{**v, 'path': '@verification/' + item['repo_path'] + '/' + v['path'][14:]} for v in values]
        entries.extend(values)
    return entries


def protected(root, work_id, records, group):
    if not group:
        return []
    result = []
    work = root / 'docs/work' / work_id
    for path in sorted(work.rglob('*')):
        p.require(not p.is_link(path), 'work records cannot traverse links')
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in records or relative == f'docs/work/{work_id}/workflow.md':
            continue
        result.append({'path': '@group/' + relative, 'mode': '100644',
                       'content': hashlib.sha256(path.read_bytes()).hexdigest()})
    return result


def infrastructure(relative):
    return relative.startswith(('.agents/skills/', '.megin/'))


def product_entries(repo, root, work_id, records, group, kind='worktree', commit=None):
    if kind == 'worktree':
        entries = group_repo_entries(repo)
    elif kind == 'index':
        entries = group_index_entries(repo)
    else:
        entries = []
        for raw in q.git(repo, 'ls-tree', '-r', '-z', '--full-tree', commit).split(b'\0'):
            if raw:
                meta, name = raw.split(b'\t', 1)
                mode, _kind, blob = meta.decode().split()
                entries.append({'path': name.decode('utf-8'), 'mode': mode, 'content': blob})
    return [x for x in entries if not infrastructure(x['path'])
            and (group or not q.process_record(x['path'], work_id, records))]


def snapshot(root, work_id, group=False, kind='worktree', commits=None):
    root = Path(root).resolve()
    _, contract, repos, records = load_contract(root, work_id, group)
    all_entries, details = [], {}
    for row in sorted(repos, key=lambda x: x['repo_path']):
        name, repo = row['repo_path'], row['repo']
        entries = product_entries(repo, root, work_id, records, group, kind, (commits or {}).get(name))
        details[name] = {'product_sha256': q.digest_entries(entries), 'path_count': len(entries)}
        if kind != 'commit':
            details[name].update(branch=context.git(repo, 'branch', '--show-current'), head=context.git(repo, 'rev-parse', 'HEAD'))
        all_entries.extend([{**x, 'path': name + '/' + x['path']} for x in entries] if group else entries)
    for row in verification_repositories(root, contract, repos, group):
        entries = product_entries(row['repo'], root, work_id, records, True, 'commit', row['base_commit'])
        all_entries.extend({**x, 'path': '@verification-repositories/' + row['repo_path'] + '/' + x['path']} for x in entries)
    inputs = fixed_inputs(root, contract, repos, group)
    all_entries.extend(input_entries(inputs))
    all_entries.extend(protected(root, work_id, records, group))
    version = contract['schema'].rsplit('/', 1)[1]
    return {'schema': 'megin-quality-snapshot/' + version if group else 'megin-repo-quality-snapshot/v3',
            'work_id': work_id, 'product_sha256': q.digest_entries(all_entries), 'repositories': details,
            'path_count': len(all_entries), 'verification_inputs_sha256': p.canonical_digest(inputs)}


def fingerprint(root):
    from repo_workspace import fingerprint as calculate
    return calculate(root)['sha256']


def approved(root, work_id, group=False, live=False, historical=False):
    fields, contract, repos, records = load_contract(root, work_id, group)
    p.require(historical or not group or contract['schema'] == 'megin-quality-contract/v6',
              'legacy Group work requires its original installed package; completed v5 supports historical export only')
    p.validate_contract(root, work_id, fields, contract, records, live=live, historical=historical)
    if not historical:
        p.require(fingerprint(root) == contract['skills_sha256'], 'installed Skills fingerprint changed')
    return fields, contract, repos, records


def validate_record(root, work_id, group=False):
    fields, _, repos, _ = approved(Path(root).resolve(), work_id, group)
    return {'ok': True, 'work_id': work_id, 'phase': fields['phase'], 'repositories': [x['repo_path'] for x in repos]}


def _writer_and_checks(root, work_id, fields, contract, records, evidence, digest, gate, reasons, test_reader=None):
    writer = evidence.get('writer', {})
    p.require(isinstance(writer, dict) and isinstance(writer.get('context'), str) and writer['context']
              and writer.get('snapshot') == digest, 'writer does not bind current snapshot')
    q.cited_claims(root, work_id, records, writer.get('source'), 'writer', reasons,
                   {'context': '- context: ' + writer['context'], 'snapshot': '- snapshot: ' + digest})
    q.validate_recorded_checks(root, work_id, records, contract['checks'], evidence, digest, reasons,
                              require_pass=gate != 'review')
    p.validate_evidence(root, work_id, fields, contract, records, evidence, digest, gate, reasons,
                        test_reader=test_reader)
    if contract.get('verification_inputs') is not None:
        group = contract['schema'].startswith('megin-quality-contract/')
        _, _, repos, _ = load_contract(root, work_id, group)
        inputs = fixed_inputs(root, contract, repos, group)
        expected = p.canonical_digest(inputs)
        bound = {identifier for item in inputs for identifier in item['check_ids']}
        for result in evidence.get('checks', []):
            if result.get('id') in bound:
                p.require(result.get('verification_inputs_sha256') == expected, 'fixed input evidence changed')
                q.cited_claims(root, work_id, records, result.get('output'), 'fixed inputs', reasons,
                               {'verification_inputs': 'Verification inputs SHA-256: ' + expected})
    p.require(isinstance(evidence.get('sources'), list), 'supporting evidence sources missing')
    for source in evidence['sources']:
        q.cited_file(root, work_id, records, source, 'supporting evidence', reasons)


def _human(root, work_id, records, evidence, digest, reasons):
    value = evidence.get('acceptance', {})
    p.require(isinstance(value, dict) and value.get('actor_kind') == 'human' and value.get('work_id') == work_id
              and value.get('verdict') == 'ACCEPTED' and value.get('snapshot') == digest
              and isinstance(value.get('version'), str) and value['version']
              and isinstance(value.get('response'), str) and value['response'].strip()
              and isinstance(value.get('timestamp'), str) and value['timestamp'], 'explicit current-snapshot human acceptance missing')
    q.cited_claims(root, work_id, records, value.get('source'), 'human acceptance', reasons,
                   {k: f'- {k}: {value[k]}' for k in ('work_id', 'version', 'snapshot', 'verdict', 'timestamp')} |
                   {'response': '- response: ' + json.dumps(value['response'], ensure_ascii=False)})


def allowed(relative, rules):
    return any(relative.startswith(x) if x.endswith('/') else relative == x for x in rules)


def check(root, work_id, gate, group=False, historical=False, require_staged=True):
    root = Path(root).resolve()
    reasons = []
    try:
        fields, contract, repos, records = approved(root, work_id, group, historical=historical)
        p.require(gate in {'review', 'acceptance', 'delivery', 'completion'}, 'invalid gate')
        phases = {'review': {'implementation', 'review'}, 'acceptance': {'verification', 'acceptance'},
                  'delivery': {'delivery'}, 'completion': {'delivery'}}
        p.require(fields['phase'] in phases[gate], 'gate does not match workflow phase')
        p.require(fields['status'] in {'active', 'awaiting_review', 'awaiting_user', 'complete'}
                  and (fields['status'] != 'complete' or gate == 'completion'), 'workflow cannot progress in this status')
        evidence = p.read_json(p.path_inside(root, contract['quality_ref']))
        p.require((evidence.get('schema'), evidence.get('work_id'), evidence.get('plan_version')) ==
                  (schemas(group, contract['schema'] == 'megin-quality-contract/v5')[2], work_id, contract['plan_version']), 'quality evidence identity mismatch')
        delivery = p.read_json(p.path_inside(root, contract['delivery_ref'])) if gate == 'completion' else None
        if delivery is not None and fields['status'] == 'complete':
            p.require(delivery.get('status') == 'complete' and delivery.get('completion_ok') is True
                      and delivery.get('completion_snapshot') == evidence.get('snapshot'), 'completed delivery record is incomplete')
        commits = {x['repo_path']: x['feature_commit'] for x in delivery.get('repositories', [])} if delivery else None
        current = snapshot(root, work_id, group, 'commit' if historical else 'worktree', commits)
        digest = current['product_sha256']
        p.require(evidence.get('snapshot') == digest, 'product or approved-contract snapshot changed')
        if group:
            from impact_policy import validate_confirmation
            validate_confirmation(root, work_id, contract, repos, records, evidence, digest,
                                  commits if gate == 'completion' else None)

        def test_reader(relative):
            if group:
                name, separator, path = relative.partition('/')
                p.require(separator and name in commits, 'historical TDD test Repo is missing')
                repo = next(x['repo'] for x in repos if x['repo_path'] == name)
            else:
                name, path, repo = '.', relative, root
            rows = q.git(repo, 'ls-tree', '-z', commits[name], '--', path).split(b'\0')
            p.require(len(rows) == 2 and rows[0], 'delivered TDD test missing')
            metadata, filename = rows[0].split(b'\t', 1)
            mode, kind, _ = metadata.decode().split()
            p.require(kind == 'blob' and mode in ('100644', '100755') and filename.decode() == path,
                      'delivered TDD test must be a regular blob')
            return q.git(repo, 'cat-file', 'blob', commits[name] + ':' + path)

        _writer_and_checks(root, work_id, fields, contract, records, evidence, digest, gate, reasons,
                           test_reader if gate == 'completion' else None)
        if not historical and gate != 'completion':
            wanted = locks.owner(root, work_id, evidence['writer']['context'], contract['skills_sha256'])
            locks.check_all(root, repos, group, wanted)
        for row in repos:
            repo, name = row['repo'], row['repo_path']
            if not historical:
                p.require(context.git(repo, 'branch', '--show-current') == row['feature_branch'], 'current feature branch changed')
                head = context.git(repo, 'rev-parse', 'HEAD')
                if gate != 'completion':
                    # A partial-delivery resume can already contain accepted feature commits.
                    p.require(head == row['base_commit'] or gate == 'delivery' and
                              context.git(repo, 'show', '-s', '--format=%P', head) == row['base_commit'],
                              'feature branch is not based on approved remote master')
                paths = q.changed_paths(repo, row['base_commit'])
                paths = {x for x in paths if not infrastructure(x) and (group or not q.process_record(x, work_id, records))}
                p.require(all(allowed(x, row['allowed_paths']) for x in paths), 'changed path outside approved scope')
                staged = q.path_set(repo, 'diff', '--cached', '--name-only', 'HEAD')
                if gate in {'review', 'acceptance'}:
                    p.require(not staged, 'files staged before human acceptance')
                p.require(all(allowed(x, row['allowed_paths']) or not group and q.process_record(x, work_id, records)
                              for x in staged), 'unapproved files are staged; preserve the existing index')
            if not historical and gate in {'delivery', 'completion'}:
                context.master(repo, row['remote_name'], row['remote_url'], row['base_commit'])
        if gate in {'delivery', 'completion'}:
            _human(root, work_id, records, evidence, digest, reasons)
        result = {'gate': gate, 'ok': not reasons, 'snapshot': digest, 'reasons': reasons,
                  'repositories': current['repositories']}
        if gate == 'delivery' and require_staged:
            staged = snapshot(root, work_id, group, 'index')
            p.require(staged['product_sha256'] == digest, 'staged bytes differ from accepted snapshot')
            result['staged_snapshot'] = staged['product_sha256']
        if gate == 'completion':
            _completion(root, work_id, contract, repos, records, delivery, current, group, historical, reasons)
            result.update(ok=not reasons, completion_ok=not reasons,
                          feature_commit=commits.get('.') if not group else None)
        return result
    except (ValueError, OSError, KeyError, TypeError, q.InvalidEvidence, subprocess.TimeoutExpired) as exc:
        reasons.append(str(exc))
        return {'gate': gate, 'ok': False, 'completion_ok': False, 'reasons': reasons}


def _completion(root, work_id, contract, repos, records, delivery, current, group, historical, reasons):
    expected_schema = 'megin-delivery-result/v2' if group else 'megin-repo-delivery-result/v2'
    p.require((delivery.get('schema'), delivery.get('work_id'), delivery.get('plan_version'), delivery.get('delivery_mode')) ==
              (expected_schema, work_id, contract['plan_version'], 'feature_handoff'), 'delivery identity mismatch')
    p.require(delivery.get('accepted_snapshot') == current['product_sha256'], 'delivery accepted snapshot changed')
    receipt = delivery.get('delivery_gate', {})
    p.require(receipt.get('exit_code') == 0, 'passing delivery gate receipt missing')
    raw = p.read_json(p.reference(root, work_id, records, receipt.get('output')))
    p.require(raw.get('gate') == 'delivery' and raw.get('ok') is True
              and raw.get('snapshot') == raw.get('staged_snapshot') == current['product_sha256'], 'delivery gate receipt changed')
    rows = delivery.get('repositories')
    p.require(isinstance(rows, list) and [x.get('repo_path') for x in rows] == contract['handoff']['merge_order'],
              'delivery must contain every selected Repo in dependency order')
    by_name = {x['repo_path']: x for x in repos}
    commits = {}
    for record in rows:
        row = by_name[record['repo_path']]
        commit, repo = record.get('feature_commit'), row['repo']
        p.require(isinstance(commit, str) and SHA.fullmatch(commit), 'feature commit missing')
        p.require(record.get('base_commit') == row['base_commit'] and record.get('feature_branch') == row['feature_branch'],
                  'delivery branch identity changed')
        p.require(not record.get('merge_commit') and not delivery.get('merge_commit'), 'feature handoff cannot include a merge commit')
        p.require(context.git(repo, 'show', '-s', '--format=%P', commit).split() == [row['base_commit']],
                  'feature commit must directly descend from approved remote master')
        if not historical:
            p.require(context.git(repo, 'rev-parse', 'refs/heads/' + row['feature_branch']) == commit,
                      'feature branch no longer points to accepted commit')
        for path in q.path_set(repo, 'diff', '--name-only', row['base_commit'], commit):
            if not group and q.process_record(path, work_id, records):
                continue
            p.require(allowed(path, row['allowed_paths']), 'committed path outside approved scope')
        commits[row['repo_path']] = commit
    committed = snapshot(root, work_id, group, 'commit', commits)
    p.require(committed['product_sha256'] == current['product_sha256'], 'feature commit tree differs from accepted snapshot')
    for record in rows:
        name = record['repo_path']
        expected = committed['repositories'][name]['product_sha256']
        p.require(record.get('product_sha256') == expected and
                  raw.get('repositories', {}).get(name, {}).get('product_sha256') == expected,
                  'per-Repo delivery digest differs from committed snapshot')


def claim(root, work_id, writer, group=False):
    root = Path(root).resolve()
    fields, contract, repos, records = approved(root, work_id, group)
    p.require((fields['phase'], fields['status']) == ('implementation', 'active'), 'claim requires approved implementation/active')
    wanted = locks.owner(root, work_id, writer, contract['skills_sha256'])
    existing = all(path.exists() for path in locks.paths(root, repos, group))
    if existing:
        locks.check_all(root, repos, group, wanted)
    else:
        p.validate_contract(root, work_id, fields, contract, records, live=True)
    quality = p.path_inside(root, contract['quality_ref'])
    p.require(quality.exists() or not any(p.path_inside(root, path).exists() for path in contract['review_records']),
              'quality summary missing; recover retained review history before writing')
    if quality.exists():
        evidence = p.read_json(quality)
        p.validate_reviews(root, work_id, contract, records, evidence, evidence.get('snapshot'), 'review')
    return locks.claim(root, repos, group, wanted)


def check_owner(root, work_id, writer, group=False, preparing=False):
    root = Path(root).resolve()
    fields, contract, repos, records = approved(root, work_id, group)
    p.require(fields['phase'] in {'implementation', 'delivery'} and fields['status'] == 'active',
              'product writes require active implementation or delivery')
    quality = p.path_inside(root, contract['quality_ref'])
    p.require(quality.exists() or not any(p.path_inside(root, path).exists() for path in contract['review_records']),
              'quality summary missing; recover retained review history before writing')
    if quality.exists():
        evidence = p.read_json(quality)
        p.validate_reviews(root, work_id, contract, records, evidence, evidence.get('snapshot'), 'review')
    if not preparing:
        for row in repos:
            p.require(context.git(row['repo'], 'branch', '--show-current') == row['feature_branch'],
                      'product writes require the approved feature branch')
            head = context.git(row['repo'], 'rev-parse', 'HEAD')
            p.require(head == row['base_commit'] or fields['phase'] == 'delivery' and
                      context.git(row['repo'], 'show', '-s', '--format=%P', head).split() == [row['base_commit']],
                      'feature branch has an unexpected parent or commit')
    return locks.check_all(root, repos, group, locks.owner(root, work_id, writer, contract['skills_sha256']))


def release(root, work_id, writer, group=False):
    root = Path(root).resolve()
    fields, contract, repos, _ = approved(root, work_id, group)
    p.require(fields['status'] == 'complete' and fields['phase'] == 'delivery', 'release requires completed workflow')
    result = check(root, work_id, 'completion', group)
    p.require(result['ok'], 'completion gate failed; keep locks: ' + '; '.join(result['reasons']))
    return locks.release(root, repos, group, locks.owner(root, work_id, writer, contract['skills_sha256']))


def _product_status(row, work_id, records, group):
    paths = q.path_set(row['repo'], 'diff', '--name-only', 'HEAD') | q.path_set(row['repo'], 'diff', '--cached', '--name-only', 'HEAD')
    paths |= q.path_set(row['repo'], 'ls-files', '--others', '--exclude-standard')
    paths = {x for x in paths if not infrastructure(x)}
    p.require(not q.path_set(row['repo'], 'diff', '--cached', '--name-only', 'HEAD'),
              'existing staged changes; preserve the index before branch preparation')
    if not group:
        prefix = f'docs/work/{work_id}/'
        paths = {x for x in paths if not x.startswith(prefix)}
    return paths


def prepare(root, work_id, writer, group=False):
    root = Path(root).resolve()
    check_owner(root, work_id, writer, group, preparing=True)
    fields, contract, repos, records = load_contract(root, work_id, group)
    p.require(fields['phase'] == 'implementation', 'branch preparation requires implementation phase')
    # Check every target before fetching or creating any branch.
    for row in repos:
        context.master(row['repo'], row['remote_name'], row['remote_url'], row['base_commit'])
        p.require(not _product_status(row, work_id, records, group), 'existing product changes; preserve them before branch preparation')
        if subprocess.run(['git', '-C', str(row['repo']), 'show-ref', '--verify', '--quiet', 'refs/heads/' + row['feature_branch']]).returncode == 0:
            p.require(context.git(row['repo'], 'rev-parse', 'refs/heads/' + row['feature_branch']) == row['base_commit'],
                      'same-name feature branch conflicts with approved base')
    for row in repos:
        proc = subprocess.run(['git', '-C', str(row['repo']), 'fetch', '--no-tags', '--refmap=', row['remote_name'], 'refs/heads/master'],
                              capture_output=True, check=False, timeout=60)
        p.require(proc.returncode == 0, 'cannot fetch remote master')
        p.require(context.git(row['repo'], 'rev-parse', 'FETCH_HEAD') == row['base_commit'], 'remote master moved during fetch')
    # A final whole-workspace check catches changes while fetching another Repo.
    for row in repos:
        context.master(row['repo'], row['remote_name'], row['remote_url'], row['base_commit'])
        # A checkout must not replace the running, approved Skills/configuration
        # or tracked current-work records. Check all targets before switching any.
        governance = ['.agents/skills', '.megin']
        if not group:
            governance.append(f'docs/work/{work_id}')
        changes = q.path_set(row['repo'], 'diff', '--name-only', 'HEAD', row['base_commit'], '--', *governance)
        p.require(not changes, 'branch preparation would replace installed governance or work records; '
                  'preserve them in a compatible workspace before preparing')
    for row in repos:
        check_owner(root, work_id, writer, group, preparing=True)
        repo, branch = row['repo'], row['feature_branch']
        exists = subprocess.run(['git', '-C', str(repo), 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch]).returncode == 0
        # post-checkout hooks may rewrite or stage files before human acceptance.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='megin-frozen-checkout-') as empty_hooks:
            args = ('switch', branch) if exists else ('switch', '-c', branch, row['base_commit'])
            context.git(repo, '-c', 'core.hooksPath=' + empty_hooks, *args)
    return {'prepared': True, 'repositories': [x['repo_path'] for x in repos]}


def save_json(path, value):
    import os
    import tempfile
    from verification_inputs import no_links
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.megin-record-', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write((json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def deliver(root, work_id, writer, group=False):
    root = Path(root).resolve()
    check_owner(root, work_id, writer, group)
    fields, contract, repos, records = load_contract(root, work_id, group)
    p.require(fields['phase'] == 'delivery', 'delivery requires delivery phase')
    evidence = p.read_json(p.path_inside(root, contract['quality_ref']))
    reasons = []
    _human(root, work_id, records, evidence, evidence.get('snapshot'), reasons)
    p.require(not reasons, '; '.join(reasons))
    # Validate all accepted content before staging; no partial acceptance.
    p.require(snapshot(root, work_id, group)['product_sha256'] == evidence.get('snapshot'), 'accepted snapshot changed')
    for row in repos:
        context.master(row['repo'], row['remote_name'], row['remote_url'], row['base_commit'])
    precheck = check(root, work_id, 'delivery', group, require_staged=False)
    p.require(precheck['ok'], 'pre-staging delivery validation failed: ' + '; '.join(precheck['reasons']))
    target = p.path_inside(root, contract['delivery_ref'])
    delivery = p.read_json(target) if target.exists() else {}
    if delivery:
        p.require(delivery.get('work_id') == work_id and delivery.get('plan_version') == contract['plan_version']
                  and delivery.get('accepted_snapshot') == evidence['snapshot'], 'existing delivery record belongs to another acceptance')
    completed = {x['repo_path']: x for x in delivery.get('repositories', [])}
    by_name = {x['repo_path']: x for x in repos}
    for name in contract['handoff']['merge_order']:
        row = by_name[name]
        repo = row['repo']
        head = context.git(repo, 'rev-parse', 'HEAD')
        if name in completed:
            p.require(head == completed[name]['feature_commit'], 'previously delivered feature commit changed')
        if head != row['base_commit']:
            # Recover a crash after git commit but before saving delivery.json.
            p.require(context.git(repo, 'show', '-s', '--format=%P', head) == row['base_commit'], 'partial feature commit has wrong parent')
            work = product_entries(repo, root, work_id, records, group)
            committed = product_entries(repo, root, work_id, records, group, 'commit', head)
            p.require(work == committed, 'partial feature commit differs from accepted content')
        context.git(repo, 'add', '-A', '--', *row['allowed_paths'])
    gate = check(root, work_id, 'delivery', group)
    p.require(gate['ok'], 'delivery gate failed: ' + '; '.join(gate['reasons']))
    gate_path = f'docs/work/{work_id}/evidence/delivery-gate.json'
    p.require(gate_path in records, 'predeclare delivery-gate.json')
    raw_path = p.path_inside(root, gate_path)
    save_json(raw_path, gate)
    delivery = {'schema': 'megin-delivery-result/v2' if group else 'megin-repo-delivery-result/v2',
                'work_id': work_id, 'plan_version': contract['plan_version'], 'delivery_mode': 'feature_handoff',
                'accepted_snapshot': evidence['snapshot'], 'status': 'partial', 'repositories': list(completed.values()),
                'delivery_gate': {'exit_code': 0, 'output': {'path': gate_path, 'sha256': hashlib.sha256(raw_path.read_bytes()).hexdigest()}}}
    save_json(target, delivery)
    for name in contract['handoff']['merge_order']:
        row = by_name[name]
        check_owner(root, work_id, writer, group)
        context.master(row['repo'], row['remote_name'], row['remote_url'], row['base_commit'])
        head = context.git(row['repo'], 'rev-parse', 'HEAD')
        if head == row['base_commit']:
            # The approved checks have already run against the frozen content.
            # Hooks can rewrite the index; isolate them for this command only,
            # while preserving normal identity/signing/commit configuration.
            import tempfile
            with tempfile.TemporaryDirectory(prefix='megin-frozen-commit-') as empty_hooks:
                context.git(row['repo'], '-c', 'core.hooksPath=' + empty_hooks,
                            'commit', '-m', f'{work_id}: accepted feature')
            head = context.git(row['repo'], 'rev-parse', 'HEAD')
        completed[name] = {'repo_path': name, 'base_commit': row['base_commit'], 'feature_branch': row['feature_branch'],
                           'feature_commit': head, 'product_sha256': gate['repositories'][name]['product_sha256']}
        delivery['repositories'] = [completed[x] for x in contract['handoff']['merge_order'] if x in completed]
        save_json(target, delivery)
    result = check(root, work_id, 'completion', group)
    p.require(result['ok'], 'completion gate failed: ' + '; '.join(result['reasons']))
    delivery.update(status='complete', completion_ok=True, completion_snapshot=result['snapshot'])
    save_json(target, delivery)
    # Status is the sole mutable workflow surface; preserve its ledger verbatim.
    workflow = root / f'docs/work/{work_id}/workflow.md'
    text = workflow.read_text(encoding='utf-8')
    text = re.sub(r'(?m)^- status: .+$', '- status: complete', text, count=1)
    workflow.write_text(text, encoding='utf-8')
    release(root, work_id, writer, group)
    return delivery


def export_receipt(root, work_id, group=False):
    root = Path(root).resolve()
    result = check(root, work_id, 'completion', group, historical=True)
    p.require(result['ok'], 'historical completion failed: ' + '; '.join(result['reasons']))
    fields, contract, _, records = load_contract(root, work_id, group)
    p.require(fields['status'] == 'complete', 'receipt requires completed delivery')
    delivery = p.read_json(root / contract['delivery_ref'])
    value = {'schema': 'megin-delivery-receipt/v2' if group else 'megin-repo-delivery-receipt/v2',
             'work_id': work_id, 'plan_version': contract['plan_version'], 'workspace_root': str(root),
             'delivery_mode': 'feature_handoff', 'accepted_snapshot': result['snapshot'],
             'repositories': delivery['repositories'], 'local_delivery_complete': True,
             'record_files': []}
    paths = {f'docs/work/{work_id}/requirements.md', f'docs/work/{work_id}/{contract["plan_version"]}/plan.md',
             f'docs/work/{work_id}/{contract["plan_version"]}/quality-contract.json'}
    paths |= {x for x in records if (root / x).is_file() and not x.endswith('/receipt.json')}
    for path in sorted(paths):
        value['record_files'].append({'path': path, 'sha256': hashlib.sha256(p.path_inside(root, path).read_bytes()).hexdigest()})
    value['receipt_sha256'] = p.canonical_digest(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('detect', 'inventory', 'resolve', 'baseline', 'fingerprint', 'claim', 'check', 'release', 'prepare', 'deliver'))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--repo', type=Path)
    selection.add_argument('--group-root', type=Path)
    parser.add_argument('--work-id')
    parser.add_argument('--writer')
    parser.add_argument('--repo-path', default='.')
    parser.add_argument('--remote')
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    try:
        if args.repo or args.group_root:
            group = args.group_root is not None
            root = context.group_root(args.group_root) if group else context.repository(args.repo)
        else:
            detected = context.detect(Path.cwd())
            group, root = detected['mode'] == 'group', Path(detected['root'])
        if args.action == 'detect':
            result = context.detect(root)
        elif args.action == 'inventory':
            result = {'mode': 'group' if group else 'repo', 'root': str(root),
                      'repositories': list(context.repositories(root)) if group else ['.']}
        elif args.action == 'resolve':
            result = context.resolve(root, args.repo_path, args.remote, group)
        elif args.action == 'baseline':
            p.require(args.destination, 'baseline requires an empty --destination outside the workspace')
            destination = args.destination.absolute()
            p.require(not destination.resolve().is_relative_to(root), 'baseline destination must be outside the workspace')
            from verification_inputs import no_links
            no_links(destination)
            p.require(not destination.exists() or destination.is_dir() and not any(destination.iterdir()),
                      'baseline destination must be empty')
            resolved = context.resolve(root, args.repo_path, args.remote, group)
            repo = context.repositories(root)[args.repo_path] if group else root
            proc = subprocess.run(['git', '-C', str(repo), 'fetch', '--no-tags', '--refmap=', resolved['remote_name'], 'refs/heads/master'],
                                  capture_output=True, check=False, timeout=60)
            p.require(proc.returncode == 0, 'cannot fetch master objects for read-only baseline exploration')
            p.require(context.git(repo, 'rev-parse', 'FETCH_HEAD') == resolved['base_commit'], 'remote master moved during baseline fetch')
            context.master(repo, resolved['remote_name'], resolved['remote_url'], resolved['base_commit'])
            from source_baseline import materialize
            from functional_inventory import scan
            exclusions = materialize(repo, resolved['base_commit'], destination)
            result = scan(destination, exclude_paths=p.SCAN_EXCLUSIONS)
            result.update(source_commit=resolved['base_commit'], repo_path=args.repo_path,
                          baseline_exclusions=exclusions, source_root=str(destination), remote_resolution=resolved)
        elif args.action == 'fingerprint':
            result = {'sha256': fingerprint(root)}
        else:
            p.require(args.work_id and args.writer, 'operation requires --work-id and --writer')
            action = {'claim': claim, 'check': check_owner, 'release': release, 'prepare': prepare, 'deliver': deliver}[args.action]
            result = action(root, args.work_id, args.writer, group)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, q.InvalidEvidence, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
