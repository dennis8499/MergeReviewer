#!/usr/bin/env python3
"""Collect immutable Git facts for the merge-reviewer skill.

The script deliberately uses Git object IDs for all comparison data. It does
not checkout, merge, reset, stage, or edit tracked files. Fetching a remote
branch is the only Git ref mutation performed by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse


SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "out",
    ".next",
    ".nuxt",
    ".venv",
    "venv",
    "__pycache__",
    ".idea",
}


class ReviewContextError(RuntimeError):
    """A user-actionable preparation failure."""


class GitCommandError(ReviewContextError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        command = "git " + " ".join(args)
        detail = stderr.strip() or "Git command failed without diagnostic output."
        super().__init__(f"{command} (exit {returncode}): {detail}")
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = detail


@dataclass(frozen=True)
class RemoteTarget:
    remote: str
    branch: str
    input_ref: str


def run_git(
    repo: Path,
    args: Sequence[str],
    *,
    check: bool = True,
    binary: bool = False,
    env: Mapping[str, str] | None = None,
) -> str | bytes:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=command_env,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        raise GitCommandError(args, completed.returncode, stderr)
    if binary:
        return completed.stdout
    return completed.stdout.decode("utf-8", errors="replace")


def run_git_capture(
    repo: Path,
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run Git without raising so callers can provide a useful diagnostic."""
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=command_env,
    )
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def git_ok(repo: Path, args: Sequence[str]) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def normalize_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def remove_tree(path: Path) -> None:
    """Remove a temporary tree even when Git created read-only object files."""
    if not path.exists():
        return

    def make_writable(function: Any, target: str, _error: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        function(target)

    shutil.rmtree(path, onerror=make_writable)


def strip_jsonc(text: str) -> str:
    """Remove JSONC comments without treating URL text as a comment."""
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2 if index + 1 <= len(text) else 0
            continue
        output.append(char)
        index += 1
    return "".join(output)


def strip_trailing_commas(text: str) -> str:
    """Accept the trailing commas allowed by VS Code JSONC files."""
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def workspace_roots(workspace: Path, workspace_file: Path | None) -> list[Path]:
    if workspace_file is None:
        return [workspace]
    try:
        content = workspace_file.read_text(encoding="utf-8")
        raw = json.loads(strip_trailing_commas(strip_jsonc(content)))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewContextError(f"無法讀取 VS Code workspace: {workspace_file}: {exc}") from exc
    folders = raw.get("folders", [])
    roots: list[Path] = []
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        value = folder.get("path")
        if not value and folder.get("uri"):
            parsed = urlparse(str(folder["uri"]))
            if parsed.scheme == "file":
                value = unquote(parsed.path)
                if os.name == "nt" and value.startswith("/") and len(value) > 2 and value[2] == ":":
                    value = value[1:]
        if not value:
            continue
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = workspace_file.parent / candidate
        roots.append(normalize_path(candidate))
    return roots or [workspace_file.parent]


def has_git_marker(path: Path) -> bool:
    marker = path / ".git"
    return marker.is_dir() or marker.is_file()


def discover_repositories(workspace: Path, workspace_file: Path | None) -> list[Path]:
    roots = workspace_roots(workspace, workspace_file)
    repositories: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        if has_git_marker(root):
            repositories.add(root)
            continue
        for current, directories, _files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories[:] = [name for name in directories if name not in SKIP_DIRECTORIES]
            if has_git_marker(current_path):
                repositories.add(normalize_path(current_path))
                directories[:] = []
    return sorted(repositories, key=lambda item: str(item).casefold())


def choose_repository(
    workspace: Path,
    workspace_file: Path | None,
    project: str | None,
) -> tuple[Path, list[Path]]:
    repositories = discover_repositories(workspace, workspace_file)
    if project:
        requested = Path(project).expanduser()
        candidates: list[Path] = []
        relative_target = workspace / requested
        if requested.is_absolute() or relative_target.exists():
            target = normalize_path(requested if requested.is_absolute() else workspace / requested)
            candidates = [repo for repo in repositories if repo == target]
        if not candidates:
            needle = project.casefold().rstrip("\\/")
            candidates = [
                repo
                for repo in repositories
                if repo.name.casefold() == needle or str(repo).casefold() == needle
            ]
        if len(candidates) != 1:
            available = ", ".join(str(repo) for repo in repositories) or "（找不到 Git repository）"
            raise ReviewContextError(
                f"專案名稱「{project}」無法唯一定位 repository。候選：{available}"
            )
        return candidates[0], repositories
    if len(repositories) != 1:
        available = ", ".join(str(repo) for repo in repositories) or "（找不到 Git repository）"
        raise ReviewContextError(
            f"未提供專案名稱，工作區必須恰好有一個 repository；候選：{available}"
        )
    return repositories[0], repositories


def remote_names(repo: Path) -> list[str]:
    output = str(run_git(repo, ["remote"]))
    return [line.strip() for line in output.splitlines() if line.strip()]


def is_shallow_repository(repo: Path) -> bool:
    return str(
        run_git(repo, ["rev-parse", "--is-shallow-repository"], check=False)
    ).strip().lower() == "true"


def remote_tracking_branch_names(repo: Path, remote: str) -> list[str]:
    """Return locally known branches for a remote, excluding its HEAD alias."""
    output = str(
        run_git(
            repo,
            [
                "for-each-ref",
                "--format=%(refname:strip=3)",
                f"refs/remotes/{remote}",
            ],
            check=False,
        )
    )
    return sorted(
        {
            line.strip()
            for line in output.splitlines()
            if line.strip() and line.strip() != "HEAD"
        }
    )


def remote_default_branch(
    repo: Path,
    remote: str,
    *,
    allow_network: bool,
) -> tuple[str, str]:
    """Resolve a remote's advertised default branch or report candidates."""
    if allow_network:
        return_code, output, error = run_git_capture(
            repo, ["ls-remote", "--symref", remote, "HEAD"]
        )
        if return_code != 0:
            detail = error.strip() or "沒有診斷訊息。"
            raise ReviewContextError(f"無法讀取 remote「{remote}」的預設分支：{detail}")
        for line in output.splitlines():
            if line.startswith("ref: ") and line.endswith("\tHEAD"):
                advertised = line[len("ref: ") : -len("\tHEAD")]
                if advertised.startswith("refs/heads/"):
                    return advertised[len("refs/heads/") :], "remote-head"

    symbolic = str(
        run_git(
            repo,
            ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"],
            check=False,
        )
    ).strip()
    prefix = f"{remote}/"
    if symbolic.startswith(prefix):
        return symbolic[len(prefix) :], "local-remote-head"

    candidates = remote_tracking_branch_names(repo, remote)
    if len(candidates) == 1:
        return candidates[0], "single-local-branch"
    formatted = ", ".join(f"{remote}/{branch}" for branch in candidates) or "（沒有本地 remote-tracking branch）"
    raise ReviewContextError(
        f"無法判定 remote「{remote}」的預設主分支；候選：{formatted}。"
        " 請使用 --base <remote>/<branch> 或先設定 remote HEAD。"
    )


def verified_commit(repo: Path, ref: str) -> str | None:
    value = str(
        run_git(
            repo,
            ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            check=False,
        )
    ).strip()
    return value or None


def resolve_commit_ref(repo: Path, ref: str) -> tuple[str | None, str]:
    """Resolve a ref while keeping local and remote branch namespaces separate."""
    target = remote_target_for_ref(repo, ref)
    if target:
        remote_ref = f"refs/remotes/{target.remote}/{target.branch}"
        remote_commit = verified_commit(repo, remote_ref)
        if remote_commit:
            return remote_commit, remote_ref

    if ref.startswith("refs/heads/"):
        local_commit = verified_commit(repo, ref)
        return (local_commit, ref)

    local_branch_ref = f"refs/heads/{ref}"
    if local_branch_exists(repo, ref):
        local_commit = verified_commit(repo, local_branch_ref)
        return (local_commit, local_branch_ref)

    if ref.startswith("refs/tags/"):
        tag_commit = verified_commit(repo, ref)
        return (tag_commit, ref)

    # Keep short tag names available, but never use an arbitrary Git revision
    # lookup here: that could silently resolve a stale remote-tracking ref.
    tag_commit = verified_commit(repo, f"refs/tags/{ref}")
    if tag_commit:
        return tag_commit, f"refs/tags/{ref}"

    if ref == "HEAD" or re.fullmatch(r"[0-9a-fA-F]{4,40}", ref):
        object_commit = verified_commit(repo, ref)
        if object_commit:
            return object_commit, ref
    return None, ref


def local_branch_exists(repo: Path, branch: str) -> bool:
    return git_ok(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])


