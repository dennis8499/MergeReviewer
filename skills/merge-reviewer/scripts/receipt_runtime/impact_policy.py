"""Fixed-source Group impact evidence. Consistency checks do not replace review.

The impact graph may contain cycles. It is deliberately separate from the
acyclic delivery order. Searches read Git blobs; no project code is executed.
"""
from __future__ import annotations

import argparse
import copy
from functools import lru_cache
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
import quality_policy as p
import quality_gate as q

KINDS = {'api', 'event', 'package', 'data-contract', 'database', 'configuration', 'internal'}
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def unique(rows, key, label):
    p.require(isinstance(rows, list) and all(isinstance(x, dict) for x in rows), f'{label} must be an array')
    names = [x.get(key) for x in rows]
    p.require(all(isinstance(x, str) and x for x in names) and len(names) == len(set(names)),
              f'{label} has missing or duplicate {key}')
    return dict(zip(names, rows))


def text(value):
    return isinstance(value, str) and bool(value.strip())


@lru_cache(maxsize=2048)
def _committed_file(repo, commit, relative):
    repo = Path(repo)
    q.canonical_relative(relative)
    p.require(SHA.fullmatch(commit or ''), 'impact source needs a full commit SHA')
    rows = q.git(repo, 'ls-tree', '-z', commit, '--', relative).split(b'\0')
    p.require(len(rows) == 2 and rows[0], 'impact source file missing')
    meta, name = rows[0].split(b'\t', 1)
    mode, kind, _ = meta.decode().split()
    p.require(mode in ('100644', '100755') and kind == 'blob' and name.decode() == relative,
              'impact source must be a regular committed file')
    return q.git(repo, 'cat-file', 'blob', commit + ':' + relative)


def committed_file(repo, commit, relative):
    return _committed_file(str(Path(repo).resolve()), commit, relative)


@lru_cache(maxsize=512)
def _search(repo, commit, query):
    """Search all inventoried text at one immutable source, including no-hit proof."""
    from source_baseline import scan_commit
    p.require(text(query), 'impact query must be nonempty')
    repo = Path(repo)
    inventory = scan_commit(repo, commit)
    matches = []
    for row in sorted(inventory['included'], key=lambda x: x['path']):
        raw = committed_file(repo, commit, row['path'])
        try:
            lines = raw.decode('utf-8-sig').splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, 1):
            if query in line:
                matches.append({'path': row['path'], 'line': number, 'text': line})
    return matches


def search(repo, commit, query):
    """Return a fresh value; only immutable commit objects are cached.

    Validation still rechecks all source trees, live HEADs and worktree scans.
    Never cache current source or diff confirmation bytes.
    """
    p.require(isinstance(commit, str) and SHA.fullmatch(commit), 'impact search needs a full commit SHA')
    q.git(Path(repo), 'cat-file', '-e', commit + '^{tree}')
    return copy.deepcopy(_search(str(Path(repo).resolve()), commit, query))


def locator(root, sources, value):
    p.require(isinstance(value, dict) and value.get('repo_path') in sources, 'impact locator Repo missing')
    name = value['repo_path']
    p.require(value.get('commit') == sources[name], 'impact locator source commit changed')
    lines = committed_file(p.path_inside(root, name), sources[name], value.get('path')).decode('utf-8-sig').splitlines()
    number = value.get('line')
    p.require(type(number) is int and 0 < number <= len(lines) and value.get('text') == lines[number - 1],
              'impact locator text/line differs from fixed source')
    return name


def reachability(contracts, edges):
    """Iterative closure visits each node once per contract, including cycles."""
    result = {}
    for identifier, row in contracts.items():
        visited, queue = set(), [row['repo_path']]
        while queue:
            name = queue.pop()
            if name in visited:
                continue
            visited.add(name)
            queue.extend(e['consumer']['repo_path'] for e in edges if e['contract_id'] == identifier
                         and e['provider']['repo_path'] == name)
        result[identifier] = visited
    return result


