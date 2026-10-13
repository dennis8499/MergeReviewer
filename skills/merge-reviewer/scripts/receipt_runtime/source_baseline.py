"""Materialize committed source for read-only architecture exploration.

Git objects are read in one batch. No archive attributes, hooks, checkout filters,
source execution or product-worktree changes are involved.
"""
from __future__ import annotations

from functools import lru_cache
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile

import quality_gate as q
import quality_policy as policy


def materialize(repo, commit, destination):
    from verification_inputs import no_links
    repo, destination = Path(repo).resolve(), Path(destination).absolute()
    no_links(destination)
    policy.require(not destination.resolve().is_relative_to(repo), 'baseline destination must be outside the product Repo')
    policy.require(not destination.exists() or destination.is_dir() and not any(destination.iterdir()),
                   'baseline destination must be an empty directory')
    rows = []
    for raw in q.git(repo, 'ls-tree', '-r', '-z', '--full-tree', commit).split(b'\0'):
        if not raw:
            continue
        meta, path = raw.split(b'\t', 1)
        mode, kind, blob = meta.decode().split()
        relative = q.canonical_relative(path.decode('utf-8'))
        policy.require(all(part.lower() != '.git' for part in Path(relative).parts),
                       'baseline cannot contain Git administrative files')
        policy.path_inside(destination, relative)
        if any(relative == p or relative.startswith(p + '/') for p in policy.SCAN_EXCLUSIONS):
            continue
        rows.append((relative, mode, kind, blob))
    blob_rows = [row for row in rows if row[2] == 'blob']
    proc = subprocess.run(['git', '-C', str(repo), 'cat-file', '--batch'],
                          input=('\n'.join(row[3] for row in blob_rows) + '\n').encode('ascii'),
                          capture_output=True, check=False)
    policy.require(proc.returncode == 0, 'baseline Git objects unavailable')
    offset, values = 0, []
    for relative, mode, _kind, blob in blob_rows:
        end = proc.stdout.index(b'\n', offset)
        header = proc.stdout[offset:end].decode().split()
        policy.require(len(header) == 3 and header[:2] == [blob, 'blob'], 'baseline blob is missing')
        size = int(header[2])
        raw = proc.stdout[end + 1:end + 1 + size]
        policy.require(len(raw) == size, 'baseline blob is truncated')
        values.append((relative, mode, raw))
        offset = end + size + 2
    destination.mkdir(parents=True, exist_ok=True)
    excluded = []
    for relative, mode, raw in values:
        # Never create or follow repository-controlled links in the scratch root.
        if mode == '120000':
            excluded.append({'path': relative, 'reason': 'committed_symlink'})
            continue
        policy.require(mode in ('100644', '100755'), 'unsupported baseline file mode')
        path = policy.path_inside(destination, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        if mode == '100755':
            path.chmod(path.stat().st_mode | 0o111)
    excluded.extend({'path': row[0], 'reason': 'committed_submodule', 'commit': row[3]}
                    for row in rows if row[1] == '160000')
    return excluded


@lru_cache(maxsize=32)
def _scan(repo, commit):
    from functional_inventory import scan
    with tempfile.TemporaryDirectory(prefix='megin-baseline-') as temporary:
        excluded = materialize(Path(repo), commit, Path(temporary))
        inventory = scan(Path(temporary), exclude_paths=policy.SCAN_EXCLUSIONS)
    inventory['source_commit'] = commit
    inventory['baseline_exclusions'] = excluded
    return inventory


def scan_commit(repo, commit):
    # Ensure the committed tree is still available before using immutable cache data.
    q.git(Path(repo), 'cat-file', '-e', commit + '^{tree}')
    return copy.deepcopy(_scan(str(Path(repo).resolve()), commit))


def validate(repo, commit, frozen):
    try:
        fresh = scan_commit(repo, commit)
    except q.InvalidEvidence as exc:
        raise ValueError('pinned master objects unavailable; explore baseline before plan approval') from exc
    policy.require(frozen.get('source_commit', commit) == commit, 'architecture baseline commit changed')
    expected = {x['path']: x for x in fresh['included']}
    recorded = {x['path']: x for x in frozen['included']}
    policy.require(set(expected) == set(recorded), 'architecture baseline file inventory differs from approved remote master')
    for path, row in expected.items():
        digests = {row['sha256']}
        # A local exploration may have observed standard CRLF checkout bytes.
        if recorded[path]['sha256'] not in digests:
            raw = q.git(Path(repo), 'show', commit + ':' + path)
            if b'\0' not in raw and b'\r' not in raw:
                digests.add(hashlib.sha256(raw.replace(b'\n', b'\r\n')).hexdigest())
        policy.require(recorded[path]['sha256'] in digests,
                       'architecture baseline source differs from approved remote master: ' + path)
    policy.require({x['entrypoint_id'] for x in frozen['entrypoint_candidates']} ==
                   {x['entrypoint_id'] for x in fresh['entrypoint_candidates']},
                   'architecture baseline entrypoints differ from approved remote master')