def remote_target_for_ref(repo: Path, ref: str) -> RemoteTarget | None:
    """Classify only explicitly remote-qualified refs.

    An unqualified branch name is deliberately never inferred from a remote or
    from a local upstream configuration.  This keeps ``release`` and
    ``origin/release`` as two distinct user choices.
    """
    remotes = remote_names(repo)
    if ref.startswith("refs/remotes/"):
        normalized = ref[len("refs/remotes/") :]
        if "/" not in normalized:
            raise ReviewContextError(f"遠端 ref「{ref}」缺少分支名稱。")
        remote, branch = normalized.split("/", 1)
        if remote not in remotes:
            choices = ", ".join(remotes) or "（沒有已設定的 remote）"
            raise ReviewContextError(f"指定的 remote「{remote}」不存在；候選：{choices}。")
        if not branch:
            raise ReviewContextError(f"遠端 ref「{ref}」缺少分支名稱。")
        return RemoteTarget(remote, branch, ref)

    if not ref.startswith("refs/") and "/" in ref:
        remote, branch = ref.split("/", 1)
        if remote in remotes:
            if not branch:
                raise ReviewContextError(f"遠端 ref「{ref}」缺少分支名稱。")
            return RemoteTarget(remote, branch, ref)
    return None


def fetch_inputs(repo: Path, refs: Iterable[str], enabled: bool) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        target = remote_target_for_ref(repo, ref)
        if not target:
            continue
        if not enabled:
            raise ReviewContextError(
                f"遠端 ref「{target.input_ref}」需要連線確認，不能搭配 --no-fetch。"
            )
        if (target.remote, target.branch) in seen:
            continue
        seen.add((target.remote, target.branch))
        try:
            remote_ref = f"refs/heads/{target.branch}"
            tracking_ref = f"refs/remotes/{target.remote}/{target.branch}"
            run_git(
                repo,
                [
                    "fetch",
                    "--no-tags",
                    "--no-prune",
                    target.remote,
                    f"{remote_ref}:{tracking_ref}",
                ],
            )
        except GitCommandError as exc:
            raise ReviewContextError(
                f"fetch {target.remote}/{target.branch} 失敗（輸入 {target.input_ref}）：{exc.stderr}"
            ) from exc
        actions.append(
            {"remote": target.remote, "branch": target.branch, "input_ref": target.input_ref}
        )
    return actions