def validate_architecture(root, work_id, contract, records, architecture, discovery):
    sources = {}
    inventories = {}
    for row in discovery['repositories']:
        inventory = p.read_json(p.reference(root, work_id, records, row['scan_ref']))
        commit = inventory.get('source_commit')
        p.require(isinstance(commit, str) and SHA.fullmatch(commit), 'Group impact discovery needs a pinned source_commit for every Repo')
        sources[row['repo_path']] = commit
        inventories[row['repo_path']] = {x['path'] for x in inventory['included']}
        q.git(p.path_inside(root, row['repo_path']), 'cat-file', '-e', commit + '^{tree}')
    planned = unique(architecture.get('changed_contracts'), 'id', 'changed contracts')
    p.require(planned, 'Group architecture needs explicit change contracts, including internal changes')
    selected = {x['repo_path']: x for x in contract['repositories']}
    initial_symbols, propagated_aliases = {}, {}
    for identifier, row in planned.items():
        p.require(row.get('repo_path') in selected and row.get('kind') in KINDS and text(row.get('description')),
                  'change contract needs a selected provider, kind and reason')
        symbols = row.get('symbols')
        p.require(isinstance(symbols, list) and len(symbols) >= 2 and len(set(symbols)) == len(symbols)
                  and all(text(x) for x in symbols), 'change contract needs at least two distinct relevant search terms')
        p.require(locator(root, sources, row.get('source')) == row['repo_path'], 'change contract provider differs from locator')
        p.require(row['source']['path'] in inventories[row['repo_path']], 'change contract source is outside inventoried source')
        aliases = row.get('symbol_sources', [])
        p.require(isinstance(aliases, list) and all(isinstance(x, dict) for x in aliases), 'contract alias source evidence must be explicit')
        anchored = {term for term in symbols if term in row['source']['text']}
        propagated_aliases[identifier] = []
        for alias in aliases:
            term = alias.get('symbol')
            alias_repo = locator(root, sources, alias.get('source'))
            p.require(term in symbols and alias['source']['path'] in inventories[alias_repo] and term in alias['source']['text'],
                      'contract search alias must be anchored to fixed source')
            if alias_repo == row['repo_path']:
                anchored.add(term)
            else:
                p.require(text(alias.get('reason')) and locator(root, sources, alias.get('via')) == alias_repo
                          and alias['via']['path'] in inventories[alias_repo],
                          'propagated alias needs a source-backed mapping reason and upstream use in the same Repo')
                propagated_aliases[identifier].append(alias)
        initial_symbols[identifier] = anchored
        p.require(anchored and anchored | {a['symbol'] for a in propagated_aliases[identifier]} == set(symbols),
                  'every contract search term needs provider or propagated alias source evidence')
    searches = unique(architecture.get('searches'), 'id', 'impact searches')
    for row in searches.values():
        identifier, name = row.get('contract_id'), row.get('repo_path')
        p.require(identifier in planned and name in sources and row.get('commit') == sources[name]
                  and row.get('mode') == 'literal' and row.get('query') in planned[identifier]['symbols'],
                  'impact search must bind a declared change contract and fixed Repo source')
        p.require(row.get('matches') == search(p.path_inside(root, name), sources[name], row['query']),
                  'impact search results differ from fixed source')
    for identifier, row in planned.items():
        for name in sources:
            queries = {x['query'] for x in searches.values() if x['contract_id'] == identifier and x['repo_path'] == name}
            p.require(queries == set(row['symbols']), f'impact coverage must search every anchored contract term: {identifier}/{name}')
    edges = architecture.get('impact_edges')
    p.require(isinstance(edges, list) and all(isinstance(x, dict) for x in edges), 'impact edges missing')
    seen = set()
    compatibility = set(contract['handoff']['compatibility_check_ids'])
    for edge in edges:
        identifier = edge.get('contract_id')
        p.require(identifier in planned and text(edge.get('reason')), 'impact edge needs a declared contract and reason')
        checks = edge.get('check_ids')
        p.require(isinstance(checks, list) and checks and len(checks) == len(set(checks)) and set(checks) <= compatibility,
                  'each impact edge must bind approved compatibility checks')
        provider, consumer = locator(root, sources, edge.get('provider')), locator(root, sources, edge.get('consumer'))
        p.require(provider != consumer, 'impact edge must connect distinct Repos')
        key = identifier, provider, consumer, p.canonical_digest(edge['consumer'])
        p.require(key not in seen, 'duplicate impact edge')
        seen.add(key)
        for endpoint in ('provider', 'consumer'):
            cite = edge[endpoint]
            p.require(cite['path'] in inventories[cite['repo_path']], 'impact edge cites excluded source')
            p.require(any(cite['path'] == hit['path'] and cite['line'] == hit['line']
                          for s in searches.values() if s['contract_id'] == identifier and s['repo_path'] == cite['repo_path']
                          for hit in s['matches']), 'impact edge endpoint is not supported by contract-related searches')
    reached = {}
    for identifier, row in planned.items():
        active = set(initial_symbols[identifier])
        visited = {row['repo_path']}
        pending_edges = [e for e in edges if e['contract_id'] == identifier]
        pending_aliases = list(propagated_aliases[identifier])
        while True:
            progress = False
            for edge in list(pending_edges):
                # A wrapper cannot create its own root dependency. First reach
                # its Repo via active upstream terms, then enable its alias.
                if edge['provider']['repo_path'] in visited and all(
                    any(term in edge[endpoint]['text'] for term in active) for endpoint in ('provider', 'consumer')
                ):
                    visited.add(edge['consumer']['repo_path'])
                    pending_edges.remove(edge)
                    progress = True
            for alias in list(pending_aliases):
                if alias['source']['repo_path'] in visited and any(term in alias['via']['text'] for term in active):
                    active.add(alias['symbol'])
                    pending_aliases.remove(alias)
                    progress = True
            if not progress:
                break
        p.require(not pending_aliases, 'propagated alias is disconnected from an already traced upstream contract')
        p.require(not pending_edges, 'impact edge is disconnected from changed provider or uses an untraced alias')
        reached[identifier] = visited
    impacts = unique(architecture.get('repository_impacts'), 'repo_path', 'repository impacts')
    p.require(set(impacts) == set(sources), 'impact dispositions must cover every Group Repo')
    for name, row in impacts.items():
        expected_ids = {identifier for identifier, names in reached.items() if name in names}
        p.require(row.get('status') in ('affected', 'unaffected', 'unknown') and text(row.get('reason')),
                  'impact disposition needs status and source-backed reason')
        p.require(row['status'] != 'unknown', 'unresolved impact or compatibility blocks planning')
        p.require(row['status'] == ('affected' if expected_ids else 'unaffected'), 'impact disposition contradicts transitive graph')
        p.require(isinstance(row.get('contract_ids'), list) and set(row['contract_ids']) == expected_ids,
                  'impact disposition contract association differs from transitive graph')
        ids = row.get('search_ids')
        p.require(isinstance(ids, list) and len(ids) == len(set(ids)) and ids
                  and all(x in searches and searches[x]['repo_path'] == name for x in ids), 'impact disposition search evidence missing')
        p.require({searches[x]['contract_id'] for x in ids} == set(planned), 'each disposition must reconcile every change contract')
    p.require(architecture.get('unresolved') == [], 'unresolved impact or external-source gaps block planning')
    verification = {x['repo_path']: x for x in contract.get('verification_repositories', [])}
    affected_unchanged = {name for name, row in impacts.items() if row['status'] == 'affected'} - set(selected)
    p.require(affected_unchanged <= set(verification), 'affected unchanged consumers need verification_repositories')
    for name, row in verification.items():
        p.require(row['base_commit'] == sources[name], 'verification Repo fixed source differs from discovery')
    if any(len(names) > 1 for names in reached.values()):
        p.require(compatibility, 'cross-Repo impact needs compatibility checks even with one modification Repo')
    for name in affected_unchanged:
        p.require(set(verification[name]['check_ids']) <= compatibility, 'unchanged consumer checks must be compatibility checks')
    return sources


