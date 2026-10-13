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
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sys
import uuid

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

CONTEXT = "merge-reviewer-workspace-context/v2"
RESULT = "merge-reviewer-workspace-result/v2"
LEGACY_CONTEXT = "merge-reviewer-workspace-context/v1"
LEGACY_RESULT = "merge-reviewer-workspace-result/v1"


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
    require(manifest.get("schema") in (CONTEXT, LEGACY_CONTEXT), "not a portable workspace context")
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
    storage.mkdir(parents=True, exist_ok=True)
    env = api.snapshot_environment(path, storage)
    require(not str(api.run_git(path, ["ls-files", "--unmerged", "-z"])), "unresolved index conflict")
    empty = str(api.run_git(path, ["hash-object", "-w", "-t", "tree", "--stdin"], input_bytes=b"", env=env)).strip()
    head = repo["head_sha"]
    base = head or empty
    add_source(context, repo, "base", base, api, env=env)
    index = api.git_path(path, "index")
    if index.exists():
        # Preserve its timestamp: a newly dated copy can hide a same-size racy
        # working edit by making stale cache entries look older than the index.
        shutil.copy2(index, env["GIT_INDEX_FILE"])
        shared = str(api.run_git(path, ["rev-parse", "--shared-index-path"])).strip()
        if shared:
            shared_path = Path(shared) if Path(shared).is_absolute() else path / shared
            shutil.copy2(shared_path, storage / shared_path.name)
    else:
        api.run_git(path, ["read-tree", "--empty"], env=env)
    if target in ("uncommitted", "quick"):
        if target == "quick":
            index_tree = str(api.run_git(path, ["write-tree"], env=env)).strip()
            add_source(context, repo, "index", index_tree, api, env=env)
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
    if target == "quick":
        repo["changed_files"] = [
            *[{**c, "source": "index"} for c in api.collect_changes(path, base, index_tree, env=env)],
            *repo["changed_files"],
        ]
        index_patch = safe_path(context, f"repos/{repo['id']}/index.patch")
        index_patch.write_bytes(api.run_git(path, ["diff", *api.DIFF_FLAGS, "--binary", "--find-renames",
                                                  base, index_tree, "--"], binary=True, env=env))
        repo["patches"].insert(0, index_patch.relative_to(context).as_posix())
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
    require(not (args.group_root and args.workspace_file),
            "an explicit Group root cannot be narrowed by --workspace-file; use one declared boundary")
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
        scope = "group" if args.group_root or api.is_group_workspace(root, workspace_file, paths) else "repo"
    if scope == "group" and not args.target and args.quick and not args.base and not args.head:
        target = "quick"
    require(not args.include_working_tree or target == "quick", "use --target uncommitted for saved files")
    if target != "branch":
        require(not (args.base or args.head or (args.quick and target != "quick") or args.remote),
                "staged/uncommitted compare local HEAD only; omit base/head/quick/remote")
        require(args.mode == "merge", "--mode direct applies only to branch review")
    delivery_seeds = getattr(args, "delivery_seeds", None)
    if delivery_seeds:
        seeds = [Path(item["repo_path"]).resolve() for item in delivery_seeds]
        require(set(seeds) <= set(paths), "receipt seeds are outside declared Group/workspace")
    elif args.project or len(paths) == 1:
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
                "workspace_file_sha256": hashlib.sha256(workspace_file.read_bytes()).hexdigest() if workspace_file else None,
                "scope": scope, "target": target, "repositories": [], "seed_repos": [],
                "searches": [], "changed_contracts": [], "context_gaps": [], "context_dir": str(context),
                "context_cleanup_required": True}
    if getattr(args, "mr_binding", None):
        manifest["mr_binding"] = args.mr_binding
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
                branch_args = copy.copy(args)
                if delivery_seeds:
                    seed = next(s for s in delivery_seeds if Path(s["repo_path"]).resolve() == path)
                    branch_args.base, branch_args.head = seed["base_commit"], seed["head_commit"]
                    branch_args.quick, branch_args.no_fetch = False, True
                branch_snapshot(context, repo, branch_args, api)
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
    if getattr(args, "supporting_sources", None):
        require(target == "quick", "supplemental committed sources apply only to Group quick")
        capture_supplemental(context, manifest, args.supporting_sources, api)
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


