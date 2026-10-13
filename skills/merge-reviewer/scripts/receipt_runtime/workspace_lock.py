"""Atomic ownership shared by Repo and Group workflows and linked worktrees."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from quality_policy import path_inside, read_json, require
from workspace_context import common_dir


def repo_lock(repo):
    return path_inside(common_dir(repo), 'megin/workspace.lock.json')


def owner(root, work_id, writer, fingerprint):
    require(isinstance(writer, str) and writer.strip() and all(ord(x) >= 32 for x in writer),
            'writer must be a nonempty identity')
    return {'schema': 'megin-workspace-owner/v2', 'root': os.path.normcase(str(Path(root).resolve())),
            'work_id': work_id, 'writer': writer, 'skills_sha256': fingerprint}


def same(current, wanted):
    return all(current.get(k) == v for k, v in wanted.items())


def acquire(path, wanted):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish fully written bytes atomically; no observer sees a partial lock.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.megin-lock-', delete=False) as stream:
        temporary = Path(stream.name)
        raw = {**wanted, 'claimed_at': datetime.now(timezone.utc).isoformat()}
        stream.write((json.dumps(raw, sort_keys=True) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            check(path, wanted)
            return False
    finally:
        temporary.unlink(missing_ok=True)


def check(path, wanted):
    # Reject substituted paths on each operation.
    from verification_inputs import no_links
    no_links(path)
    current = read_json(path)
    require(same(current, wanted), 'workspace lock belongs to another root, Work ID, writer or Skills version')
    return current


def remove(path, wanted):
    check(path, wanted)
    path.unlink()


def paths(root, repos, group):
    values = [repo_lock(item['repo']) for item in repos]
    require(len({str(p) for p in values}) == len(values), 'selected Repos share a Git common-dir')
    values = sorted(values, key=lambda p: os.path.normcase(str(p)))
    if group:
        values.insert(0, path_inside(root, '.megin/workspace.lock.json'))
    return values


def claim(root, repos, group, wanted):
    obtained = []
    try:
        for item in repos:
            legacy = item['repo'] / '.megin/workspace.lock.json'
            require(not legacy.exists(), 'legacy Repo lock exists; complete work using its original package')
        for path in paths(root, repos, group):
            if acquire(path, wanted):
                obtained.append(path)
    except Exception:
        for path in reversed(obtained):
            remove(path, wanted)
        raise
    return {**wanted, 'claimed': True}


def check_all(root, repos, group, wanted):
    for path in paths(root, repos, group):
        check(path, wanted)
    return {**wanted, 'owned': True}


def release(root, repos, group, wanted):
    # Completion can resume a crash halfway through lock release. Verify all
    # remaining locks first and never remove a subsequent owner's lock.
    remaining = [path for path in paths(root, repos, group) if path.exists()]
    for path in remaining:
        check(path, wanted)
    for path in reversed(remaining):
        remove(path, wanted)
    return {'released': True, 'work_id': wanted['work_id']}
