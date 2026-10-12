#!/usr/bin/env python3
"""Portable fixed-source Repo/Group contexts and auditable impact searches.

Preparation freezes versions, not semantic conclusions. Codex follows contracts
with search/read and records the resulting impact graph in its review draft.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sys
import uuid

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

CONTEXT = "merge-reviewer-workspace-context/v1"
RESULT = "merge-reviewer-workspace-result/v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_path(root: Path, value: str) -> Path:
    require(isinstance(value, str) and bool(value), "path must be nonempty")
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    require(not posix.is_absolute() and not windows.is_absolute() and not windows.drive
            and "\\" not in value and all(p not in ("", ".", "..") for p in value.split("/")),
            f"invalid relative path: {value!r}")
    path = root.joinpath(*posix.parts)
    require(path.resolve().is_relative_to(root.resolve()), "path escapes context")
    probe = path
    while probe != root:
        require(not is_link(probe), f"linked evidence path: {probe}")
        probe = probe.parent
    return path


def is_link(path: Path) -> bool:
    import stat
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def save(context: Path, manifest: dict):
    # Search is sequential; atomic replacement avoids a half-written ledger.
    temporary = context / (".manifest-" + uuid.uuid4().hex + ".json")
    try:
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, context / "manifest.json")
    finally:
        temporary.unlink(missing_ok=True)


def load(context: Path) -> dict:
    from review_session import validate_session
    context = validate_session(context)
    manifest = json.loads(safe_path(context, "manifest.json").read_text(encoding="utf-8"))
    require(manifest.get("schema") == CONTEXT, "not a portable workspace context")
    return manifest


def repository(manifest: dict, repo_id: str) -> dict:
    matches = [r for r in manifest["repositories"] if r["id"] == repo_id]
    require(len(matches) == 1, f"unknown/ambiguous Repo ID: {repo_id}")
    return matches[0]


def source_environment(context: Path, repo: dict, source: dict, api) -> dict:
    storage = source.get("objects")
    if not storage:
        return {}
    objects = safe_path(context, storage)
    require(objects.is_dir(), "fixed object storage is missing")
    return {"GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(api.git_path(Path(repo["path"]), "objects"))}


def fixed_bytes(context: Path, repo: dict, name: str, path: str, api) -> bytes:
    safe_path(context, path)
    source = repo.get("sources", {}).get(name)
    require(source is not None, f"unknown fixed source: {repo['id']}/{name}")
    matches = [f for f in source["files"] if f["path"] == path]
    require(len(matches) == 1 and matches[0]["kind"] == "blob", "path is not a fixed blob")
    env = source_environment(context, repo, source, api)
    data = api.run_git(Path(repo["path"]), ["cat-file", "blob", f"{source['ref']}:{path}"], binary=True, env=env)
    digest = str(api.run_git(Path(repo["path"]), ["hash-object", "--stdin"], input_bytes=data)).strip()
    require(digest == matches[0]["blob_sha"], "fixed blob identity mismatch")
    return data


def add_source(context: Path, repo: dict, name: str, reference: str, api, *, env=None):
    path = Path(repo["path"])
    env = env or {}
    tree = str(api.run_git(path, ["rev-parse", f"{reference}^{{tree}}"], env=env)).strip()
    raw = api.run_git(path, ["ls-tree", "-r", "-z", tree], binary=True, env=env)
    files = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, file_path = item.split(b"\t", 1)
        mode, kind, blob = meta.decode("ascii").split()
        relative = file_path.decode("utf-8", errors="strict")
        safe_path(context, relative)
        files.append({"path": relative, "mode": mode, "kind": kind, "blob_sha": blob})
    storage = env.get("GIT_OBJECT_DIRECTORY")
    repo["sources"][name] = {"ref": reference, "tree_sha": tree, "files": files,
                             "objects": Path(storage).relative_to(context).as_posix() if storage else None}


def local_snapshot(context: Path, repo: dict, target: str, api):
    path = Path(repo["path"])
    storage = safe_path(context, f"repos/{repo['id']}/snapshot")
    storage.mkdir(parents=True)
    env = api.snapshot_environment(path, storage)
    require(not str(api.run_git(path, ["ls-files", "--unmerged", "-z"])), "unresolved index conflict")
    empty = str(api.run_git(path, ["hash-object", "-w", "-t", "tree", "--stdin"], input_bytes=b"", env=env)).strip()
    head = repo["head_sha"]
    base = head or empty
    add_source(context, repo, "base", base, api, env=env)
    index = api.git_path(path, "index")
    if index.exists():
        shutil.copyfile(index, env["GIT_INDEX_FILE"])
        shared = str(api.run_git(path, ["rev-parse", "--shared-index-path"])).strip()
        if shared:
            shared_path = Path(shared) if Path(shared).is_absolute() else path / shared
            shutil.copyfile(shared_path, storage / shared_path.name)
    else:
        api.run_git(path, ["read-tree", "--empty"], env=env)
    if target == "uncommitted":
        # Starting from the real index preserves explicitly force-staged ignored files.
        flags = api.run_git(path, ["ls-files", "-v", "-z"], binary=True, env=env).split(b"\0")
        assumed = [row[2:] for row in flags if row[:1].islower()]
        present_skipped = [row[2:] for row in flags if row[:1] in (b"S", b"s")
                           and os.path.lexists(path / os.fsdecode(row[2:]))]
        for option, paths in (("--no-assume-unchanged", assumed), ("--no-skip-worktree", present_skipped)):
            if paths:
                api.run_git(path, ["update-index", option, "-z", "--stdin"],
                            input_bytes=b"\0".join(paths) + b"\0", env=env)
        # Present files outside sparse-checkout still count as saved local input.
        # Absent skip-worktree paths keep their index entries, not fake deletions.
        api.run_git(path, ["-c", "core.sparseCheckout=false", "add", "--all", "--", "."], env=env)
        tracked_reports = set(str(api.run_git(path, ["ls-tree", "-r", "--name-only", "-z", base,
                                                   "--", "review-reports"], env=env)).split("\0"))
        reports = str(api.run_git(path, ["ls-files", "-z", "--", "review-reports"], env=env)).split("\0")
        for report in reports:
            if report and report not in tracked_reports:
                api.run_git(path, ["update-index", "--force-remove", "--", report], env=env)
    tree = str(api.run_git(path, ["write-tree"], env=env)).strip()
    name = "index" if target == "staged" else "working-tree"
    add_source(context, repo, name, tree, api, env=env)
    repo["review_source"] = name
    repo["changed_files"] = [{**c, "source": name} for c in api.collect_changes(path, base, tree, env=env)]
    patch = api.run_git(path, ["diff", *api.DIFF_FLAGS, "--binary", "--find-renames", "--find-copies",
                               base, tree, "--"], binary=True, env=env)
    destination = safe_path(context, f"repos/{repo['id']}/{name}.patch")
    destination.write_bytes(patch)
    repo["patches"] = [destination.relative_to(context).as_posix()]
    repo["diff_base"] = base


def branch_snapshot(context: Path, repo: dict, args, api):
    native_args = copy.copy(args)
    native_args.workspace, native_args.workspace_file = repo["path"], None
    native_args.project = repo["path"]
    native_args.context_dir = str(safe_path(context, f"repos/{repo['id']}/native"))
    native = api._build_manifest(native_args)
    repo["native"] = native
    repo["review_source"] = "head"
    for name, reference in (("head", native["head_sha"]), ("base", native["base_sha"])):
        add_source(context, repo, name, reference, api)
    if native["merge_base"]:
        add_source(context, repo, "merge-base", native["merge_base"], api)
    for merge in native["merge_commits"]:
        for parent in merge["parents"]:
            name = "parent-" + parent
            if name not in repo["sources"]:
                add_source(context, repo, name, parent, api)
    repo["changed_files"] = [{**c, "source": "head"} for c in native["changed_files"]]
    repo["patches"] = [f"repos/{repo['id']}/native/diff.patch"]
    preview = native.get("merge_preview")
    if preview and preview.get("tree_sha"):
        info = preview["manifest_info"]
        env = {"GIT_OBJECT_DIRECTORY": info["object_directory"],
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": info["alternate_objects"]}
        add_source(context, repo, "merge-preview", preview["tree_sha"], api, env=env)
        repo["changed_files"].extend({**c, "source": "merge-preview"} for c in preview["changed_files"])
        repo["patches"].append(f"repos/{repo['id']}/native/merge-preview.patch")
    repo["limitations"].extend(native["context_gaps"])
    repo["diff_base"] = native["diff_base"]


def validate_root(path: Path, api) -> Path:
    require(not is_link(path), f"linked Repo root: {path}")
    resolved = path.resolve(strict=True)
    require(str(api.run_git(resolved, ["rev-parse", "--show-toplevel"])).strip()
            and Path(str(api.run_git(resolved, ["rev-parse", "--show-toplevel"])).strip()).resolve() == resolved,
            "Repo path must be its Git root")
    return resolved


def build(args, api) -> dict:
    target = args.target or "branch"
    require(not (getattr(args, "mr_task", None) or getattr(args, "mr_context", None)
                 or getattr(args, "mr_context_base64", None) or getattr(args, "megin_receipt", None)),
            "MR/receipt binding uses the compatible branch workflow; omit --scope/--target")
    require(not getattr(args, "supporting_sources", None), "--supporting-sources is legacy Group-quick-only; use fixed-source search/read")
    require(not args.include_working_tree, "use --target uncommitted, or omit --scope/--target for legacy quick + working-tree")
    if target != "branch":
        require(not (args.base or args.head or args.quick or args.remote),
                "staged/uncommitted compare local HEAD only; omit base/head/quick/remote")
        require(args.mode == "merge", "--mode direct applies only to branch review")
    declared_root = Path(args.group_root or args.workspace).expanduser().absolute()
    require(not is_link(declared_root), "workspace root cannot be a link or junction")
    root = api.normalize_path(declared_root)
    workspace_file = api.normalize_path(args.workspace_file) if args.workspace_file else None
    roots = api.workspace_roots(root, workspace_file)
    # Check declared roots before discovery normalizes them and loses link identity.
    require(all(not is_link(r) for r in roots), "workspace roots cannot be links or junctions")
    paths = api.discover_repositories(root, workspace_file)
    require(all(any(p.is_relative_to(r) for r in roots) for p in paths),
            "Repo canonical path escapes the declared workspace roots")
    require(bool(paths), "找不到 Git Repo；請指定正確工作區。")
    scope = args.scope or "auto"
    if scope == "auto":
        scope = "group" if args.group_root or len(paths) > 1 else "repo"
    if args.project or len(paths) == 1:
        seed, _ = api.choose_repository(root, workspace_file, args.project)
        seeds = [seed]
    else:
        require(scope == "group" and target != "branch",
                "請指定起點 Repo (--project)：" + ", ".join(str(p) for p in paths))
        seeds = paths[:]
    if scope == "repo":
        paths = seeds[:]
    context = Path(args.context_dir)
    manifest = {"schema": CONTEXT, "generated_at": datetime.now(timezone.utc).isoformat(),
                "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8],
                "workspace": str(workspace_file.parent if workspace_file and not args.group_root else root),
                "workspace_file": str(workspace_file) if workspace_file else None,
                "scope": scope, "target": target, "repositories": [], "seed_repos": [],
                "searches": [], "context_gaps": [], "context_dir": str(context),
                "context_cleanup_required": True}
    for index, candidate in enumerate(paths):
        repo = {"id": f"repo-{index:03}", "name": candidate.name, "path": str(candidate),
                "role": "seed" if candidate in seeds else "candidate", "sources": {},
                "changed_files": [], "patches": [], "limitations": [], "state": "captured"}
        manifest["repositories"].append(repo)
        if candidate in seeds:
            manifest["seed_repos"].append(repo["id"])
        try:
            path = validate_root(candidate, api)
            repo["before"] = api.snapshot(path)
            repo["head_sha"] = api.verified_commit(path, "HEAD")
            safe_path(context, f"repos/{repo['id']}").mkdir(parents=True)
            if target == "branch" and repo["role"] == "seed":
                branch_snapshot(context, repo, args, api)
            elif target == "branch":
                require(repo["head_sha"], "related Repo has no HEAD commit")
                repo["review_source"] = "head"
                add_source(context, repo, "head", repo["head_sha"], api)
            else:
                local_snapshot(context, repo, target, api)
            after = api.snapshot(path)
            require(repo["before"] == after, "Repo changed during snapshot; rebuild context")
            if after["dirty_submodules"]:
                repo["limitations"].append("未擷取子模組內部修改或未初始化內容。")
        except (ValueError, OSError, api.ReviewContextError) as exc:
            if target == "branch" and repo["role"] == "seed":
                raise
            repo["state"] = "error"
            repo["limitations"].append(str(exc))
        if repo["role"] == "seed":
            manifest["context_gaps"].extend(f"{repo['id']}: {gap}" for gap in repo["limitations"])
    # Without an explicit seed, only locally changed/error Repos start a chain.
    if not args.project and target != "branch" and len(seeds) > 1:
        manifest["seed_repos"] = [r["id"] for r in manifest["repositories"] if r["changed_files"] or r["state"] == "error"]
        for repo in manifest["repositories"]:
            repo["role"] = "seed" if repo["id"] in manifest["seed_repos"] else "candidate"
    manifest["required_coverage"] = required_coverage(manifest)
    manifest["context_complete"] = not manifest["context_gaps"]
    save(context, manifest)
    return manifest


def required_coverage(manifest: dict) -> list[dict]:
    required = []
    for repo in manifest["repositories"]:
        if repo["id"] not in manifest["seed_repos"]:
            continue
        for change in repo["changed_files"]:
            for path in dict.fromkeys([change["path"], change.get("old_path")]):
                if path:
                    item = {"repo": repo["id"], "source": change["source"], "path": path}
                    if item not in required:
                        required.append(item)
    return required


def search(context: Path, manifest: dict, query: str, repo_ids: list[str] | None, api, source_name=None) -> dict:
    require(bool(query), "search query must be nonempty")
    selected = repo_ids or [r["id"] for r in manifest["repositories"]]
    require(len(selected) == len(set(selected)), "duplicate search Repo IDs")
    results = []
    for repo_id in selected:
        repo = repository(manifest, repo_id)
        name = source_name or repo.get("review_source")
        record = {"id": f"S-{len(manifest['searches']) + 1:04}", "repo": repo_id, "source": name,
                  "query": query, "matches": [], "status": "complete"}
        try:
            source = repo["sources"][name]
            env = source_environment(context, repo, source, api)
            code, output, error = api.run_git_capture(Path(repo["path"]),
                ["grep", "-n", "-I", "--no-textconv", "-F", "-e", query, source["ref"], "--"], env=env)
            require(code in (0, 1), "fixed-source search failed: " + error)
            record["ref"] = source["ref"]
            record["output"] = output
            # Keep exact raw output as well: Git quoting may escape unusual paths.
            record["matches"] = output.splitlines()
        except (KeyError, ValueError, api.ReviewContextError) as exc:
            record.update(status="unresolved", reason=str(exc))
        manifest["searches"].append(record)
        results.append(record)
    save(context, manifest)
    return {"searches": results}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("read", "search", "inventory"))
    parser.add_argument("--context-dir", type=Path, required=True)
    parser.add_argument("--repo", action="append")
    parser.add_argument("--source")
    parser.add_argument("--path")
    parser.add_argument("--query")
    parser.add_argument("--git-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    import git_review_context as api
    context = args.context_dir.resolve()
    token = api.GIT_TIMEOUT_SECONDS.set(api.positive_timeout(str(args.git_timeout)))
    try:
        manifest = load(context)
        if args.command == "search":
            value = search(context, manifest, args.query, args.repo, api, args.source)
        elif args.command == "read":
            require(args.repo and len(args.repo) == 1 and args.path, "read needs one --repo and --path")
            repo = repository(manifest, args.repo[0])
            name = args.source or repo["review_source"]
            content = fixed_bytes(context, repo, name, args.path, api)
            require(b"\0" not in content, "binary blob; record a coverage limitation")
            value = {"repo": repo["id"], "source": name, "ref": repo["sources"][name]["ref"], "path": args.path,
                     "sha256": hashlib.sha256(content).hexdigest(),
                     "lines": [{"line": i, "text": s} for i, s in enumerate(content.decode("utf-8", errors="replace").splitlines(), 1)]}
        else:
            value = {"repositories": [{"repo": r["id"], "name": r["name"], "path": r["path"], "role": r["role"],
                       "sources": {k: {"ref": v["ref"], "files": v["files"]} for k, v in r["sources"].items()}}
                      for r in manifest["repositories"] if not args.repo or r["id"] in args.repo]}
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, api.ReviewContextError) as exc:
        print(f"workspace-review: {exc}", file=sys.stderr)
        return 2
    finally:
        api.GIT_TIMEOUT_SECONDS.reset(token)


if __name__ == "__main__":
    raise SystemExit(main())