def capture_supplemental(context, manifest, request_file, api):
    """Keep historical committed evidence available without replacing live candidates."""
    requests = json.loads(Path(request_file).read_text(encoding="utf-8"))
    require(isinstance(requests, list), "supporting sources must be an array")
    seen = set()
    for item in requests:
        require(isinstance(item, dict) and set(item) == {"repo", "commit", "paths"}, "invalid supporting source")
        matches = [r for r in manifest["repositories"] if item["repo"] in (r["id"], r["name"], r["path"])]
        require(len(matches) == 1, "supporting Repo must identify one captured candidate")
        repo = matches[0]
        commit = item["commit"]
        require(isinstance(commit, str) and api.verified_commit(Path(repo["path"]), commit) == commit,
                "supporting source needs an available full commit ID")
        name = "supporting-" + commit
        if name not in repo["sources"]:
            add_source(context, repo, name, commit, api)
        require(isinstance(item["paths"], list) and item["paths"], "supporting paths must not be empty")
        for path in item["paths"]:
            require((repo["id"], commit, path) not in seen, "duplicate supporting source")
            seen.add((repo["id"], commit, path))
            fixed_bytes(context, repo, name, path, api)


def contract_rows(manifest):
    values = manifest.get("changed_contracts", [])
    require(isinstance(values, list), "changed contracts must be an array")
    return {c["id"]: c for c in values}