def diff_summary(root, work_id, contract, repos, records, commits=None):
    from unified_workflow import product_entries
    rows = []
    paths = {}
    for row in sorted(repos, key=lambda x: x['repo_path']):
        name, repo = row['repo_path'], row['repo']
        before = {x['path']: x for x in product_entries(repo, root, work_id, records, True, 'commit', row['base_commit'])}
        after = {x['path']: x for x in product_entries(repo, root, work_id, records, True,
                 'commit' if commits else 'worktree', (commits or {}).get(name))}
        changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        paths[name] = changed
        rows.extend({'repo_path': name, 'path': path, 'before': before.get(path), 'after': after.get(path)} for path in changed)
    return {'changed_paths': paths, 'diff_sha256': p.canonical_digest(rows)}


def confirmation_searches(root, work_id, contract, repos, records, contract_ids, commits=None):
    """Replay contract searches against actual product bytes plus pinned peers.

    Ignored/sensitive/generated sources keep the scanner's visible boundary.
    The semantic reviewer must reconcile those exclusions and external gaps.
    """
    from unified_workflow import product_entries
    from functional_inventory import scan
    from source_baseline import scan_commit
    architecture = p.read_json(p.reference(root, work_id, records, contract['architecture_ref']))
    discovery = p.read_json(p.reference(root, work_id, records, contract['discovery_ref']))
    selected = {x['repo_path']: x for x in repos}
    contracts = {x['id']: x for x in architecture['changed_contracts']}
    result = []
    for candidate in discovery['repositories']:
        name = candidate['repo_path']
        repo = p.path_inside(root, name)
        if name in selected:
            commit = (commits or {}).get(name)
        else:
            commit = p.read_json(p.reference(root, work_id, records, candidate['scan_ref']))['source_commit']
        entries = product_entries(repo, root, work_id, records, True, 'commit' if commit else 'worktree', commit)
        source_digest = q.digest_entries(entries)
        inventory = scan_commit(repo, commit) if commit else scan(repo, exclude_paths=p.SCAN_EXCLUSIONS)
        product_paths = {x['path'] for x in entries if x['mode'] in ('100644', '100755')}
        files = {}
        for row in inventory['included']:
            if row['path'] not in product_paths:
                continue
            raw = committed_file(repo, commit, row['path']) if commit else p.path_inside(repo, row['path']).read_bytes()
            try:
                files[row['path']] = raw.decode('utf-8-sig').splitlines()
            except UnicodeDecodeError:
                continue
        for identifier in sorted(contract_ids):
            p.require(identifier in contracts, 'actual search references an unapproved contract')
            for number, query in enumerate(contracts[identifier]['symbols']):
                matches = [{'path': path, 'line': line_number, 'text': line}
                           for path, lines in sorted(files.items()) for line_number, line in enumerate(lines, 1) if query in line]
                result.append({'id': f'actual-{identifier}-{name}-{number}', 'contract_id': identifier, 'repo_path': name,
                               'source_product_sha256': source_digest, 'query': query, 'mode': 'literal', 'matches': matches})
    return result


