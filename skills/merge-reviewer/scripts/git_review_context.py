#!/usr/bin/env python3
"""Collect immutable Git facts for the merge-reviewer skill.

The script deliberately uses Git object IDs for all comparison data. It does
not checkout, merge, reset, stage, or edit tracked files. Fetching a remote
branch is the only Git ref mutation performed by default.
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import git_network_lease

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
    ".review-runtime",
}


class ReviewContextError(RuntimeError):
    """A user-actionable preparation failure; ``hint`` is the suggested next step."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class GitCommandError(ReviewContextError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        command = "git " + " ".join(args)
        detail = stderr.strip() or "Git command failed without diagnostic output."
        hint = (
            "請確認 repository 或遠端連線狀態；可使用 --git-timeout 增加等待時間後重試。"
            if returncode == 124
            else None
        )
        super().__init__(f"{command} (exit {returncode}): {detail}", hint=hint)
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = detail


@dataclass(frozen=True)
class RemoteTarget:
    remote: str
    branch: str
    input_ref: str


DIFF_FLAGS = ("--no-ext-diff", "--no-textconv")
DEFAULT_GIT_TIMEOUT_SECONDS = 180.0
GIT_TIMEOUT_SECONDS = contextvars.ContextVar(
    "merge_reviewer_git_timeout", default=DEFAULT_GIT_TIMEOUT_SECONDS
)
NETWORK_ACTIVITY = contextvars.ContextVar("merge_reviewer_network_activity", default=None)
CAPTURE_PHASE = contextvars.ContextVar("merge_reviewer_capture_phase", default="local-preflight")


def empty_network_activity() -> dict[str, int]:
    return {"fetch_attempt_count": 0, "fetch_success_count": 0,
            "requested_ref_count": 0, "synced_ref_count": 0}


def network_activity() -> dict[str, int]:
    """Return capture-scoped fetch command counts, never connection counts."""
    return dict(NETWORK_ACTIVITY.get() or empty_network_activity())


def capture_phase(value: str) -> None:
    CAPTURE_PHASE.set(value)
    import review_coordinator
    active = review_coordinator.ACTIVE.get()
    if active:
        active.phase(value)


def capture_diagnostic(exc: Exception) -> dict[str, Any]:
    return {"phase": getattr(exc, "phase", CAPTURE_PHASE.get()),
            "network_activity": getattr(exc, "network_activity", network_activity())}