def validate_contract(context, manifest, item, api):
    """Anchor reviewer-selected contracts before searching their names or aliases."""
    require(manifest.get("schema") == CONTEXT, "contract registration needs context v2")
    require(isinstance(item, dict), "contract must be an object")
    contracts = contract_rows(manifest)
    identifier = item.get("id")
    require(isinstance(identifier, str) and identifier and identifier not in contracts, "duplicate/empty contract ID")
    require(item.get("kind") in ("api", "event", "package", "data", "table", "config", "behavior"), "invalid contract kind")
    require(isinstance(item.get("symbol"), str) and item["symbol"], "contract symbol is required")
    root = item.get("root")
    require(root in manifest["required_coverage"], "contract root must identify a selected change")
    parents = item.get("parent_ids", [])
    require(isinstance(parents, list) and len(set(parents)) == len(parents) and all(p in contracts for p in parents),
            "parent contracts must already be registered")
    require(all(contracts[p]["root"] == root for p in parents), "propagated contract root differs from its parents")
    evidence = item.get("evidence")
    require(isinstance(evidence, dict), "contract needs one frozen evidence anchor")
    repo = repository(manifest, evidence.get("repo"))
    source, path = evidence.get("source"), evidence.get("path")
    require(source in repo["sources"] and evidence.get("ref") == repo["sources"][source]["ref"], "contract source identity differs")
    raw = fixed_bytes(context, repo, source, path, api)
    lines = raw.decode("utf-8", errors="strict").splitlines()
    start, end = evidence.get("line_start"), evidence.get("line_end")
    require(type(start) is int and type(end) is int and 1 <= start <= end <= len(lines), "invalid contract line interval")
    text = "\n".join(lines[start-1:end])
    require(item["symbol"] in text, "contract symbol must occur in its frozen anchor")
    if not parents:
        from workspace_report import allowed_sources, location
        key = location(root)
        require(evidence["repo"] == root["repo"] and path == root["path"] and
                source in allowed_sources(repo, key), "initial contract anchor must cover its changed root")
        comparison = repo["sources"][root["source"]]
        baseline = repo["sources"]["head"]["ref"] if root["source"] == "merge-preview" else repo["diff_base"]
        patch = str(api.run_git(Path(repo["path"]), ["diff", *api.DIFF_FLAGS, "--no-renames", "--unified=0",
                            baseline, comparison["ref"], "--", path],
                            env=source_environment(context, repo, comparison, api)))
        hunks = re.findall(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", patch, re.MULTILINE)
        if hunks:
            side = 2 if source == root["source"] else 0
            intervals = [(int(h[side]), int(h[side + 1] or "1")) for h in hunks]
            require(any(count > 0 and first <= end and start <= first + count - 1 for first, count in intervals),
                    "initial contract anchor must overlap the actual changed lines")
    terms = item.get("search_terms")
    require(isinstance(terms, list) and terms and all(isinstance(t, dict) and
            isinstance(t.get("query"), str) and t["query"] and isinstance(t.get("reason"), str) and
            t["reason"].strip() for t in terms), "contract search terms need query and relevance reason")
    require(len({t["query"] for t in terms}) == len(terms) and item["symbol"] in {t["query"] for t in terms},
            "search terms need the exact symbol and unique queries")
    for term in terms:
        if term["query"] in text:
            continue
        alias = term.get("evidence")
        require(isinstance(alias, dict), "alternate query needs a frozen alias/wrapper evidence anchor")
        alias_repo = repository(manifest, alias.get("repo"))
        alias_source = alias.get("source")
        require(alias_source in alias_repo["sources"] and alias.get("ref") == alias_repo["sources"][alias_source]["ref"],
                "alias source identity differs")
        alias_lines = fixed_bytes(context, alias_repo, alias_source, alias.get("path"), api).decode("utf-8").splitlines()
        first, last = alias.get("line_start"), alias.get("line_end")
        require(type(first) is int and type(last) is int and 1 <= first <= last <= len(alias_lines) and
                term["query"] in "\n".join(alias_lines[first-1:last]), "alternate query must occur in frozen alias evidence")
    value = {**item, "parent_ids": parents, "anchor_sha256": hashlib.sha256(raw).hexdigest()}
    return value


def register_contract(context, manifest, item, api):
    value = validate_contract(context, manifest, item, api)
    manifest["changed_contracts"].append(value)
    save(context, manifest)
    return value


def search(context: Path, manifest: dict, query: str, repo_ids: list[str] | None, api, source_name=None,
           contract_id=None) -> dict:
    require(bool(query), "search query must be nonempty")
    if manifest.get("schema") == CONTEXT and manifest.get("scope") == "group":
        require(contract_id in contract_rows(manifest), "Group searches require a registered --contract")
    if contract_id is not None:
        contract = contract_rows(manifest).get(contract_id)
        require(contract and query in {t["query"] for t in contract["search_terms"]},
                "search query is unrelated to the registered contract")
    selected = repo_ids or [r["id"] for r in manifest["repositories"]]
    require(len(selected) == len(set(selected)), "duplicate search Repo IDs")
    results = []
    for repo_id in selected:
        repo = repository(manifest, repo_id)
        name = source_name or repo.get("review_source")
        record = {"id": f"S-{len(manifest['searches']) + 1:04}", "repo": repo_id, "source": name,
                  "query": query, "contract_id": contract_id, "matches": [], "status": "complete"}
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
    parser.add_argument("command", choices=("read", "search", "inventory", "register-contract"))
    parser.add_argument("--context-dir", type=Path, required=True)
    parser.add_argument("--repo", action="append")
    parser.add_argument("--source")
    parser.add_argument("--path")
    parser.add_argument("--query")
    parser.add_argument("--contract", help="Previously registered changed/propagated contract ID")
    parser.add_argument("--contract-file", type=Path, help="One fixed-anchor contract JSON object")
    parser.add_argument("--git-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    import git_review_context as api
    context = args.context_dir.resolve()
    token = api.GIT_TIMEOUT_SECONDS.set(api.positive_timeout(str(args.git_timeout)))
    try:
        manifest = load(context)
        if args.command == "search":
            value = search(context, manifest, args.query, args.repo, api, args.source, args.contract)
        elif args.command == "register-contract":
            require(args.contract_file, "register-contract needs --contract-file")
            value = register_contract(context, manifest, json.loads(args.contract_file.read_text(encoding="utf-8")), api)
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