def ref_source_description(repo: Path, ref: str) -> str:
    target = remote_target_for_ref(repo, ref)
    if target:
        return f"指定遠端分支 {target.remote}/{target.branch}"
    if ref.startswith("refs/heads/"):
        return "本機分支"
    if ref.startswith("refs/tags/"):
        return "指定 tag"
    if local_branch_exists(repo, ref):
        return "本機分支"
    if ref == "HEAD":
        return "HEAD"
    if re.fullmatch(r"[0-9a-fA-F]{4,40}", ref):
        return "commit SHA"
    return "本機分支或短 tag"


def parse_name_status(raw: str) -> list[dict[str, str | None]]:
    parts = raw.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    changes: list[dict[str, str | None]] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(parts):
                raise ReviewContextError("Git 回傳的 rename/copy name-status 資料不完整。")
            old_path, new_path = parts[index], parts[index + 1]
            index += 2
            changes.append({"status": status, "old_path": old_path, "path": new_path})
        else:
            if index >= len(parts):
                raise ReviewContextError("Git 回傳的 name-status 資料不完整。")
            changes.append({"status": status, "old_path": None, "path": parts[index]})
            index += 1
    return changes


def parse_numstat(raw: str) -> dict[str, tuple[str, str]]:
    records: dict[str, tuple[str, str]] = {}
    for record in raw.split("\0"):
        if not record:
            continue
        fields = record.split("\t", 2)
        if len(fields) != 3:
            continue
        additions, deletions, path = fields
        records[path] = (additions, deletions)
    return records


