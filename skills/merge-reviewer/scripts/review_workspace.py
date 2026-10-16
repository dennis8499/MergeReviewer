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
import sys
import uuid

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

CONTEXT = "merge-reviewer-workspace-context/v3"
RESULT = "merge-reviewer-workspace-result/v3"
SNAPSHOT_POLICY = "frozen-start/v1"


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
    if manifest.get("snapshot_policy") is not None:
        require(manifest["snapshot_policy"] == SNAPSHOT_POLICY, "unsupported snapshot policy")
        require(all(repo.get("snapshot") is not None for repo in manifest["repositories"] if repo["state"] != "error"),
                "frozen context needs an owned snapshot for every captured Repo")
    return manifest


def repository(manifest: dict, repo_id: str) -> dict:
    matches = [r for r in manifest["repositories"] if r["id"] == repo_id]
    require(len(matches) == 1, f"unknown/ambiguous Repo ID: {repo_id}")
    if manifest.get("snapshot_policy") is not None and matches[0]["state"] != "error":
        require(matches[0].get("snapshot") is not None, "frozen Repo snapshot metadata is missing")
    return matches[0]


def evidence_repository(context: Path, repo: dict) -> Path:
    """New sessions never read evidence through the mutable source repository."""
    snapshot = repo.get("snapshot")
    if snapshot is None:
        require(not any(isinstance(source.get("objects"), str) and "/snapshot.git/" in source["objects"]
                        for source in repo.get("sources", {}).values()), "frozen Repo snapshot metadata is missing")
        return Path(repo["path"])  # Legacy v3 contexts retain their original behavior.
    require(isinstance(snapshot, dict) and snapshot.get("policy") == SNAPSHOT_POLICY,
            "invalid frozen snapshot metadata")
    expected = f"repos/{repo['id']}/snapshot.git"
    require(snapshot.get("git_dir") == expected, "snapshot repository identity differs")
    path = safe_path(context, expected)
    objects = safe_path(context, expected + "/objects")
    require(path.is_dir() and safe_path(context, expected + "/HEAD").is_file() and objects.is_dir(),
            "frozen snapshot repository is missing")
    safe_path(context, expected + "/config")
    pack = safe_path(context, expected + "/objects/pack")
    require(pack.is_dir() and all(not is_link(item) for item in pack.iterdir()), "linked frozen object storage")
    require(not (path / "objects/info/alternates").exists(), "frozen snapshot cannot borrow alternate objects")
    return path


