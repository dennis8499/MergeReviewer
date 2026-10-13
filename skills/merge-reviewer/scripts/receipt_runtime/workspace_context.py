"""Read-only workspace discovery and exact remote-master resolution."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit, urlunsplit

import quality_gate as q
from quality_policy import is_link, path_inside, read_json, require


def git(repo, *args):
    return q.line(Path(repo), *args)


def repository(path):
    path = Path(path).absolute()
    require(not is_link(path), 'repository cannot be a symlink or junction')
    path = path.resolve()
    require(path.is_dir() and Path(git(path, 'rev-parse', '--show-toplevel')).resolve() == path,
            'selected path must be its Git root')
    return path


def group_root(path):
    path = Path(path).absolute()
    require(not is_link(path), 'Group root cannot be a symlink or junction')
    path = path.resolve()
    require(path.is_dir(), 'Group root must exist')
    probe = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                           capture_output=True, check=False)
    require(probe.returncode != 0 and b'not a git repository' in probe.stderr.lower(),
            'Group root must be outside a Git repository')
    return path


def repositories(root):
    """Only real direct-child Git roots; never descend through links."""
    root = group_root(root)
    result = {}
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if is_link(child) or not child.is_dir():
            continue
        if (child / '.git').exists():
            repo = repository(child)
            require(repo.parent == root, 'repository escapes Group root')
            result[child.name] = repo
    return result


def detect(path):
    path = Path(path).absolute()
    require(not is_link(path), 'workspace root cannot be a link')
    probe = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                           capture_output=True, text=True, encoding='utf-8', check=False)
    if probe.returncode == 0:
        root = repository(Path(probe.stdout.strip()))
        return {'mode': 'repo', 'root': str(root), 'repositories': ['.']}
    root = group_root(path)
    names = list(repositories(root))
    require(names, 'no direct-child Git repositories found')
    return {'mode': 'group', 'root': str(root), 'repositories': names}


def common_dir(repo):
    repo = repository(repo)
    return Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()


def remote_url(value):
    """Credential-free identity; never persist a token from a Git remote URL."""
    if '://' in value:
        parsed = urlsplit(value)
        require(parsed.hostname, 'invalid remote URL')
        host = parsed.hostname
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port:
            host += ':' + str(parsed.port)
        if parsed.scheme.lower() == 'ssh' and parsed.username:
            host = parsed.username + '@' + host
        return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip('/'), '', ''))
    # SCP syntax is home-relative; SSH URL paths are absolute. SSH usernames
    # select namespaces and are nonsecret endpoint identity, not HTTP passwords.
    return value.rstrip('/')


def master(repo, remote, expected_url=None, expected_sha=None):
    require(isinstance(remote, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', remote),
            'remote name is missing or unsafe')
    url = remote_url(git(repo, 'remote', 'get-url', remote))
    require(expected_url is None or url == expected_url, 'remote URL changed')
    # Do not echo Git stderr, which can contain a credential-bearing remote URL.
    result = subprocess.run(['git', '-C', str(repo), 'ls-remote', '--exit-code', remote,
                             'refs/heads/master'], capture_output=True, text=True,
                            encoding='utf-8', check=False, timeout=30)
    require(result.returncode == 0, 'remote master unavailable (connection, authentication or missing branch)')
    rows = [row.split() for row in result.stdout.splitlines() if row.strip()]
    require(len(rows) == 1 and len(rows[0]) == 2 and rows[0][1] == 'refs/heads/master'
            and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', rows[0][0]),
            'remote did not return an exact master SHA')
    require(expected_sha is None or rows[0][0] == expected_sha, 'remote master changed; renew plan approval')
    return {'remote_name': remote, 'remote_url': url, 'base_branch': 'master', 'base_commit': rows[0][0]}


def resolve(root, repo_path='.', explicit=None, group=False):
    root = group_root(root) if group else repository(root)
    candidates = repositories(root) if group else {'.': root}
    require(repo_path in candidates, 'repository is not in this workspace')
    repo = candidates[repo_path]
    config_path = path_inside(root, '.megin/group.json' if group else '.megin/repo.json')
    configured, source, digest = None, None, None
    if config_path.exists():
        config = read_json(config_path)
        require(config.get('schema') == ('megin-group-config/v1' if group else 'megin-repo-config/v1'),
                'invalid workspace configuration schema')
        require(set(config) <= ({'schema', 'defaults', 'repositories'} if group else {'schema', 'remote'}),
                'unknown workspace configuration field')
        if group:
            defaults, overrides = config.get('defaults', {}), config.get('repositories', {})
            require(isinstance(defaults, dict) and isinstance(overrides, dict), 'invalid Group configuration')
            for name, values in [('defaults', defaults), *overrides.items()]:
                if name != 'defaults':
                    require(isinstance(name, str) and '/' not in name and name not in ('.', '..'), 'invalid Repo override')
                require(isinstance(values, dict) and set(values) <= {'remote', 'base_branch'}, 'invalid Repo settings')
                require(values.get('base_branch', 'master') == 'master', 'new work requires remote master')
                require('remote' not in values or isinstance(values['remote'], str) and values['remote'], 'invalid remote setting')
            override = overrides.get(repo_path, {})
            configured = override.get('remote', defaults.get('remote'))
            source = 'repo_override' if 'remote' in override else 'group_default'
        else:
            configured, source = config.get('remote'), 'repo_config'
            require(configured is None or isinstance(configured, str) and configured, 'invalid remote setting')
        digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    remotes = git(repo, 'remote').splitlines()
    if explicit is not None:
        selected, source = explicit, 'explicit'
    elif configured is not None:
        selected = configured
    elif 'origin' in remotes:
        selected, source = 'origin', 'origin'
    else:
        require(len(remotes) == 1, 'remote is missing or ambiguous; select one explicitly')
        selected, source = remotes[0], 'unique_remote'
    require(selected in remotes, 'selected remote does not exist')
    return {**master(repo, selected), 'repo_path': repo_path, 'source': source, 'config_sha256': digest}
