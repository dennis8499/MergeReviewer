#!/usr/bin/env python3
"""Collect immutable Git facts for the merge-reviewer skill.

The script deliberately uses Git object IDs for all comparison data. It does
not checkout, merge, reset, stage, or edit tracked files. Fetching a remote
branch is the only Git ref mutation performed by default.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
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
) -> str | bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        raise GitCommandError(args, completed.returncode, stderr)
    if binary:
        return completed.stdout
    return completed.stdout.decode("utf-8", errors="replace")


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
    """Resolve an input ref, falling back to a fetched remote-tracking ref."""
    direct = verified_commit(repo, ref)
    if direct:
        return direct, ref
    target = remote_target_for_ref(repo, ref)
    if target:
        remote_ref = f"refs/remotes/{target.remote}/{target.branch}"
        remote_commit = verified_commit(repo, remote_ref)
        if remote_commit:
            return remote_commit, remote_ref
    return None, ref


def local_branch_exists(repo: Path, branch: str) -> bool:
    return git_ok(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])


def tracking_remote_for_local_branch(repo: Path, branch: str) -> RemoteTarget | None:
    if not local_branch_exists(repo, branch):
        return None
    upstream = str(
        run_git(
            repo,
            ["for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}"],
            check=False,
        )
    ).strip()
    if not upstream or "/" not in upstream:
        return None
    remote, remote_branch = upstream.split("/", 1)
    if remote not in remote_names(repo):
        return None
    return RemoteTarget(remote, remote_branch, branch)


def remote_target_for_ref(repo: Path, ref: str) -> RemoteTarget | None:
    remotes = remote_names(repo)
    normalized = ref
    if normalized.startswith("refs/remotes/"):
        normalized = normalized[len("refs/remotes/") :]
    if "/" in normalized:
        first, remainder = normalized.split("/", 1)
        if first in remotes:
            return RemoteTarget(first, remainder, ref)
    local_branch = ref.removeprefix("refs/heads/")
    if local_branch_exists(repo, local_branch):
        return tracking_remote_for_local_branch(repo, local_branch)
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", ref) and verified_commit(repo, ref):
        return None

    matches = [
        RemoteTarget(remote, local_branch, ref)
        for remote in remotes
        if git_ok(
            repo,
            [
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/remotes/{remote}/{local_branch}",
            ],
        )
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        choices = ", ".join(f"{item.remote}/{item.branch}" for item in matches)
        raise ReviewContextError(f"分支「{ref}」存在多個 remote 候選：{choices}；請使用明確 remote ref。")
    if len(remotes) == 1 and ref and not ref.startswith("refs/"):
        return RemoteTarget(remotes[0], local_branch, ref)
    if len(remotes) > 1 and ref and not ref.startswith("refs/"):
        choices = ", ".join(f"{remote}/{ref}" for remote in remotes)
        raise ReviewContextError(f"無法判斷分支「{ref}」所屬 remote；候選：{choices}。請使用明確 remote ref。")
    return None


def fetch_inputs(repo: Path, refs: Iterable[str], enabled: bool) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if not enabled:
        return actions
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        target = remote_target_for_ref(repo, ref)
        if not target or (target.remote, target.branch) in seen:
            continue
        seen.add((target.remote, target.branch))
        try:
            run_git(repo, ["fetch", "--no-tags", "--no-prune", target.remote, target.branch])
        except GitCommandError as exc:
            raise ReviewContextError(
                f"fetch {target.remote}/{target.branch} 失敗（輸入 {target.input_ref}）：{exc.stderr}"
            ) from exc
        actions.append(
            {"remote": target.remote, "branch": target.branch, "input_ref": target.input_ref}
        )
    return actions


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


def collect_changes(repo: Path, left: str, right: str) -> list[dict[str, Any]]:
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


def snapshot(repo: Path) -> dict[str, str]:
    head = str(run_git(repo, ["rev-parse", "--verify", "HEAD"], check=False)).strip()
    branch = str(run_git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)).strip()
    status = str(run_git(repo, ["status", "--porcelain=v1", "--untracked-files=all"], check=False))
    return {"head": head, "branch": branch, "status": status}


def write_context_bundle(
    context_dir: Path,
    manifest: dict[str, Any],
    repo: Path,
    diff_left: str,
    diff_right: str,
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
    )
    (context_dir / "diff.patch").write_bytes(patch if isinstance(patch, bytes) else patch.encode())
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


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    workspace = normalize_path(args.workspace)
    workspace_file = normalize_path(args.workspace_file) if args.workspace_file else None
    if workspace_file and not workspace_file.exists():
        raise ReviewContextError(f"VS Code workspace 不存在：{workspace_file}")
    repo, repositories = choose_repository(workspace, workspace_file, args.project)

    before = snapshot(repo)
    fetches = fetch_inputs(repo, [args.base, args.head], not args.no_fetch)
    base_sha, base_resolved_ref = resolve_commit_ref(repo, args.base)
    head_sha, head_resolved_ref = resolve_commit_ref(repo, args.head)
    if not base_sha:
        raise ReviewContextError(f"找不到基礎 ref 的 commit：{args.base}")
    if not head_sha:
        raise ReviewContextError(f"找不到比較 ref 的 commit：{args.head}")

    merge_bases = [
        line.strip()
        for line in str(run_git(repo, ["merge-base", "--all", base_sha, head_sha])).splitlines()
        if line.strip()
    ]
    if not merge_bases:
        raise ReviewContextError("兩個版本沒有共同祖先，無法建立可靠的比較範圍。")
    if len(merge_bases) > 1:
        raise ReviewContextError(
            "兩個版本存在多個共同祖先（criss-cross history），請先指定可接受的歷史或整理分支。"
        )
    merge_base = merge_bases[0]
    diff_left = merge_base if args.mode == "merge" else base_sha
    changes = collect_changes(repo, diff_left, head_sha)
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
            head_sha,
            "--",
        ],
        binary=True,
    )
    patch_size = len(patch) if isinstance(patch, bytes) else len(patch.encode())
    diff_check = str(run_git(repo, ["diff", "--check", diff_left, head_sha, "--"], check=False)).strip()
    after = snapshot(repo)
    working_tree_unchanged = before == after

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(workspace),
        "workspace_file": str(workspace_file) if workspace_file else None,
        "repositories": [str(item) for item in repositories],
        "project_input": args.project,
        "project": repo.name,
        "repo": str(repo),
        "base_input": args.base,
        "head_input": args.head,
        "base_resolved_ref": base_resolved_ref,
        "head_resolved_ref": head_resolved_ref,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "mode": args.mode,
        "merge_base": merge_base,
        "diff_base": diff_left,
        "fetches": fetches,
        "fetch_status": "completed" if fetches else "not-needed",
        "working_tree_unchanged": working_tree_unchanged,
        "working_tree_before": before,
        "working_tree_after": after,
        "changed_files": changes,
        "changed_file_count": len(changes),
        "merge_commits": merge_commits,
        "commit_count": int(
            str(run_git(repo, ["rev-list", "--count", f"{diff_left}..{head_sha}"])).strip() or "0"
        ),
        "diff_patch_bytes": patch_size,
        "diff_check": diff_check,
        "diff_stat": str(run_git(repo, ["diff", "--stat", "--find-renames", diff_left, head_sha])).strip(),
        "binary_or_submodule_paths": [
            str(change["path"])
            for change in changes
            if bool(change.get("binary")) or str(change.get("status", "")).startswith("T")
        ],
    }
    if args.context_dir:
        context_dir = normalize_path(args.context_dir)
        manifest["context_dir"] = str(context_dir)
        write_context_bundle(context_dir, manifest, repo, diff_left, head_sha)
    return manifest


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect immutable Git context for Merge Reviewer.")
    parser.add_argument("--workspace", default=os.getcwd(), help="Workspace root to search for repositories.")
    parser.add_argument("--workspace-file", help="Optional VS Code .code-workspace file.")
    parser.add_argument("--project", help="Repository folder name or path.")
    parser.add_argument("--base", required=True, help="Base branch, remote ref, tag, or commit.")
    parser.add_argument("--head", required=True, help="Comparison branch, remote ref, tag, or commit.")
    parser.add_argument("--mode", choices=("merge", "direct"), default="merge")
    parser.add_argument("--no-fetch", action="store_true", help="Do not fetch remote branches.")
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