def collect_changes(
    repo: Path,
    left: str,
    right: str,
    *,
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    name_status = str(
        run_git(
            repo,
            [
                "diff",
                "--no-ext-diff",
                "--name-status",
                "--find-renames",
                "--find-copies",
                "-z",
                "--diff-algorithm=histogram",
                left,
                right,
                "--",
            ],
            env=env,
        )
    )
    numstat = parse_numstat(
        str(
            run_git(
                repo,
                [
                    "diff",
                    "--no-ext-diff",
                    "--numstat",
                    "--no-renames",
                    "-z",
                    left,
                    right,
                    "--",
                ],
                env=env,
            )
        )
    )
    changes = parse_name_status(name_status)
    for change in changes:
        path = str(change["path"])
        additions, deletions = numstat.get(path, ("0", "0"))
        change["additions"] = None if additions == "-" else int(additions)
        change["deletions"] = None if deletions == "-" else int(deletions)
        change["binary"] = additions == "-" or deletions == "-"
    return changes


def commit_subject(repo: Path, commit: str) -> str:
    return str(run_git(repo, ["show", "-s", "--format=%s", commit])).strip()


def merge_commit_records(repo: Path, left: str, right: str) -> list[dict[str, Any]]:
    merge_shas = [
        line.strip()
        for line in str(
            run_git(repo, ["rev-list", "--merges", "--reverse", f"{left}..{right}"])
        ).splitlines()
        if line.strip()
    ]
    records: list[dict[str, Any]] = []
    for merge_sha in merge_shas:
        parents_line = str(run_git(repo, ["rev-list", "--parents", "-n", "1", merge_sha])).strip()
        tokens = parents_line.split()
        parents = tokens[1:]
        parent_views = []
        for position, parent in enumerate(parents, start=1):
            parent_views.append(
                {
                    "position": position,
                    "sha": parent,
                    "changes": collect_changes(repo, parent, merge_sha),
                    "stat": str(run_git(repo, ["diff", "--stat", "--find-renames", parent, merge_sha])).strip(),
                }
            )
        records.append(
            {
                "sha": merge_sha,
                "subject": commit_subject(repo, merge_sha),
                "parents": parents,
                "parent_views": parent_views,
            }
        )
    return records


def git_path(repo: Path, name: str) -> Path:
    value = Path(str(run_git(repo, ["rev-parse", "--git-path", name])).strip())
    return value if value.is_absolute() else repo / value


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return "missing"
    return digest.hexdigest()


def working_tree_digest(repo: Path) -> str:
    """Hash actual tracked and non-ignored untracked file contents."""
    tracked_raw = str(run_git(repo, ["ls-files", "-z"]))
    tracked = {item for item in tracked_raw.split("\0") if item}
    paths_raw = str(
        run_git(repo, ["ls-files", "-co", "--exclude-standard", "-z"])
    )
    records: list[tuple[str, str]] = []
    for relative in sorted({item for item in paths_raw.split("\0") if item}):
        if relative.startswith("review-reports/") and relative not in tracked:
            continue
        path = repo / Path(relative)
        if path.is_dir():
            # A gitlink is represented by its index/tree entry, not by the
            # contents of the nested repository.
            marker = str(
                run_git(repo, ["ls-files", "-s", "--", relative], check=False)
            ).strip()
            records.append((relative, marker or "directory"))
        else:
            records.append((relative, file_digest(path)))
    digest = hashlib.sha256()
    for relative, value in records:
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(value.encode("ascii", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def submodule_paths(repo: Path) -> set[str]:
    records = str(run_git(repo, ["ls-files", "-s", "-z"], check=False)).split("\0")
    paths: set[str] = set()
    for record in records:
        if not record or "\t" not in record:
            continue
        metadata, path = record.split("\t", 1)
        if metadata.split(" ", 1)[0] == "160000":
            paths.add(path)
    return paths


def dirty_submodule_paths(repo: Path) -> list[str]:
    """Find submodules whose nested checkout is not a committed clean tree."""
    candidates = submodule_paths(repo)
    if not candidates:
        return []
    status = str(
        run_git(
            repo,
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            check=False,
        )
    )
    dirty: set[str] = set()
    for record in status.split("\0"):
        if len(record) < 4:
            continue
        path = record[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[-1]
        if path in candidates and record[:2] != "  ":
            dirty.add(path)
    for path in candidates:
        nested = repo / Path(path)
        if not nested.exists() or not (nested / ".git").exists():
            dirty.add(path)
    return sorted(dirty)


def snapshot(repo: Path) -> dict[str, Any]:
    head = str(run_git(repo, ["rev-parse", "--verify", "HEAD"], check=False)).strip()
    branch = str(run_git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)).strip()
    status = str(run_git(repo, ["status", "--porcelain=v1", "--untracked-files=all"], check=False))
    index = git_path(repo, "index")
    return {
        "head": head,
        "branch": branch,
        "status": status,
        "index_digest": file_digest(index),
        "working_tree_digest": working_tree_digest(repo),
        "dirty_submodules": dirty_submodule_paths(repo),
    }


def snapshot_environment(repo: Path, temporary_dir: Path) -> dict[str, str]:
    objects = temporary_dir / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    repository_objects = git_path(repo, "objects")
    return {
        "GIT_INDEX_FILE": str(temporary_dir / "index"),
        "GIT_OBJECT_DIRECTORY": str(objects),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(repository_objects),
    }


def create_worktree_snapshot(repo: Path) -> tuple[Path, dict[str, str], str]:
    """Create a tree from the final working files without touching user state."""
    conflicts = str(run_git(repo, ["ls-files", "-u", "--full-name", "-z"], check=False))
    if conflicts:
        raise ReviewContextError("工作區有未解決的 merge conflict，無法建立穩定快照。")
    temporary_dir = Path(tempfile.mkdtemp(prefix="merge-reviewer-"))
    env = snapshot_environment(repo, temporary_dir)
    try:
        working_report_paths = [
            item
            for item in str(
                run_git(
                    repo,
                    ["ls-files", "-co", "--exclude-standard", "-z", "--", "review-reports"],
                    check=False,
                )
            ).split("\0")
            if item
        ]
        committed_report_paths = {
            item
            for item in str(
                run_git(repo, ["ls-tree", "-r", "--name-only", "HEAD", "--", "review-reports"], check=False)
            ).splitlines()
            if item
        }
        excluded_report_paths = [
            item for item in working_report_paths if item not in committed_report_paths
        ]
        run_git(repo, ["read-tree", "HEAD"], env=env)
        run_git(repo, ["add", "--all", "--", "."], env=env)
        if excluded_report_paths:
            run_git(repo, ["reset", "--quiet", "--", *excluded_report_paths], env=env)
        tree_sha = str(run_git(repo, ["write-tree"], env=env)).strip()
        if not tree_sha:
            raise ReviewContextError("無法建立工作區 tree 快照。")
        return temporary_dir, env, tree_sha
    except Exception:
        remove_tree(temporary_dir)
        raise


def write_context_bundle(
    context_dir: Path,
    manifest: dict[str, Any],
    repo: Path,
    diff_left: str,
    diff_right: str,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    context_dir = normalize_path(context_dir)
    context_dir.mkdir(parents=True, exist_ok=False)
    (context_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    patch = run_git(
        repo,
        [
            "diff",
            "--no-ext-diff",
            "--binary",
            "--find-renames",
            "--find-copies",
            diff_left,
            diff_right,
            "--",
        ],
        binary=True,
        env=env,
    )
    (context_dir / "diff.patch").write_bytes(patch if isinstance(patch, bytes) else patch.encode())
    if manifest.get("review_scope") == "working-tree":
        (context_dir / "working-tree.patch").write_bytes(
            patch if isinstance(patch, bytes) else patch.encode()
        )
        snapshot_dir = context_dir / "working-tree-files"
        for change in manifest.get("changed_files", []):
            path_value = str(change.get("path") or "")
            if not path_value or path_value.startswith("/") or ".." in Path(path_value).parts:
                continue
            try:
                content = run_git(
                    repo,
                    ["cat-file", "blob", f"{diff_right}:{path_value}"],
                    binary=True,
                    env=env,
                )
            except GitCommandError:
                continue
            destination = snapshot_dir / Path(path_value)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content if isinstance(content, bytes) else content.encode())
    for merge in manifest["merge_commits"]:
        for parent_view in merge["parent_views"]:
            parent = parent_view["sha"]
            parent_patch = run_git(
                repo,
                ["diff", "--no-ext-diff", "--binary", "--find-renames", parent, merge["sha"], "--"],
                binary=True,
            )
            filename = f"merge-{merge['sha'][:12]}-parent-{parent_view['position']}.patch"
            (context_dir / filename).write_bytes(
                parent_patch if isinstance(parent_patch, bytes) else parent_patch.encode()
            )


def resolve_quick_inputs(
    repo: Path,
    args: argparse.Namespace,
) -> tuple[str, str, str | None, str | None, str]:
    """Return base ref, current head ref and remote selection metadata."""
    branch = str(
        run_git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    ).strip()
    if not branch:
        raise ReviewContextError("快速審查需要目前位於本地分支；detached HEAD 請改用 --base/--head。")
    if not verified_commit(repo, "HEAD"):
        raise ReviewContextError("目前分支尚無 commit，無法建立快速審查版本。")
    if args.head:
        raise ReviewContextError("快速模式會使用目前本地分支 HEAD，不可同時指定 --head。")
    if args.include_working_tree and args.mode == "direct":
        raise ReviewContextError("--include-working-tree 目前只支援快速模式的合併前審查。")
    if args.base and args.remote:
        raise ReviewContextError("快速模式的 --base 與 --remote 不能同時使用。")
    remotes = remote_names(repo)
    if not remotes:
        raise ReviewContextError("快速審查找不到 remote；請先設定 remote，或使用一般的 --base/--head 模式。")

    selection_source: str
    selected_remote: str | None = args.remote
    selected_branch: str | None = None
    if args.base:
        target = remote_target_for_ref(repo, args.base)
        if target:
            if args.no_fetch:
                raise ReviewContextError(
                    f"遠端 ref「{args.base}」需要連線確認，不能搭配 --no-fetch。"
                )
            selected_remote = target.remote
            selected_branch = target.branch
        selection_source = "explicit-base"
        return args.base, "HEAD", selected_remote, selected_branch, selection_source

    if args.no_fetch:
        raise ReviewContextError("快速審查使用遠端 base，不能搭配 --no-fetch；請移除 --no-fetch。")

    if selected_remote:
        if selected_remote not in remotes:
            choices = ", ".join(remotes)
            raise ReviewContextError(f"remote「{selected_remote}」不存在；候選：{choices}。")
        selection_source = "explicit-remote"
    elif len(remotes) == 1:
        selected_remote = remotes[0]
        selection_source = "single-remote"
    else:
        choices = ", ".join(remotes)
        raise ReviewContextError(
            f"快速審查無法在多個 remote 中自動選擇；候選：{choices}。請使用 --remote <name>。"
        )

    selected_branch, branch_source = remote_default_branch(
        repo, selected_remote, allow_network=True
    )
    selection_source = f"{selection_source}+{branch_source}"
    return f"{selected_remote}/{selected_branch}", "HEAD", selected_remote, selected_branch, selection_source


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    if args.quick:
        if not args.base and args.head:
            raise ReviewContextError("快速模式需要自動選擇或明確指定 --base；--head 不可單獨使用。")
        if args.mode == "direct" and args.include_working_tree:
            raise ReviewContextError("--include-working-tree 不支援 direct 模式。")
    elif args.remote or args.include_working_tree:
        raise ReviewContextError("--remote 與 --include-working-tree 只能搭配 --quick 使用。")
    elif not args.base or not args.head:
        raise ReviewContextError("一般模式需要同時指定 --base 與 --head；快速模式請加 --quick。")

    workspace = normalize_path(args.workspace)
    workspace_file = normalize_path(args.workspace_file) if args.workspace_file else None
    if workspace_file and not workspace_file.exists():
        raise ReviewContextError(f"VS Code workspace 不存在：{workspace_file}")
    repo, repositories = choose_repository(workspace, workspace_file, args.project)
    if is_shallow_repository(repo):
        raise ReviewContextError(
            "repository 是 shallow clone，無法可靠判定共同祖先；請先取得完整歷史後再審查。"
        )

    before = snapshot(repo)
    remote_selection_source: str | None = None
    selected_remote: str | None = None
    selected_remote_branch: str | None = None
    if args.quick:
        base_input, head_input, selected_remote, selected_remote_branch, remote_selection_source = resolve_quick_inputs(repo, args)
    else:
        base_input, head_input = args.base, args.head

    fetch_refs = [base_input] if args.quick else [base_input, head_input]
    fetches = fetch_inputs(repo, fetch_refs, not args.no_fetch)
    base_sha, base_resolved_ref = resolve_commit_ref(repo, base_input)
    if not base_sha:
        raise ReviewContextError(
            f"找不到基礎 ref 的 commit：{base_input}（查找來源：{ref_source_description(repo, base_input)}）"
        )
    if args.quick:
        head_sha = verified_commit(repo, "HEAD")
        head_resolved_ref = "HEAD"
    else:
        head_sha, head_resolved_ref = resolve_commit_ref(repo, head_input)
    if not head_sha:
        raise ReviewContextError(
            f"找不到比較 ref 的 commit：{head_input}（查找來源：{ref_source_description(repo, head_input)}）"
        )

    merge_base_output = str(
        run_git(repo, ["merge-base", "--all", base_sha, head_sha], check=False)
    )
    merge_bases = [line.strip() for line in merge_base_output.splitlines() if line.strip()]
    if not merge_bases:
        raise ReviewContextError("兩個版本沒有共同祖先，無法建立可靠的比較範圍。")
    if len(merge_bases) > 1:
        raise ReviewContextError(
            "兩個版本存在多個共同祖先（criss-cross history），請先指定可接受的歷史或整理分支。"
        )
    merge_base = merge_bases[0]
    diff_left = merge_base if args.mode == "merge" else base_sha

    temporary_dir: Path | None = None
    automatic_context_parent: Path | None = None
    context_bundle_written = False
    snapshot_env: dict[str, str] | None = None
    review_right = head_sha
    review_scope = "committed"
    try:
        if args.quick and args.include_working_tree:
            temporary_dir, snapshot_env, review_right = create_worktree_snapshot(repo)
            review_scope = "working-tree"
        changes = collect_changes(repo, diff_left, review_right, env=snapshot_env)
        merge_commits = merge_commit_records(repo, diff_left, head_sha)
        patch = run_git(
            repo,
            [
                "diff",
                "--no-ext-diff",
                "--binary",
                "--find-renames",
                "--find-copies",
                diff_left,
                review_right,
                "--",
            ],
            binary=True,
            env=snapshot_env,
        )
        patch_size = len(patch) if isinstance(patch, bytes) else len(patch.encode())
        diff_check = str(
            run_git(
                repo,
                ["diff", "--check", diff_left, review_right, "--"],
                check=False,
                env=snapshot_env,
            )
        ).strip()
        after = snapshot(repo)
        working_tree_unchanged = before == after
        if args.quick and not working_tree_unchanged:
            raise ReviewContextError("審查期間工作區、index 或 HEAD 發生變更，已捨棄本次審查結果；請重新執行。")
        dirty_submodules = sorted(
            set(before.get("dirty_submodules", []))
            | set(after.get("dirty_submodules", []))
        )
        review_limitations: list[str] = []
        if dirty_submodules:
            review_limitations.append(
                "submodule 內部有未提交或未初始化的內容，未納入本次 Git tree 審查："
                + ", ".join(dirty_submodules)
            )
        binary_or_submodule_paths = sorted(
            {
                str(change["path"])
                for change in changes
                if bool(change.get("binary")) or str(change.get("status", "")).startswith("T")
            }
            | set(dirty_submodules)
        )

        manifest: dict[str, Any] = {
            "schema_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "workspace": str(workspace),
            "workspace_file": str(workspace_file) if workspace_file else None,
            "repositories": [str(item) for item in repositories],
            "project_input": args.project,
            "project": repo.name,
            "repo": str(repo),
            "quick": bool(args.quick),
            "remote": selected_remote,
            "remote_branch": selected_remote_branch,
            "remote_selection_source": remote_selection_source,
            "base_input": base_input,
            "head_input": head_input,
            "base_resolved_ref": base_resolved_ref,
            "head_resolved_ref": head_resolved_ref,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "mode": args.mode,
            "merge_base": merge_base,
            "diff_base": diff_left,
            "review_scope": review_scope,
            "review_right": review_right,
            "review_tree_sha": review_right if review_scope == "working-tree" else None,
            "snapshot_read_info": None,
            "fetches": fetches,
            "fetch_status": "completed" if fetches else ("disabled" if args.no_fetch else "not-needed"),
            "working_tree_unchanged": working_tree_unchanged,
            "working_tree_before": before,
            "working_tree_after": after,
            "dirty_submodule_paths": dirty_submodules,
            "review_limitations": review_limitations,
            "review_complete": not dirty_submodules,
            "changed_files": changes,
            "changed_file_count": len(changes),
            "merge_commits": merge_commits,
            "commit_count": int(
                str(run_git(repo, ["rev-list", "--count", f"{diff_left}..{head_sha}"])).strip() or "0"
            ),
            "diff_patch_bytes": patch_size,
            "diff_check": diff_check,
            "diff_stat": str(
                run_git(
                    repo,
                    ["diff", "--stat", "--find-renames", diff_left, review_right],
                    env=snapshot_env,
                )
            ).strip(),
            "binary_or_submodule_paths": binary_or_submodule_paths,
        }
        if temporary_dir:
            manifest["snapshot_read_info"] = {
                "index": "repository-external temporary index",
                "object_directory": "repository-external temporary object directory",
                "alternate_objects": "repository object directory, read-only reference",
                "temporary_cleanup": "after-generation",
            }
            manifest["working_tree_snapshot"] = {
                "tree_sha": review_right,
                "temporary": True,
                "cleaned_up_after_generation": True,
            }
        context_dir: Path | None = None
        if args.context_dir:
            context_dir = normalize_path(args.context_dir)
            if args.quick and args.include_working_tree:
                try:
                    context_dir.relative_to(repo)
                except ValueError:
                    pass
                else:
                    raise ReviewContextError(
                        "工作區快照的 --context-dir 必須位於 repository 外，避免將暫存資料混入審查範圍。"
                    )
        elif args.quick and args.include_working_tree:
            automatic_context_parent = Path(tempfile.mkdtemp(prefix="merge-review-context-"))
            context_dir = automatic_context_parent / "bundle"
            manifest["context_cleanup_required"] = True
            manifest["context_cleanup_note"] = "審查報告完成後可刪除 context_dir；helper 會保留它供工作區內容審查。"
        if context_dir:
            manifest["context_dir"] = str(context_dir)
            context_dir_was_absent = not context_dir.exists()
            try:
                write_context_bundle(
                    context_dir,
                    manifest,
                    repo,
                    diff_left,
                    review_right,
                    env=snapshot_env,
                )
            except Exception:
                if context_dir_was_absent:
                    remove_tree(context_dir)
                raise
            context_bundle_written = True
        return manifest
    finally:
        if temporary_dir:
            remove_tree(temporary_dir)
        if automatic_context_parent and not context_bundle_written:
            remove_tree(automatic_context_parent)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect immutable Git context for Merge Reviewer.")
    parser.add_argument("--workspace", default=os.getcwd(), help="Workspace root to search for repositories.")
    parser.add_argument("--workspace-file", help="Optional VS Code .code-workspace file.")
    parser.add_argument("--project", help="Repository folder name or path.")
    parser.add_argument("--quick", action="store_true", help="Compare the current local branch with a remote default branch.")
    parser.add_argument("--remote", help="Remote name to use in quick mode when more than one remote exists.")
    parser.add_argument("--include-working-tree", action="store_true", help="Include staged, unstaged, and non-ignored untracked files in quick mode.")
    parser.add_argument("--base", help="Base branch, remote ref, tag, or commit.")
    parser.add_argument("--head", help="Comparison branch or commit.")
    parser.add_argument("--mode", choices=("merge", "direct"), default="merge")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip fetch only for comparisons without remote-qualified refs.",
    )
    parser.add_argument("--format", choices=("json",), default="json")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--context-dir", help="Write manifest and complete patch files to this new directory.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = build_manifest(args)
    except (ReviewContextError, OSError, ValueError) as exc:
        print(f"merge-reviewer: error: {exc}", file=sys.stderr)
        return 2
    indent = 2 if args.pretty else None
    print(json.dumps(manifest, ensure_ascii=False, indent=indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