def validate_confirmation(root, work_id, contract, repos, records, evidence, snapshot, commits=None):
    if contract.get('schema') != 'megin-quality-contract/v6':
        return
    value = p.read_json(p.reference(root, work_id, records, evidence.get('impact_confirmation_ref')))
    p.require((value.get('schema'), value.get('work_id'), value.get('plan_version'), value.get('requirements_revision')) ==
              ('megin-impact-confirmation/v1', work_id, contract['plan_version'], contract['requirements_revision']),
              'actual impact confirmation identity mismatch')
    p.require(value.get('architecture_sha256') == contract['architecture_ref']['sha256'] and value.get('snapshot') == snapshot,
              'actual impact confirmation is stale or approved architecture changed')
    expected = diff_summary(root, work_id, contract, repos, records, commits)
    p.require(all(value.get(k) == v for k, v in expected.items()), 'actual impact confirmation diff changed')
    architecture = p.read_json(p.reference(root, work_id, records, contract['architecture_ref']))
    contracts = {x['id']: x for x in architecture['changed_contracts']}
    changes = value.get('changes')
    p.require(isinstance(changes, list) and all(isinstance(x, dict) for x in changes), 'actual impact per-path classifications missing')
    actual_paths = {(name, path) for name, paths in expected['changed_paths'].items() for path in paths}
    keys = [(x.get('repo_path'), x.get('path')) for x in changes]
    p.require(len(keys) == len(set(keys)) and set(keys) == actual_paths, 'actual impact must reconcile every changed product path')
    used = set()
    for row in changes:
        ids = row.get('contract_ids')
        p.require(row.get('kind') in ('interface', 'internal') and text(row.get('reason')) and isinstance(ids, list)
                  and len(ids) == len(set(ids)) and set(ids) <= set(contracts), 'uncovered interface requires renewed architecture and plan approval')
        expected_ids = {identifier for identifier, c in contracts.items()
                        if c['repo_path'] == row['repo_path'] and c['source']['path'] == row['path']}
        p.require(expected_ids <= set(ids) and (row['kind'] != 'interface' or bool(ids)),
                  'changed interface is outside the approved impact graph; renew plan approval')
        p.require(all(contracts[x]['repo_path'] == row['repo_path'] for x in ids), 'actual interface belongs to a different provider')
        used.update(ids)
    p.require(value.get('contract_ids') == sorted(used), 'actual changed contract list differs from per-path evidence')
    p.require(value.get('searches') == confirmation_searches(root, work_id, contract, repos, records, used, commits),
              'actual impact searches are missing, stale or unrelated to the changed contracts')
    p.require(value.get('repository_impacts') == architecture['repository_impacts'],
              'new or changed consumer impact requires renewed plan approval')
    p.require(value.get('unresolved') == [], 'unresolved actual cross-Repo impact blocks review and acceptance')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('search', 'read', 'diff', 'actual-searches', 'validate-architecture'))
    parser.add_argument('--group-root', type=Path, required=True)
    parser.add_argument('--repo-path')
    parser.add_argument('--commit')
    parser.add_argument('--query')
    parser.add_argument('--contract-id')
    parser.add_argument('--id')
    parser.add_argument('--path')
    parser.add_argument('--line', type=int)
    parser.add_argument('--work-id')
    args = parser.parse_args()
    try:
        from workspace_context import group_root, repositories
        root = group_root(args.group_root)
        if args.action in ('diff', 'actual-searches', 'validate-architecture'):
            from unified_workflow import load_contract
            _, contract, repos, records = load_contract(root, args.work_id, True)
            if args.action == 'diff':
                result = diff_summary(root, args.work_id, contract, repos, records)
            elif args.action == 'actual-searches':
                architecture = p.read_json(p.reference(root, args.work_id, records, contract['architecture_ref']))
                identifiers = {x['id'] for x in architecture['changed_contracts']}
                result = confirmation_searches(root, args.work_id, contract, repos, records, identifiers)
            else:
                discovery = p.read_json(p.reference(root, args.work_id, records, contract['discovery_ref']))
                p.validate_architecture(root, args.work_id, contract, records, discovery)
                result = {'ok': True, 'work_id': args.work_id}
        else:
            available = repositories(root)
            p.require(args.repo_path in available, 'Repo must be a declared Group direct child')
            if args.action == 'search':
                p.require(args.id and args.contract_id, 'search needs --id and --contract-id')
                result = {'id': args.id, 'contract_id': args.contract_id, 'repo_path': args.repo_path,
                          'commit': args.commit, 'mode': 'literal', 'query': args.query,
                          'matches': search(available[args.repo_path], args.commit, args.query)}
            else:
                lines = committed_file(available[args.repo_path], args.commit, args.path).decode('utf-8-sig').splitlines()
                p.require(args.line is not None and 0 < args.line <= len(lines), 'source line outside file')
                result = {'repo_path': args.repo_path, 'commit': args.commit, 'path': args.path,
                          'line': args.line, 'text': lines[args.line - 1]}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, q.InvalidEvidence) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