def run_git(
    repo: Path,
    args: Sequence[str],
    *,
    check: bool = True,
    binary: bool = False,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> str | bytes:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    command_env["GIT_OPTIONAL_LOCKS"] = "0"
    command_env["GIT_NO_LAZY_FETCH"] = "1"
    command_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    command_env["GIT_TERMINAL_PROMPT"] = "0"
    if not (args and args[0] == "fetch"):
        # Older Git ignores GIT_NO_LAZY_FETCH; deny implicit transports too.
        command_env["GIT_ALLOW_PROTOCOL"] = ""
    try:
        runner = git_network_lease.run if args and args[0] == "fetch" else subprocess.run
        options = {}
        if runner is git_network_lease.run:
            import review_coordinator
            active = review_coordinator.ACTIVE.get()
            if active:
                options["cancelled"] = active.network_cancelled
                active.phase("waiting-network")
                options["observer"] = active.phase
        completed = runner(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=command_env,
            timeout=GIT_TIMEOUT_SECONDS.get(),
            **options,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewContextError(
            f"Git 指令逾時（{GIT_TIMEOUT_SECONDS.get():g} 秒）：git {' '.join(args)}",
            hint="請確認 repository、磁碟或遠端連線狀態；可使用 --git-timeout 增加等待時間後重試。",
        ) from exc
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
    command_env["GIT_OPTIONAL_LOCKS"] = "0"
    command_env["GIT_NO_LAZY_FETCH"] = "1"
    command_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    command_env["GIT_TERMINAL_PROMPT"] = "0"
    command_env["GIT_ALLOW_PROTOCOL"] = ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=command_env,
            timeout=GIT_TIMEOUT_SECONDS.get(),
        )
    except subprocess.TimeoutExpired as exc:
        message = (
            f"Git 指令逾時（{GIT_TIMEOUT_SECONDS.get():g} 秒）："
            f"git {' '.join(args)}"
        )
        return 124, "", message
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def git_ok(repo: Path, args: Sequence[str]) -> bool:
    command_env = os.environ.copy()
    command_env["GIT_OPTIONAL_LOCKS"] = "0"
    command_env["GIT_NO_LAZY_FETCH"] = "1"
    command_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    command_env["GIT_TERMINAL_PROMPT"] = "0"
    command_env["GIT_ALLOW_PROTOCOL"] = ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env=command_env,
            timeout=GIT_TIMEOUT_SECONDS.get(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewContextError(
            f"Git 指令逾時（{GIT_TIMEOUT_SECONDS.get():g} 秒）：git {' '.join(args)}",
            hint="請確認 repository 狀態；可使用 --git-timeout 增加等待時間後重試。",
        ) from exc
    return completed.returncode == 0


def normalize_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def safe_relative_git_path(value: str) -> bool:
    """Reject paths that could escape a bundle on either supported OS."""
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    return bool(value) and not (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    )


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
    if not isinstance(raw, dict):
        raise ReviewContextError(
            f"VS Code workspace 必須是包含 folders 陣列的 JSON 物件：{workspace_file}"
        )
    folders = raw.get("folders", [])
    if not isinstance(folders, list):
        raise ReviewContextError(
            f"VS Code workspace 的 folders 必須是陣列：{workspace_file}"
        )
    roots: list[Path] = []
    for folder in folders:
        if not isinstance(folder, dict):
            raise ReviewContextError("VS Code workspace 的每個 folder 必須是物件。")
        value = folder.get("path")
        if not value and folder.get("uri"):
            parsed = urlparse(str(folder["uri"]))
            if parsed.scheme == "file":
                value = unquote(parsed.path)
                if os.name == "nt" and value.startswith("/") and len(value) > 2 and value[2] == ":":
                    value = value[1:]
        if not value:
            raise ReviewContextError("VS Code workspace 含無法在本機讀取的 folder；請提供本機 path 或 file URI。")
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = workspace_file.parent / candidate
        roots.append(normalize_path(candidate))
    if not roots:
        raise ReviewContextError("VS Code workspace 必須宣告至少一個可在本機讀取的資料夾。")
    return roots


def has_git_marker(path: Path) -> bool:
    marker = path / ".git"
    return marker.is_dir() or marker.is_file()


def discover_repositories(workspace: Path, workspace_file: Path | None) -> list[Path]:
    roots = workspace_roots(workspace, workspace_file)
    repositories: set[Path] = set()
    def discovery_error(error):
        raise ReviewContextError(f"無法讀取宣告的工作區目錄：{error}")
    for root in roots:
        if not root.exists():
            continue
        if has_git_marker(root):
            repositories.add(root)
            continue
        for current, directories, _files in os.walk(root, topdown=True, followlinks=False, onerror=discovery_error):
            current_path = Path(current)
            directories[:] = [name for name in directories if name not in SKIP_DIRECTORIES]
            if has_git_marker(current_path):
                repositories.add(normalize_path(current_path))
                directories[:] = []
    return sorted(repositories, key=lambda item: str(item).casefold())


def is_group_workspace(workspace: Path, workspace_file: Path | None, repositories: list[Path]) -> bool:
    """Only two or more distinct declared Git roots enable impact extension."""
    return len(set(repositories)) > 1

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
        explicit_path = requested.is_absolute() or any(separator in project for separator in ("/", "\\"))
        if explicit_path:
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
                f"專案名稱「{project}」無法唯一定位 repository。候選：{available}",
                hint="請從候選清單擇一，以完整路徑或資料夾名稱重新指定專案名稱。",
            )
        return candidates[0], repositories
    if len(repositories) != 1:
        available = ", ".join(str(repo) for repo in repositories) or "（找不到 Git repository）"
        raise ReviewContextError(
            f"未提供專案名稱，工作區必須恰好有一個 repository；候選：{available}",
            hint="請指定專案名稱或 repository 路徑；若找不到 repository，請確認工作區資料夾是否正確。",
        )
    return repositories[0], repositories


def remote_names(repo: Path) -> list[str]:
    output = str(run_git(repo, ["remote"]))
    return [line.strip() for line in output.splitlines() if line.strip()]


def is_shallow_repository(repo: Path) -> bool:
    return str(
        run_git(repo, ["rev-parse", "--is-shallow-repository"], check=False)
    ).strip().lower() == "true"


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
    """Resolve branches only; never substitute tags, revisions, or cached remotes."""
    target = remote_target_for_ref(repo, ref)
    if target:
        remote_ref = f"refs/remotes/{target.remote}/{target.branch}"
        return verified_commit(repo, remote_ref), remote_ref
    local_ref = ref if ref.startswith("refs/heads/") else f"refs/heads/{ref}"
    if ref == "HEAD" or (ref.startswith("refs/") and not ref.startswith("refs/heads/")):
        return None, ref
    if not git_ok(repo, ["check-ref-format", local_ref]):
        return None, ref
    return verified_commit(repo, local_ref), local_ref

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
            raise ReviewContextError(
                f"指定的 remote「{remote}」不存在；候選：{choices}。",
                hint="請改用已設定的 remote 名稱，例如 origin/main。",
            )
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
    grouped: dict[str, list[RemoteTarget]] = {}
    for ref in refs:
        target = remote_target_for_ref(repo, ref)
        if not target:
            continue
        if (target.remote, target.branch) in seen:
            continue
        seen.add((target.remote, target.branch))
        grouped.setdefault(target.remote, []).append(target)
    activity = NETWORK_ACTIVITY.get()
    if activity is not None:
        activity["requested_ref_count"] += len(seen)
    if grouped and not enabled:
        target = next(iter(grouped.values()))[0]
        raise ReviewContextError(
            f"遠端 ref「{target.input_ref}」需要連線確認，不能搭配 --no-fetch。",
            hint="請移除 --no-fetch；若要離線審查，請改用本機分支。",
        )
    for remote, targets in grouped.items():
        def fetch(refspecs):
            capture_phase("fetch")
            if activity is not None:
                activity["fetch_attempt_count"] += 1
            run_git(repo, ["fetch", "--no-tags", "--no-prune", "--atomic",
                           "--no-recurse-submodules", "--no-auto-maintenance",
                           "--no-write-fetch-head", "--jobs=1", remote, *refspecs])
            if activity is not None:
                activity["fetch_success_count"] += 1
                activity["synced_ref_count"] += len(targets)
        try:
            import review_coordinator
            active = review_coordinator.ACTIVE.get()
            if active:
                active.sync(repo, remote, targets, sys.modules[__name__], fetch)
            else:
                fetch([f"refs/heads/{item.branch}:refs/remotes/{remote}/{item.branch}" for item in targets])
        except GitCommandError as exc:
            raise ReviewContextError(
                f"fetch {remote} 失敗（輸入 {', '.join(item.input_ref for item in targets)}）：{exc.stderr}",
                hint="請確認網路、帳號權限，以及遠端是否有此分支；為避免審查過期內容，不會自動改用舊版本。",
            ) from exc
        actions.extend({"remote": remote, "branch": item.branch, "input_ref": item.input_ref}
                       for item in targets)
    return actions


def ref_source_description(repo: Path, ref: str) -> str:
    target = remote_target_for_ref(repo, ref)
    return f"指定遠端分支 {target.remote}/{target.branch}" if target else "指定本機分支"

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
    parts = raw.split("\0")
    if parts and not parts[-1]:
        parts.pop()
    index = 0
    while index < len(parts):
        fields = parts[index].split("\t", 2)
        index += 1
        if len(fields) != 3:
            raise ReviewContextError("Git 回傳的 numstat 資料不完整。")
        additions, deletions, path = fields
        if path:
            records[path] = (additions, deletions)
            continue
        if index + 1 >= len(parts):
            raise ReviewContextError("Git 回傳的 rename/copy numstat 路徑不完整。")
        _old_path, new_path = parts[index], parts[index + 1]
        index += 2
        records[new_path] = (additions, deletions)
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
                *DIFF_FLAGS,
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
                    *DIFF_FLAGS,
                    "--numstat",
                    "--find-renames",
                    "--find-copies",
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
    raw_status = str(
        run_git(
            repo,
            ["diff", *DIFF_FLAGS, "--raw", "--no-abbrev", "--find-renames", "--find-copies", "-z", left, right, "--"],
            env=env,
        )
    )
    raw_parts = raw_status.split("\0")
    raw_records: dict[tuple[str | None, str], dict[str, str]] = {}
    index = 0
    while index < len(raw_parts):
        metadata = raw_parts[index]
        index += 1
        if not metadata:
            continue
        fields = metadata.removeprefix(":").split()
        if len(fields) < 5:
            raise ReviewContextError("Git 回傳的 raw diff 資料不完整。")
        old_mode, new_mode, old_blob, new_blob, status = fields[:5]
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(raw_parts):
            raise ReviewContextError("Git 回傳的 raw diff 路徑資料不完整。")
        paths = raw_parts[index : index + path_count]
        index += path_count
        old_path, path = (paths[0], paths[1]) if path_count == 2 else (None, paths[0])
        raw_records[(old_path, path)] = {
            "old_mode": old_mode,
            "new_mode": new_mode,
            "old_blob": old_blob,
            "new_blob": new_blob,
        }
    for change in changes:
        path = str(change["path"])
        additions, deletions = numstat.get(path, ("0", "0"))
        change["additions"] = None if additions == "-" else int(additions)
        change["deletions"] = None if deletions == "-" else int(deletions)
        change["binary"] = additions == "-" or deletions == "-"
        change.update(raw_records.get((change["old_path"], path), {}))
    return changes


def collect_submodule_paths(
    repo: Path,
    left: str,
    right: str,
    *,
    env: Mapping[str, str] | None = None,
) -> set[str]:
    """Return changed paths that are gitlinks on either side of a diff."""
    raw = str(
        run_git(
            repo,
            ["diff", *DIFF_FLAGS, "--raw", "--no-abbrev", "-z", left, right, "--"],
            env=env,
        )
    )
    parts = raw.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(parts):
        metadata = parts[index]
        index += 1
        if not metadata:
            continue
        fields = metadata.removeprefix(":").split()
        if len(fields) < 5:
            raise ReviewContextError("Git 回傳的 raw diff 資料不完整。")
        old_mode, new_mode, _old_sha, _new_sha, status = fields[:5]
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(parts):
            raise ReviewContextError("Git 回傳的 raw diff 路徑資料不完整。")
        changed_paths = [path for path in parts[index : index + path_count] if path]
        index += path_count
        if old_mode == "160000" or new_mode == "160000":
            paths.update(changed_paths)
    return paths


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


def create_merge_preview(
    repo: Path,
    base_sha: str,
    head_sha: str,
    merge_base: str,
    temporary_dir: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Preview a merge in temporary object storage without writing the repository."""
    object_directory = temporary_dir / "objects"
    object_directory.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_OBJECT_DIRECTORY": str(object_directory),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_path(repo, "objects")),
    }
    args = [
        "merge-tree",
        "--write-tree",
        "--messages",
        f"--merge-base={merge_base}",
        base_sha,
        head_sha,
    ]
    returncode, output, error = run_git_capture(repo, args, env=env)
    first_line = output.splitlines()[0].strip() if output.splitlines() else ""
    if returncode not in (0, 1) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", first_line):
        detail = error.strip() or output.strip() or f"git merge-tree failed (exit {returncode})"
        return {
            "status": "unavailable",
            "exit_code": returncode,
            "detail": detail,
            "tree_sha": None,
            "changed_files": [],
        }, env
    changes = collect_changes(repo, head_sha, first_line, env=env)
    return {
        "status": "conflicts" if returncode == 1 else "clean",
        "exit_code": returncode,
        "detail": output,
        "tree_sha": first_line,
        "head_sha": head_sha,
        "changed_files": changes,
        "env": env,
        "manifest_info": {},
    }, env


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


def tracked_index_entries(repo: Path, *, check: bool = True) -> list[tuple[str, str]]:
    """Read index metadata and unquoted names together, including every stage."""
    raw = str(run_git(repo, ["ls-files", "-s", "-z"], check=check))
    entries = []
    for record in raw.split("\0"):
        if record and "\t" in record:
            metadata, path = record.split("\t", 1)
            entries.append((metadata, path))
    return entries


def working_tree_digest(
    repo: Path, *, tracked_index: Sequence[tuple[str, str]] | None = None
) -> str:
    """Hash actual tracked and non-ignored untracked file contents."""
    if tracked_index is None:
        tracked_index = tracked_index_entries(repo)
    tracked = {path for _metadata, path in tracked_index}
    paths_raw = str(
        run_git(repo, ["ls-files", "-co", "--exclude-standard", "-z"])
    )
    records: list[tuple[str, str]] = []
    for relative in sorted({item for item in paths_raw.split("\0") if item}):
        if relative.startswith("review-reports/") and relative not in tracked:
            continue
        path = repo / Path(relative)
        if path.is_symlink():
            records.append((relative, hashlib.sha256(os.readlink(path).encode("utf-8", errors="surrogateescape")).hexdigest()))
        elif path.is_dir():
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


def submodule_paths(
    repo: Path, *, tracked_index: Sequence[tuple[str, str]] | None = None
) -> set[str]:
    if tracked_index is None:
        tracked_index = tracked_index_entries(repo, check=False)
    return {path for metadata, path in tracked_index
            if metadata.split(" ", 1)[0] == "160000"}


def dirty_submodule_paths(
    repo: Path, *, tracked_index: Sequence[tuple[str, str]] | None = None
) -> list[str]:
    """Find submodules whose nested checkout is not a committed clean tree."""
    candidates = submodule_paths(repo, tracked_index=tracked_index)
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
    index_digest = file_digest(index)
    tracked_index = tracked_index_entries(repo)
    return {
        "head": head,
        "branch": branch,
        "status": status,
        "index_digest": index_digest,
        "working_tree_digest": working_tree_digest(repo, tracked_index=tracked_index),
        "dirty_submodules": dirty_submodule_paths(repo, tracked_index=tracked_index),
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


def write_context_bundle(
    context_dir: Path,
    manifest: dict[str, Any],
    repo: Path,
    diff_right: str,
    patch: bytes,
    *,
    env: Mapping[str, str] | None = None,
    integration: Mapping[str, Any] | None = None,
) -> None:
    context_dir = normalize_path(context_dir)
    context_dir.mkdir(parents=True, exist_ok=True)
    try:
        (context_dir / "diff.patch").write_bytes(patch)
        for merge in manifest["merge_commits"]:
            for parent_view in merge["parent_views"]:
                parent = parent_view["sha"]
                parent_patch = run_git(
                    repo,
                    ["diff", *DIFF_FLAGS, "--binary", "--find-renames", parent, merge["sha"], "--"],
                    binary=True,
                )
                filename = f"merge-{merge['sha'][:12]}-parent-{parent_view['position']}.patch"
                (context_dir / filename).write_bytes(
                    parent_patch if isinstance(parent_patch, bytes) else parent_patch.encode()
                )
        if integration:
            (context_dir / "merge-preview-status.txt").write_text(
                str(integration.get("detail") or ""), encoding="utf-8"
            )
            integration_patch = run_git(
                repo,
                [
                    "diff",
                    *DIFF_FLAGS,
                    "--binary",
                    "--find-renames",
                    "--find-copies",
                    str(integration["head_sha"]),
                    str(integration["tree_sha"]),
                    "--",
                ],
                binary=True,
                env=integration["env"],
            )
            (context_dir / "merge-preview.patch").write_bytes(
                integration_patch if isinstance(integration_patch, bytes) else integration_patch.encode()
            )
            for change in integration["changed_files"]:
                path_value = str(change.get("path") or "")
                if (
                    not safe_relative_git_path(path_value)
                    or path_value in manifest.get("submodule_paths", [])
                    or change.get("old_mode") == "160000"
                    or change.get("new_mode") == "160000"
                    or str(change.get("status", "")).startswith("D")
                ):
                    continue
                try:
                    content = run_git(
                        repo,
                        ["cat-file", "blob", f"{integration['tree_sha']}:{path_value}"],
                        binary=True,
                        env=integration["env"],
                    )
                except GitCommandError as exc:
                    raise ReviewContextError(
                        f"無法匯出合併預覽檔案「{path_value}」：{exc.stderr}",
                        hint="請確認 repository objects 可讀取，修復後重新產生審查內容。",
                    ) from exc
                destination = context_dir / "merge-preview-files" / Path(path_value)
                destination.parent.mkdir(parents=True, exist_ok=True)
                encoded_content = content if isinstance(content, bytes) else content.encode()
                destination.write_bytes(encoded_content)
                manifest.setdefault("evidence_files", []).append(
                    {
                        "source": "merge-preview",
                        "path": path_value,
                        "blob_sha": change.get("new_blob"),
                        "sha256": hashlib.sha256(encoded_content).hexdigest(),
                    }
                )
            object_copy = context_dir / "merge-preview-objects"
            source_objects = Path(str(integration["env"]["GIT_OBJECT_DIRECTORY"]))
            shutil.copytree(source_objects, object_copy, dirs_exist_ok=True)
            integration["manifest_info"]["object_directory"] = str(object_copy)
            integration["manifest_info"]["alternate_objects"] = str(
                integration["env"]["GIT_ALTERNATE_OBJECT_DIRECTORIES"]
            )
        (context_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:
        remove_tree(context_dir)
        raise


def pin_branch_refs(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Resolve the requested branches once, before any Repo is sealed."""
    capture_phase("branch-preflight")
    if is_shallow_repository(repo):
        raise ReviewContextError(
            "repository 是 shallow clone，無法可靠判定共同祖先；請先取得完整歷史後再審查。",
            hint="請執行 git fetch --unshallow 取得完整歷史後再重新審查。",
        )

    base_input, head_input = args.base, args.head
    # Verify local branch namespaces before any remote synchronization.
    for value in (base_input, head_input):
        target = remote_target_for_ref(repo, value)
        if target:
            if not git_ok(repo, ["check-ref-format", f"refs/heads/{target.branch}"]):
                raise ReviewContextError(f"無效的遠端分支名稱：{value}")
        elif resolve_commit_ref(repo, value)[0] is None:
            raise ReviewContextError(
                f"找不到本機分支：{value}；僅支援分支，不接受 tag、commit SHA 或 HEAD。",
                hint="請確認本機分支名稱；遠端分支需加上 remote 前綴，例如 origin/main。",
            )
    fetch_refs = [base_input, head_input]
    fetches = fetch_inputs(repo, fetch_refs, not args.no_fetch)
    capture_phase("ref-pinning")
    import review_coordinator
    active = review_coordinator.ACTIVE.get()
    def resolved(value):
        if active and value in active.pins:
            pin = active.pins[value]
            return pin["sha"], pin["ref"]
        return resolve_commit_ref(repo, value)
    base_sha, base_resolved_ref = resolved(base_input)
    if not base_sha:
        raise ReviewContextError(
            f"找不到基礎 ref 的 commit：{base_input}（查找來源：{ref_source_description(repo, base_input)}）",
            hint="請確認分支名稱拼字；遠端分支需加上 remote 前綴（例如 origin/main），本機分支不需要。",
        )
    head_sha, head_resolved_ref = resolved(head_input)
    if not head_sha:
        raise ReviewContextError(
            f"找不到比較 ref 的 commit：{head_input}（查找來源：{ref_source_description(repo, head_input)}）",
            hint="請確認分支名稱拼字；遠端分支需加上 remote 前綴（例如 origin/feature），本機分支不需要。",
        )

    return {"base_sha": base_sha, "head_sha": head_sha,
            "base_resolved_ref": base_resolved_ref, "head_resolved_ref": head_resolved_ref,
            "fetches": fetches, "network_activity": network_activity()}


def _build_manifest(args: argparse.Namespace, *, frozen_refs: Mapping[str, Any] | None = None, before_snapshot=None) -> dict[str, Any]:
    if not args.project or not args.base or not args.head:
        raise ReviewContextError("請同時指定 --project、--base 與 --head。")
    if args.mode != "merge":
        raise ReviewContextError("只支援兩個分支的合併前審查。")

    workspace = normalize_path(args.workspace)
    workspace_file = normalize_path(args.workspace_file) if args.workspace_file else None
    if workspace_file and not workspace_file.exists():
        raise ReviewContextError(f"VS Code workspace 不存在：{workspace_file}")
    repo, repositories = choose_repository(workspace, workspace_file, args.project)
    before = before_snapshot if before_snapshot is not None else snapshot(repo)
    base_input, head_input = args.base, args.head
    pinned = frozen_refs if frozen_refs is not None else pin_branch_refs(repo, args)
    base_sha, head_sha = pinned["base_sha"], pinned["head_sha"]
    base_resolved_ref, head_resolved_ref = pinned["base_resolved_ref"], pinned["head_resolved_ref"]
    fetches = pinned["fetches"]

    merge_base_args = ["merge-base", "--all", base_sha, head_sha]
    merge_base_returncode, merge_base_output, merge_base_error = run_git_capture(repo, merge_base_args)
    merge_bases = [line.strip() for line in merge_base_output.splitlines() if line.strip()]
    if merge_base_returncode not in (0, 1):
        raise GitCommandError(merge_base_args, merge_base_returncode, merge_base_error)
    if args.mode == "merge" and not merge_bases:
        raise ReviewContextError(
            "兩個版本沒有共同祖先，無法建立可靠的比較範圍。",
            hint="請確認兩個分支屬於同一個 Repo 且具有共同歷史。",
        )
    if args.mode == "merge" and len(merge_bases) > 1:
        raise ReviewContextError(
            "兩個版本存在多個共同祖先（criss-cross history），請先指定可接受的歷史或整理分支。",
            hint="請先整理分支歷史，使兩個分支具有唯一共同起點後再審查。",
        )
    merge_base = merge_bases[0] if len(merge_bases) == 1 else None
    diff_left = merge_base if args.mode == "merge" else base_sha
    if diff_left is None:
        raise ReviewContextError("合併前審查需要唯一共同祖先。")

    integration_temporary_dir: Path | None = None
    context_bundle_written = False
    snapshot_env: dict[str, str] | None = None
    review_right = head_sha
    review_scope = "committed"
    try:
        integration: dict[str, Any] | None = None
        integration_env: dict[str, str] | None = None
        if args.mode == "merge":
            integration_temporary_dir = Path(
                tempfile.mkdtemp(prefix="merge-review-integration-")
            )
            integration, integration_env = create_merge_preview(
                repo,
                base_sha,
                head_sha,
                str(merge_base),
                integration_temporary_dir,
            )
        changes = collect_changes(repo, diff_left, review_right, env=snapshot_env)
        merge_commits = merge_commit_records(repo, diff_left, head_sha)
        patch = run_git(
            repo,
            [
                "diff",
                *DIFF_FLAGS,
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
                ["diff", *DIFF_FLAGS, "--check", diff_left, review_right, "--"],
                check=False,
                env=snapshot_env,
            )
        ).strip()
        after = snapshot(repo)
        working_tree_unchanged = before == after
        if not working_tree_unchanged:
            raise ReviewContextError(
                "審查期間工作區、index 或 HEAD 發生變更，已捨棄本次審查結果；請重新執行。",
                hint="請在審查進行中避免編輯、切換分支或提交，完成後重新執行。",
            )
        dirty_submodules = sorted(
            set(before.get("dirty_submodules", []))
            | set(after.get("dirty_submodules", []))
        )
        review_limitations: list[str] = []
        context_gaps: list[str] = []
        if dirty_submodules:
            limitation = (
                "submodule 內部有未提交或未初始化的內容，未納入本次 Git tree 審查："
                + ", ".join(dirty_submodules)
            )
            review_limitations.append(limitation)
            context_gaps.append(limitation)
        changed_submodules = collect_submodule_paths(repo, diff_left, review_right, env=snapshot_env)
        if changed_submodules:
            limitation = (
                "submodule 僅檢查 Git link 版本指標，未檢查內部程式碼："
                + ", ".join(sorted(changed_submodules))
            )
            review_limitations.append(limitation)
            context_gaps.append(limitation)
        if integration and integration["status"] == "conflicts":
            limitation = "基礎版本與比較版本的合併預覽存在衝突；依路徑逐一處理前，合併相容性審查未完成。"
            review_limitations.append(limitation)
            context_gaps.append(limitation)
        elif integration and integration["status"] == "unavailable":
            limitation = "無法產生合併預覽：" + str(integration["detail"])
            review_limitations.append(limitation)
            context_gaps.append(limitation)
        binary_or_submodule_paths = sorted(
            {
                str(change["path"])
                for change in changes
                if bool(change.get("binary"))
            }
            | set(dirty_submodules)
            | changed_submodules
        )

        manifest: dict[str, Any] = {
            "schema_version": 4,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "workspace": str(workspace),
            "workspace_file": str(workspace_file) if workspace_file else None,
            "repositories": [str(item) for item in repositories],
            "project_input": args.project,
            "project": repo.name,
            "repo": str(repo),
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
            "fetches": fetches,
            "network_activity": network_activity(),
            "fetch_status": "completed" if fetches else ("disabled" if args.no_fetch else "not-needed"),
            "working_tree_unchanged": working_tree_unchanged,
            "working_tree_before": before,
            "working_tree_after": after,
            "dirty_submodule_paths": dirty_submodules,
            "review_limitations": review_limitations,
            "context_complete": not context_gaps,
            "context_gaps": context_gaps,
            "review_complete": not context_gaps,
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
                    ["diff", *DIFF_FLAGS, "--stat", "--find-renames", diff_left, review_right],
                    env=snapshot_env,
                )
            ).strip(),
            "binary_or_submodule_paths": binary_or_submodule_paths,
            "submodule_paths": sorted(set(dirty_submodules) | changed_submodules),
            "evidence_files": [],
            "merge_preview": None,
        }
        if integration:
            integration.pop("env", None)
            integration["manifest_info"] = {
                "object_directory": None,
                "alternate_objects": None,
                "cleaned_after_review": True,
            }
            manifest["merge_preview"] = {
                "status": integration["status"],
                "exit_code": integration["exit_code"],
                "detail": str(integration["detail"])[:4000],
                "tree_sha": integration["tree_sha"],
                "changed_files": integration["changed_files"],
                "manifest_info": integration["manifest_info"],
            }
        context_dir = normalize_path(args.context_dir)
        try:
            context_dir.relative_to(repo)
        except ValueError:
            pass
        else:
            raise ReviewContextError(
                "--context-dir 必須位於 repository 外，避免將暫存資料混入審查範圍。"
            )
        manifest["context_cleanup_required"] = True
        manifest["context_cleanup_note"] = (
            "本次 context 位於系統暫存目錄；審查結束或中止時請執行 "
            "review_session.py cleanup --context-dir。"
        )
        manifest["context_dir"] = str(context_dir)
        write_context_bundle(
            context_dir,
            manifest,
            repo,
            review_right,
            patch if isinstance(patch, bytes) else patch.encode(),
            env=snapshot_env,
            integration=(
                {
                    **integration,
                    "head_sha": head_sha,
                    "env": integration_env,
                }
                if integration and integration_env and integration["tree_sha"]
                else None
            ),
        )
        return manifest
    finally:
        if integration_temporary_dir:
            remove_tree(integration_temporary_dir)


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("逾時秒數必須為正數。") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("逾時秒數必須大於零。")
    return timeout


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    import review_session
    from review_workspace import build

    activity_token = NETWORK_ACTIVITY.set(empty_network_activity())
    phase_token = CAPTURE_PHASE.set("session-preflight")
    try:
        timeout = positive_timeout(str(args.git_timeout))
        session_dir = (review_session.validate_session(args._prepared_context)
                       if getattr(args, "_prepared_context", None)
                       else review_session.create_session(args.context_dir))
        args.context_dir = str(session_dir)
        token = GIT_TIMEOUT_SECONDS.set(timeout)
        succeeded = False
        try:
            capture_phase("local-preflight")
            result = build(args, sys.modules[__name__])
            succeeded = True
            return result
        finally:
            GIT_TIMEOUT_SECONDS.reset(token)
            if not succeeded:
                review_session.cleanup_session(session_dir)
    except (ReviewContextError, OSError, ValueError) as exc:
        exc.phase = CAPTURE_PHASE.get()
        exc.network_activity = network_activity()
        raise
    finally:
        NETWORK_ACTIVITY.reset(activity_token)
        CAPTURE_PHASE.reset(phase_token)