def source_environment(context: Path, repo: dict, source: dict, api) -> dict:
    if repo.get("snapshot") is not None:
        git_dir = evidence_repository(context, repo)
        require(source.get("objects") == f"repos/{repo['id']}/snapshot.git/objects",
                "source is outside its frozen snapshot object store")
        return {"GIT_DIR": str(git_dir), "GIT_COMMON_DIR": str(git_dir),
                "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": "", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1"}
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
    git_repo = evidence_repository(context, repo)
    data = api.run_git(git_repo, ["cat-file", "blob", f"{source['ref']}:{path}"], binary=True, env=env)
    digest = str(api.run_git(git_repo, ["hash-object", "--stdin"], input_bytes=data, env=env)).strip()
    require(digest == matches[0]["blob_sha"], "fixed blob identity mismatch")
    return data


class FixedSourceCache:
    """Verified fixed content shared only within one report validation.

    Metadata and safe paths are checked on every lookup. Changing a source,
    entry, repository, or object location therefore cannot reuse older bytes.
    """

    def __init__(self, context: Path, api):
        self.context = Path(context).resolve()
        self.api = api
        self._content = {}
        self._patches = {}

    def _source_key(self, repo, name, path):
        safe_path(self.context, path)
        source = repo.get("sources", {}).get(name)
        require(source is not None, f"unknown fixed source: {repo['id']}/{name}")
        storage = source.get("objects")
        if storage:
            objects = safe_path(self.context, storage)
            require(objects.is_dir(), "fixed object storage is missing")
            storage = str(objects.resolve())
        inherited_objects = tuple(os.environ.get(key) for key in (
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_DIR", "GIT_COMMON_DIR"))
        return (str(self.context), repo["id"], str(Path(repo["path"]).resolve()),
                name, source["ref"], source["tree_sha"], storage, repo.get("snapshot"), inherited_objects, path), source

    def read(self, repo: dict, source_name: str, path: str) -> bytes:
        key, source = self._source_key(repo, source_name, path)
        matches = [f for f in source["files"] if f["path"] == path]
        require(len(matches) == 1 and matches[0]["kind"] == "blob", "path is not a fixed blob")
        entry = matches[0]
        # Manifest values may be malformed JSON arrays/objects. Keep them in
        # the identity without bypassing the original content/tree validation.
        key = json.dumps(("read", key, entry["blob_sha"], entry.get("mode"), entry["kind"]),
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in self._content:
            self._content[key] = fixed_bytes(self.context, repo, source_name, path, self.api)
        return self._content[key]

    def changed_patch(self, repo: dict, comparison_source_name: str, baseline: str, path: str) -> str:
        key, source = self._source_key(repo, comparison_source_name, path)
        entries = tuple((entry["blob_sha"], entry.get("mode"), entry["kind"])
                        for entry in source["files"] if entry["path"] == path)
        key = json.dumps(("changed-patch", key, entries, baseline, tuple(self.api.DIFF_FLAGS)),
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in self._patches:
            self._patches[key] = str(self.api.run_git(
                evidence_repository(self.context, repo), ["diff", *self.api.DIFF_FLAGS, "--no-renames", "--unified=0",
                                    baseline, source["ref"], "--", path],
                env=source_environment(self.context, repo, source, self.api)))
        return self._patches[key]


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


def seal_snapshot(context: Path, repo: dict, api):
    """Pack complete captured trees, without copying unrelated commit history."""
    source_repo = Path(repo["path"])
    relative = f"repos/{repo['id']}/snapshot.git"
    destination = safe_path(context, relative)
    object_format = str(api.run_git(source_repo, ["rev-parse", "--show-object-format"])).strip()
    require(object_format in ("sha1", "sha256"), "unsupported Git object format")
    api.run_git(context, ["init", "--bare", f"--object-format={object_format}", str(destination)])
    object_ids = set()
    pack_environment = {}
    for source in repo["sources"].values():
        environment = source_environment(context, repo, source, api)
        # Preview objects borrow the original store, and can read every source.
        if source.get("objects"):
            pack_environment = environment
        object_ids.update((source["ref"], source["tree_sha"]))
        listing = api.run_git(source_repo, ["ls-tree", "-r", "-t", "-z", source["tree_sha"]],
                              binary=True, env=environment)
        for entry in listing.split(b"\0"):
            if entry:
                metadata = entry.split(b"\t", 1)[0].decode("ascii").split()
                if metadata[1] in ("tree", "blob"):
                    object_ids.add(metadata[2])
    # A normal pack includes its delta bases; no thin pack, alternates or hardlinks.
    api.run_git(source_repo, ["pack-objects", str(destination / "objects/pack/snapshot")],
                input_bytes=("\n".join(sorted(object_ids)) + "\n").encode("ascii"), env=pack_environment)
    repo["snapshot"] = {"policy": SNAPSHOT_POLICY, "git_dir": relative,
                        "captured_at": datetime.now(timezone.utc).isoformat(), "object_format": object_format}
    for source in repo["sources"].values():
        source["objects"] = relative + "/objects"
        environment = source_environment(context, repo, source, api)
        actual = str(api.run_git(destination, ["rev-parse", f"{source['ref']}^{{tree}}"], env=environment)).strip()
        require(actual == source["tree_sha"], "frozen snapshot tree identity mismatch")
        listing = api.run_git(destination, ["ls-tree", "-r", "-z", source["tree_sha"]],
                              binary=True, env=environment)
        require(len([entry for entry in listing.split(b"\0") if entry]) == len(source["files"]),
                "frozen snapshot inventory differs")
    checked = str(api.run_git(destination, ["cat-file", "--batch-check"],
                              input_bytes=("\n".join(sorted(object_ids)) + "\n").encode("ascii"),
                              env=source_environment(context, repo, next(iter(repo["sources"].values())), api)))
    require(len(checked.splitlines()) == len(object_ids) and all(len(line.split()) == 3 for line in checked.splitlines()),
            "frozen snapshot object closure is incomplete")
    primary = repo["sources"][repo["review_source"]]
    repo["instructions"] = []
    for entry in primary["files"]:
        if entry["kind"] == "blob" and PurePosixPath(entry["path"]).name == "AGENTS.md":
            content = fixed_bytes(context, repo, repo["review_source"], entry["path"], api)
            repo["instructions"].append({"repo": repo["id"], "source": repo["review_source"],
                                         "ref": primary["ref"], "path": entry["path"],
                                         "sha256": hashlib.sha256(content).hexdigest()})


def capture_workspace_instructions(context, roots, repositories):
    entries = []
    scopes = set(roots)
    for repo in repositories:
        scopes.update(parent for parent in repo.parents if any(parent.is_relative_to(root) for root in roots))
    for root in sorted(scopes):
        if root in repositories:
            continue  # Repo instructions come from committed, fixed trees.
        path = root / "AGENTS.md"
        require(not is_link(path), "linked workspace instruction file")
        if path.is_file():
            content = path.read_bytes()
            stored = f"instructions/workspace-{len(entries):03}.md"
            target = safe_path(context, stored)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            entries.append({"path": str(path), "scope": str(root), "stored_path": stored,
                            "sha256": hashlib.sha256(content).hexdigest()})
    return entries


def instructions(context, manifest, repo_id, path, api):
    repo = repository(manifest, repo_id)
    if path is not None:
        safe_path(context, path)
    entries = []
    workspace_entries = manifest.get("workspace_instructions", [])
    require(isinstance(workspace_entries, list), "workspace instructions must be an array")
    for item in workspace_entries:
        require(isinstance(item, dict) and all(isinstance(item.get(field), str) and item[field]
                for field in ("path", "scope", "stored_path", "sha256")), "invalid workspace instruction metadata")
        if Path(repo["path"]).is_relative_to(Path(item["scope"])):
            raw = safe_path(context, item["stored_path"]).read_bytes()
            require(hashlib.sha256(raw).hexdigest() == item["sha256"], "workspace instruction identity mismatch")
            entries.append({**item, "content": raw.decode("utf-8", errors="replace")})
    repo_entries = repo.get("instructions", [])
    require(isinstance(repo_entries, list), "Repo instructions must be an array")
    if repo.get("snapshot") is not None:
        primary = repo["sources"][repo["review_source"]]
        expected = {entry["path"] for entry in primary["files"]
                    if entry["kind"] == "blob" and PurePosixPath(entry["path"]).name == "AGENTS.md"}
        require(all(isinstance(item, dict) and item.get("repo") == repo_id
                    and item.get("source") == repo["review_source"] and item.get("ref") == primary["ref"]
                    and isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str)
                    for item in repo_entries), "invalid frozen Repo instruction metadata")
        require(len(repo_entries) == len(expected) and {item["path"] for item in repo_entries} == expected,
                "frozen Repo instruction inventory differs")
    for item in repo_entries:
        parent = PurePosixPath(item["path"]).parent
        if path is None or parent in PurePosixPath(path).parents:
            raw = fixed_bytes(context, repo, item["source"], item["path"], api)
            require(hashlib.sha256(raw).hexdigest() == item["sha256"], "Repo instruction identity mismatch")
            entries.append({**item, "content": raw.decode("utf-8", errors="replace")})
    return {"instructions": entries}


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
    git_root = str(api.run_git(resolved, ["rev-parse", "--show-toplevel"])).strip()
    require(git_root and Path(git_root).resolve() == resolved,
            "Repo path must be its Git root")
    return resolved


def build(args, api) -> dict:
    declared_root = Path(args.workspace).expanduser().absolute()
    require(not is_link(declared_root), "workspace root cannot be a link or junction")
    root = api.normalize_path(declared_root)
    workspace_file = api.normalize_path(args.workspace_file) if args.workspace_file else None
    roots = api.workspace_roots(root, workspace_file)
    require(all(not is_link(r) for r in roots), "workspace roots cannot be links or junctions")
    require(all(r.is_dir() for r in roots), "宣告的工作區資料夾不存在，無法確認 Repo 範圍。")
    paths = api.discover_repositories(root, workspace_file)
    require(bool(paths), "找不到 Git Repo；請指定正確工作區。")
    require(all(any(p.is_relative_to(r) for r in roots) for p in paths),
            "Repo canonical path escapes the declared workspace roots")
    seed, _ = api.choose_repository(root, workspace_file, args.project)
    enabled = len(paths) > 1
    context = Path(args.context_dir)
    manifest = {
        "schema": CONTEXT, "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8],
        "workspace": str(root), "workspace_roots": [str(r) for r in roots],
        "workspace_file": str(workspace_file) if workspace_file else None,
        "workspace_file_sha256": hashlib.sha256(workspace_file.read_bytes()).hexdigest() if workspace_file else None,
        "review_basis": "codex-native", "scope": "group" if enabled else "repo", "target": "branch",
        "extension_enabled": enabled,
        "extension_reason": f"宣告工作區包含 {len(paths)} 個不同的 Git 根目錄。" if enabled else "宣告工作區只有一個 Git 根目錄。",
        "selected_repo": None, "repositories": [], "seed_repos": [], "searches": [],
        "changed_contracts": [], "context_gaps": [], "context_dir": str(context),
        "context_cleanup_required": True, "snapshot_policy": SNAPSHOT_POLICY,
        "workspace_instructions": capture_workspace_instructions(context, roots, paths),
    }
    for index, candidate in enumerate(paths):
        record = {"id": f"repo-{index:03}", "name": candidate.name, "path": str(candidate),
                  "role": "seed" if candidate == seed else "candidate", "sources": {},
                  "changed_files": [], "patches": [], "limitations": [], "state": "captured"}
        manifest["repositories"].append(record)
        if candidate == seed:
            manifest["selected_repo"] = record["id"]
            manifest["seed_repos"] = [record["id"]]
        try:
            actual = validate_root(candidate, api)
            record["before"] = api.snapshot(actual)
            record["head_sha"] = api.verified_commit(actual, "HEAD")
            safe_path(context, f"repos/{record['id']}").mkdir(parents=True)
            if candidate == seed:
                branch_snapshot(context, record, args, api)
            else:
                require(record["head_sha"], "related Repo has no HEAD commit")
                record["review_source"] = "head"
                add_source(context, record, "head", record["head_sha"], api)
            require(record["before"] == api.snapshot(actual), "Repo changed during capture; rebuild context")
            seal_snapshot(context, record, api)
        except (ValueError, OSError, api.ReviewContextError) as exc:
            if candidate == seed:
                raise
            record["state"] = "error"
            record["review_source"] = "head"
            # A failed candidate has no sealed evidence; never expose borrowed
            # partial sources as if they were an old, live-backed v3 context.
            record["sources"] = {}
            record["instructions"] = []
            record.pop("snapshot", None)
            record["limitations"].append(str(exc))
        if candidate == seed:
            manifest["context_gaps"].extend(f"{record['id']}: {gap}" for gap in record["limitations"])
    manifest["required_coverage"] = required_coverage(manifest)
    manifest["context_complete"] = not manifest["context_gaps"]
    manifest["capture_completed_at"] = datetime.now(timezone.utc).isoformat()
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


def contract_rows(manifest):
    values = manifest.get("changed_contracts", [])
    require(isinstance(values, list), "changed contracts must be an array")
    return {c["id"]: c for c in values}


def validate_contract(context, manifest, item, api, *, cache=None):
    """Anchor reviewer-selected contracts before searching their names or aliases."""
    require(manifest.get("schema") == CONTEXT, "contract registration needs context v3")
    require(manifest.get("extension_enabled") is True, "contract registration applies only to multi-Repo extension")
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
    raw = cache.read(repo, source, path) if cache is not None else fixed_bytes(context, repo, source, path, api)
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
        patch = (cache.changed_patch(repo, root["source"], baseline, path) if cache is not None else
                 str(api.run_git(evidence_repository(context, repo), ["diff", *api.DIFF_FLAGS, "--no-renames", "--unified=0",
                             baseline, comparison["ref"], "--", path],
                             env=source_environment(context, repo, comparison, api))))
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
        alias_raw = (cache.read(alias_repo, alias_source, alias.get("path")) if cache is not None else
                     fixed_bytes(context, alias_repo, alias_source, alias.get("path"), api))
        alias_lines = alias_raw.decode("utf-8").splitlines()
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
    if manifest.get("extension_enabled") is True:
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
            code, output, error = api.run_git_capture(evidence_repository(context, repo),
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


def read(context: Path, manifest: dict, repo_id: str, path: str, api, source_name=None) -> dict:
    require(isinstance(repo_id, str) and bool(repo_id) and path, "read needs one --repo and --path")
    repo = repository(manifest, repo_id)
    name = source_name or repo["review_source"]
    content = fixed_bytes(context, repo, name, path, api)
    require(b"\0" not in content, "binary blob; record a coverage limitation")
    return {"repo": repo["id"], "source": name, "ref": repo["sources"][name]["ref"], "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "lines": [{"line": i, "text": s} for i, s in enumerate(
                content.decode("utf-8", errors="replace").splitlines(), 1)]}


def batch_read(context: Path, manifest: dict, requests: list[dict], api) -> dict:
    require(isinstance(requests, list), "batch requests must be an array")
    results = []
    for request in requests:
        require(isinstance(request, dict), "batch request must be an object")
        require(set(request) <= {"repo", "source", "path"}, "unknown batch-read request argument")
        require(request.get("source") is None or isinstance(request["source"], str), "source must be text")
        results.append(read(context, manifest, request.get("repo"), request.get("path"), api, request.get("source")))
    return {"reads": results}


def batch_search(context: Path, manifest: dict, requests: list[dict], api) -> dict:
    require(isinstance(requests, list), "batch requests must be an array")
    results = []
    for request in requests:
        require(isinstance(request, dict), "batch request must be an object")
        require(set(request) <= {"repo", "source", "query", "contract"}, "unknown batch-search request argument")
        repos = request.get("repo")
        require(repos is None or isinstance(repos, list) and all(isinstance(r, str) for r in repos),
                "search repo must be an array of Repo IDs")
        require(request.get("query") is None or isinstance(request["query"], str), "search query must be text")
        require(request.get("source") is None or isinstance(request["source"], str), "source must be text")
        require(request.get("contract") is None or isinstance(request["contract"], str), "contract must be text")
        value = search(context, manifest, request.get("query"), repos, api,
                       request.get("source"), request.get("contract"))
        results.extend(value["searches"])
    return {"searches": results}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("read", "search", "batch-read", "batch-search", "inventory", "register-contract", "instructions"))
    parser.add_argument("--context-dir", type=Path, required=True)
    parser.add_argument("--repo", action="append")
    parser.add_argument("--source")
    parser.add_argument("--path")
    parser.add_argument("--query")
    parser.add_argument("--contract", help="Previously registered changed/propagated contract ID")
    parser.add_argument("--contract-file", type=Path, help="One fixed-anchor contract JSON object")
    parser.add_argument("--requests-file", type=Path, help="Ordered JSON array for batch-read or batch-search")
    parser.add_argument("--git-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    import git_review_context as api
    context = args.context_dir.resolve()
    token = api.GIT_TIMEOUT_SECONDS.set(api.positive_timeout(str(args.git_timeout)))
    try:
        manifest = load(context)
        if args.command in ("batch-read", "batch-search"):
            require(args.requests_file, f"{args.command} needs --requests-file")
            requests = json.loads(args.requests_file.read_text(encoding="utf-8"))
            operation = batch_read if args.command == "batch-read" else batch_search
            value = operation(context, manifest, requests, api)
        elif args.command == "search":
            value = search(context, manifest, args.query, args.repo, api, args.source, args.contract)
        elif args.command == "register-contract":
            require(args.contract_file, "register-contract needs --contract-file")
            value = register_contract(context, manifest, json.loads(args.contract_file.read_text(encoding="utf-8")), api)
        elif args.command == "read":
            require(args.repo and len(args.repo) == 1 and args.path, "read needs one --repo and --path")
            value = read(context, manifest, args.repo[0], args.path, api, args.source)
        elif args.command == "instructions":
            require(args.repo and len(args.repo) == 1, "instructions needs one --repo")
            value = instructions(context, manifest, args.repo[0], args.path, api)
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