def context_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project the persisted context into a compact, distinctly typed index."""
    fields = (
        "generated_at", "run_id", "workspace", "workspace_roots", "workspace_file",
        "workspace_file_sha256", "review_basis", "scope", "target", "extension_enabled",
        "extension_reason", "selected_repo", "seed_repos", "context_dir",
        "context_cleanup_required", "required_coverage", "context_complete", "context_gaps",
        "snapshot_policy", "refs_pinned_at", "capture_completed_at", "workspace_instructions",
        "merge_parent_policy", "required_merge_views", "network_activity", "coverage_policy",
    )
    summary = {key: manifest[key] for key in fields if key in manifest}
    summary.update(schema="merge-reviewer-workspace-summary/v1",
                   context_schema=manifest["schema"],
                   manifest_path=str(Path(manifest["context_dir"]) / "manifest.json"),
                   repositories=[])
    native_fields = (
        "base_input", "head_input", "base_resolved_ref", "head_resolved_ref", "base_sha",
        "head_sha", "mode", "merge_base", "diff_base", "review_scope", "review_right",
        "fetches", "fetch_status", "network_activity", "working_tree_unchanged", "dirty_submodule_paths",
        "review_limitations", "context_complete", "context_gaps", "review_complete",
        "changed_file_count", "commit_count", "diff_patch_bytes", "diff_check", "diff_stat",
        "binary_or_submodule_paths", "submodule_paths",
    )
    for repo in manifest["repositories"]:
        item = {key: repo[key] for key in
                ("id", "name", "path", "role", "state", "review_source", "head_sha", "diff_base", "limitations", "snapshot", "instructions")
                if key in repo}
        item["sources"] = {
            name: {"ref": source["ref"], "tree_sha": source["tree_sha"],
                   "file_count": len(source["files"])}
            for name, source in repo["sources"].items()
        }
        item["changed_file_count"] = len(repo["changed_files"])
        item["patches"] = list(repo["patches"])
        native = repo.get("native")
        if native:
            item["native"] = {key: native[key] for key in native_fields if key in native}
            merges = []
            for merge in native["merge_commits"]:
                parents = []
                for parent in merge["parent_views"]:
                    patch = (f"repos/{repo['id']}/native/merge-{merge['sha'][:12]}"
                             f"-parent-{parent['position']}.patch")
                    parents.append({"position": parent["position"], "sha": parent["sha"],
                                    "changed_file_count": len(parent["changes"]), "patch": patch})
                    if patch not in item["patches"]:
                        item["patches"].append(patch)
                merges.append({"sha": merge["sha"], "subject": merge["subject"],
                               "parents": merge["parents"], "parent_views": parents})
            item["native"]["merge_commits"] = merges
            preview = native.get("merge_preview")
            item["native"]["merge_preview"] = (
                {**{key: value for key, value in preview.items() if key != "changed_files"},
                 "changed_file_count": len(preview["changed_files"])} if preview else None
            )
        summary["repositories"].append(item)
    return summary


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare one Repo's base/head branch evidence; Codex performs the review."
    )
    parser.add_argument("--project", required=True, help="The single Repo name or exact path.")
    parser.add_argument("--base", required=True, help="Base local or explicitly remote-qualified branch.")
    parser.add_argument("--head", required=True, help="Comparison local or explicitly remote-qualified branch.")
    parser.add_argument("--workspace", default=os.getcwd(), help="Declared root used for Repo discovery.")
    parser.add_argument("--workspace-file", help="Actual VS Code .code-workspace boundary.")
    parser.add_argument("--no-fetch", action="store_true", help="Offline preparation; local branches only.")
    parser.add_argument("--format", choices=("json",), default="json")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--summary", action="store_true", help="Print a compact context index; persist the complete manifest as usual.")
    parser.add_argument("--summary-v2", action="store_true", help="Print the compact v2 context index")
    parser.add_argument("--context-dir", help="Fresh evidence directory inside system temporary storage.")
    parser.add_argument("--git-timeout", type=positive_timeout, default=180.0, help="Seconds per Git command (default: 180).")
    arguments = parser.parse_args(argv)
    arguments.mode = "merge"
    return arguments

def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = build_manifest(args)
    except (ReviewContextError, OSError, ValueError) as exc:
        print(f"merge-reviewer: error: {exc}", file=sys.stderr)
        hint = getattr(exc, "hint", None)
        if hint:
            print(f"merge-reviewer: 建議：{hint}", file=sys.stderr)
        print("merge-reviewer: diagnostic: " + json.dumps(capture_diagnostic(exc), ensure_ascii=False),
              file=sys.stderr)
        return 2
    indent = 2 if args.pretty else None
    result = context_summary(manifest) if args.summary or args.summary_v2 else manifest
    if args.summary_v2:
        result["schema"] = "merge-reviewer-workspace-summary/v2"
    print(json.dumps(result,
                     ensure_ascii=False, indent=indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
