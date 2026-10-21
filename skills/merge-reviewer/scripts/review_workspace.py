#!/usr/bin/env python3
"""Portable fixed-source Repo/Group contexts and auditable impact searches.

Preparation freezes versions, not semantic conclusions. Codex follows contracts
with search/read and records the resulting impact graph in its review draft.
"""
from __future__ import annotations

import argparse
import copy
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import uuid
from contextlib import nullcontext

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import review_runtime
import merge_parent_coverage

CONTEXT = "merge-reviewer-workspace-context/v3"
RESULT = "merge-reviewer-workspace-result/v3"
SNAPSHOT_POLICY = "frozen-start/v1"
COVERAGE_POLICY = "ancestor-noop/v1"


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
    review_runtime.save_manifest(context, manifest)


def load(context: Path) -> dict:
    from review_session import validate_session
    context = validate_session(context)
    manifest = json.loads(safe_path(context, "manifest.json").read_text(encoding="utf-8"))
    require(manifest.get("schema") == CONTEXT, "not a portable workspace context")
    require("coverage_policy" not in manifest or manifest["coverage_policy"] == COVERAGE_POLICY,
            "unsupported coverage policy")
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
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": "", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
                "GIT_ALLOW_PROTOCOL": ""}
    storage = source.get("objects")
    if not storage:
        return {"GIT_ALLOW_PROTOCOL": ""}
    objects = safe_path(context, storage)
    require(objects.is_dir(), "fixed object storage is missing")
    return {"GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(api.git_path(Path(repo["path"]), "objects")),
            "GIT_ALLOW_PROTOCOL": ""}


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
    """Verified fixed content shared only within one read/validation operation.

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
        if repo.get("snapshot") is not None:
            source_environment(self.context, repo, source, self.api)
        # Manifest values may be malformed JSON arrays/objects. Keep them in
        # the identity without bypassing the original content/tree validation.
        key = json.dumps(("read", key, entry["blob_sha"], entry.get("mode"), entry["kind"]),
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in self._content:
            self._content[key] = fixed_bytes(self.context, repo, source_name, path, self.api)
        return self._content[key]

    def prefetch(self, manifest, requests):
        groups = {}
        for request in requests:
            repo = repository(manifest, request.get("repo"))
            name = request.get("source") or repo["review_source"]
            path = request.get("path")
            if repo.get("snapshot") is None or not isinstance(path, str) or "\n" in path or "\r" in path:
                continue
            key, source = self._source_key(repo, name, path)
            matches = [entry for entry in source["files"] if entry["path"] == path]
            require(len(matches) == 1 and matches[0]["kind"] == "blob", "path is not a fixed blob")
            entry = matches[0]
            key = json.dumps(("read", key, entry["blob_sha"], entry.get("mode"), entry["kind"]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key in self._content:
                continue
            environment = source_environment(self.context, repo, source, self.api)
            group = groups.setdefault(repo["id"], {"repo": repo, "env": environment, "items": [], "keys": []})
            expression = source["ref"] + ":" + path
            group["items"].append((expression, entry["blob_sha"]))
            group["keys"].append((key, expression))
        for group in groups.values():
            repo = group["repo"]
            bodies = review_runtime.batch_blobs(lambda data: self.api.run_git(evidence_repository(self.context, repo),
                ["cat-file", "--batch"], binary=True, input_bytes=data, env=group["env"]),
                group["items"], repo["snapshot"]["object_format"])
            for key, expression in group["keys"]:
                self._content[key] = bodies[expression]

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


SEARCH_FLAGS = ("grep", "-n", "-I", "--no-textconv", "-F", "-e")


class FixedSearchCache:
    """Replay each successful frozen search once within one operation.

    Callers still validate and record every logical search. Legacy live-backed
    contexts and unsuccessful commands are always executed again.
    """

    def __init__(self, context: Path, api):
        self.context = Path(context).resolve()
        self.api = api
        self._results = {}

    def run(self, repo: dict, source_name: str, query: str) -> tuple[int, str, str]:
        source = repo["sources"][source_name]
        environment = source_environment(self.context, repo, source, self.api)
        git_repo = evidence_repository(self.context, repo)
        inherited = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
        key = json.dumps((str(self.context), repo["id"], str(Path(repo["path"]).resolve()),
                          str(git_repo), source_name, source["ref"], source["tree_sha"],
                          source.get("objects"), repo.get("snapshot"), inherited, environment,
                          SEARCH_FLAGS, query), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot = repo.get("snapshot")
        frozen = isinstance(snapshot, dict) and snapshot.get("policy") == SNAPSHOT_POLICY
        if not frozen or key not in self._results:
            result = self.api.run_git_capture(
                git_repo, [*SEARCH_FLAGS, query, source["ref"], "--"], env=environment)
            if not frozen or result[0] not in (0, 1):
                return result
            self._results[key] = result
        return self._results[key]


class CaptureTreeCache:
    """Tree inventories and object closure isolated to one capture operation."""

    def __init__(self, context: Path, api):
        self.context = context
        self.api = api
        self._trees = {}

    def inventory(self, repo: dict, tree: str, environment: dict) -> tuple[list[dict], frozenset[str]]:
        inherited = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
        inherited.update(environment)
        key = (repo["id"], str(Path(repo["path"]).resolve()), tree, tuple(sorted(inherited.items())))
        if key not in self._trees:
            raw = self.api.run_git(Path(repo["path"]), ["ls-tree", "-r", "-t", "-z", tree],
                                   binary=True, env=environment)
            files, objects = [], {tree}
            for item in raw.split(b"\0"):
                if not item:
                    continue
                meta, file_path = item.split(b"\t", 1)
                mode, kind, object_id = meta.decode("ascii").split()
                if kind in ("tree", "blob"):
                    objects.add(object_id)
                if kind != "tree":
                    relative = file_path.decode("utf-8", errors="strict")
                    safe_path(self.context, relative)
                    files.append({"path": relative, "mode": mode, "kind": kind, "blob_sha": object_id})
            self._trees[key] = (files, frozenset(objects))
        files, objects = self._trees[key]
        # Source metadata stays independently mutable, as it was without caching.
        return [entry.copy() for entry in files], objects


def add_source(context: Path, repo: dict, name: str, reference: str, api, *, env=None, tree_cache=None):
    path = Path(repo["path"])
    env = env or {}
    tree = str(api.run_git(path, ["rev-parse", f"{reference}^{{tree}}"], env=env)).strip()
    cache = tree_cache if tree_cache is not None else CaptureTreeCache(context, api)
    files, _ = cache.inventory(repo, tree, env)
    storage = env.get("GIT_OBJECT_DIRECTORY")
    repo["sources"][name] = {"ref": reference, "tree_sha": tree, "files": files,
                             "objects": Path(storage).relative_to(context).as_posix() if storage else None}


def validate_commit_path(raw: bytes, path: list[str], object_format: str):
    """Verify raw commit identities and the direct parent edges of one path."""
    require(object_format in ("sha1", "sha256"), "ancestor proof object format is invalid")
    length = 40 if object_format == "sha1" else 64
    require(isinstance(raw, bytes) and isinstance(path, list) and path
            and all(isinstance(reference, str) and re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", reference)
                    for reference in path) and len(path) == len(set(path)),
            "ancestor proof batch identity is invalid")
    offset = 0
    for index, reference in enumerate(path):
        boundary = raw.find(b"\n", offset)
        require(boundary >= 0, "ancestor proof commit header is incomplete")
        header = raw[offset:boundary].split()
        require(len(header) == 3 and header[0] == reference.encode("ascii")
                and header[1] == b"commit" and header[2].isdigit(), "ancestor proof commit identity differs")
        size = int(header[2])
        offset = boundary + 1
        body = raw[offset:offset + size]
        offset += size
        require(len(body) == size and raw[offset:offset + 1] == b"\n", "ancestor proof commit body is incomplete")
        offset += 1
        digest = hashlib.new(object_format, b"commit " + str(size).encode("ascii") + b"\0" + body).hexdigest()
        require(digest == reference, "ancestor proof commit checksum differs")
        if index + 1 < len(path):
            parents = [line[7:] for line in body.partition(b"\n\n")[0].split(b"\n")
                       if line.startswith(b"parent ")]
            require(path[index + 1].encode("ascii") in parents, "ancestor proof parent link differs")
    require(offset == len(raw), "ancestor proof has unexpected trailing bytes")


def capture_ancestor_path(repo: Path, base: str, head: str, api) -> list[str]:
    """Find one fixed parent path without retaining its historical trees."""
    if base == head:
        path = [base]
    else:
        graph = {}
        raw = str(api.run_git(repo, ["rev-list", "--ancestry-path", "--parents", f"{head}..{base}"]))
        for line in raw.splitlines():
            row = line.split()
            require(row and all(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", item) for item in row)
                    and row[0] not in graph, "ancestor proof commit graph is invalid")
            graph[row[0]] = row[1:]
        queue, child = deque([base]), {base: None}
        while queue and head not in child:
            current = queue.popleft()
            for parent in graph.get(current, []):
                if parent not in child and (parent == head or parent in graph):
                    child[parent] = current
                    queue.append(parent)
        require(head in child, "ancestor proof cannot connect the fixed base and head")
        path, current = [], head
        while current is not None:
            path.append(current)
            current = child[current]
        path.reverse()
    bodies = api.run_git(repo, ["cat-file", "--batch"], binary=True,
                         input_bytes=("\n".join(path) + "\n").encode("ascii"))
    validate_commit_path(bodies, path, "sha1" if len(base) == 40 else "sha256")
    return path


def seal_snapshot(context: Path, repo: dict, api, *, tree_cache=None, extra_commit_objects=()):
    """Pack complete captured trees, without copying unrelated commit history."""
    source_repo = Path(repo["path"])
    relative = f"repos/{repo['id']}/snapshot.git"
    destination = safe_path(context, relative)
    object_format = str(api.run_git(source_repo, ["rev-parse", "--show-object-format"])).strip()
    require(object_format in ("sha1", "sha256"), "unsupported Git object format")
    api.run_git(context, ["init", "--bare", f"--object-format={object_format}", str(destination)])
    object_ids = set()
    pack_environment = {}
    cache = tree_cache if tree_cache is not None else CaptureTreeCache(context, api)
    for source in repo["sources"].values():
        environment = source_environment(context, repo, source, api)
        # Preview objects borrow the original store, and can read every source.
        if source.get("objects"):
            pack_environment = environment
        object_ids.update((source["ref"], source["tree_sha"]))
        _, objects = cache.inventory(repo, source["tree_sha"], environment)
        object_ids.update(objects)
    extra = list(extra_commit_objects)
    if extra:
        bodies = api.run_git(source_repo, ["cat-file", "--batch"], binary=True,
                             input_bytes=("\n".join(extra) + "\n").encode("ascii"), env=pack_environment)
        validate_commit_path(bodies, extra, object_format)
        object_ids.update(extra)
    # A normal pack includes its delta bases; no thin pack, alternates or hardlinks.
    api.run_git(source_repo, ["pack-objects", str(destination / "objects/pack/snapshot")],
                input_bytes=("\n".join(sorted(object_ids)) + "\n").encode("ascii"), env=pack_environment)
    repo["snapshot"] = {"policy": SNAPSHOT_POLICY, "git_dir": relative,
                        "captured_at": datetime.now(timezone.utc).isoformat(), "object_format": object_format}
    verified_trees = {}
    for source in repo["sources"].values():
        source["objects"] = relative + "/objects"
        environment = source_environment(context, repo, source, api)
        actual = str(api.run_git(destination, ["rev-parse", f"{source['ref']}^{{tree}}"], env=environment)).strip()
        require(actual == source["tree_sha"], "frozen snapshot tree identity mismatch")
        tree = source["tree_sha"]
        if tree not in verified_trees:
            listing = api.run_git(destination, ["ls-tree", "-r", "-z", tree], binary=True, env=environment)
            verified_trees[tree] = len([entry for entry in listing.split(b"\0") if entry])
        require(verified_trees[tree] == len(source["files"]),
                "frozen snapshot inventory differs")
    checked = str(api.run_git(destination, ["cat-file", "--batch-check"],
                              input_bytes=("\n".join(sorted(object_ids)) + "\n").encode("ascii"),
                              env=source_environment(context, repo, next(iter(repo["sources"].values())), api)))
    require(len(checked.splitlines()) == len(object_ids) and all(len(line.split()) == 3 for line in checked.splitlines()),
            "frozen snapshot object closure is incomplete")
    if extra:
        bodies = api.run_git(destination, ["cat-file", "--batch"], binary=True,
                             input_bytes=("\n".join(extra) + "\n").encode("ascii"),
                             env=source_environment(context, repo, next(iter(repo["sources"].values())), api))
        validate_commit_path(bodies, extra, object_format)
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


def branch_snapshot(context: Path, repo: dict, args, api, *, tree_cache=None, frozen_refs=None):
    native_args = copy.copy(args)
    native_args.workspace, native_args.workspace_file = repo["path"], None
    native_args.project = repo["path"]
    native_args.context_dir = str(safe_path(context, f"repos/{repo['id']}/native"))
    native = api._build_manifest(native_args, frozen_refs=frozen_refs, before_snapshot=repo.get("before"))
    repo["native"] = native
    repo["review_source"] = "head"
    for name, reference in (("head", native["head_sha"]), ("base", native["base_sha"])):
        add_source(context, repo, name, reference, api, tree_cache=tree_cache)
    if native["merge_base"]:
        add_source(context, repo, "merge-base", native["merge_base"], api, tree_cache=tree_cache)
    for merge in native["merge_commits"]:
        for parent in merge["parents"]:
            name = "parent-" + parent
            if name not in repo["sources"]:
                add_source(context, repo, name, parent, api, tree_cache=tree_cache)
    repo["changed_files"] = [{**c, "source": "head"} for c in native["changed_files"]]
    repo["patches"] = [f"repos/{repo['id']}/native/diff.patch"]
    preview = native.get("merge_preview")
    if preview and preview.get("tree_sha"):
        info = preview["manifest_info"]
        env = {"GIT_OBJECT_DIRECTORY": info["object_directory"],
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": info["alternate_objects"]}
        add_source(context, repo, "merge-preview", preview["tree_sha"], api, env=env, tree_cache=tree_cache)
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
    api.capture_phase("local-preflight")
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
        "workspace_instructions": [], "coverage_policy": "ancestor-noop/v1",
        "network_activity": api.network_activity(),
    }
    branch_refs = None
    # Check every local root before the selected Repo is allowed to fetch.
    # Peer failures stay visible without changing ownership configuration.
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
            record["head_sha"] = api.verified_commit(actual, "HEAD")
            if candidate != seed:
                require(record["head_sha"], "related Repo has no HEAD commit")
            object_format = str(api.run_git(actual, ["rev-parse", "--show-object-format"])).strip()
            require(object_format in ("sha1", "sha256"), "unsupported Git object format")
            record["refs_pinned_at"] = datetime.now(timezone.utc).isoformat()
        except (ValueError, OSError, api.ReviewContextError) as exc:
            if candidate == seed:
                raise
            record["state"] = "error"
            record["review_source"] = "head"
            record["instructions"] = []
            record["limitations"].append(str(exc))
    api.capture_phase("output-preflight")
    require(not any(context.resolve().is_relative_to(path.resolve()) for path in paths),
            "--context-dir must be outside every repository")
    manifest["workspace_instructions"] = capture_workspace_instructions(context, roots, paths)
    for record in manifest["repositories"]:
        if record["state"] == "error":
            continue
        try:
            safe_path(context, f"repos/{record['id']}").mkdir(parents=True)
            if record["id"] == manifest["selected_repo"]:
                safe_path(context, f"repos/{record['id']}/native").mkdir()
                record["before"] = api.snapshot(Path(record["path"]))
        except (ValueError, OSError, api.ReviewContextError) as exc:
            if record["id"] == manifest["selected_repo"]:
                raise
            record["state"] = "error"
            record["review_source"] = "head"
            record["instructions"] = []
            record["limitations"].append(str(exc))
    branch_refs = api.pin_branch_refs(seed, args)
    manifest["network_activity"] = api.network_activity()
    selected = next(record for record in manifest["repositories"] if record["id"] == manifest["selected_repo"])
    selected["refs_pinned_at"] = datetime.now(timezone.utc).isoformat()
    manifest["refs_pinned_at"] = datetime.now(timezone.utc).isoformat()
    # All branch/peer object IDs are now fixed; sealing stays offline.
    api.capture_phase("snapshot-capture")
    for record in manifest["repositories"]:
        if record["state"] == "error":
            continue
        candidate = actual = Path(record["path"])
        try:
            tree_cache = CaptureTreeCache(context, api)
            if candidate != seed:
                record["before"] = api.snapshot(actual)
            if candidate == seed:
                branch_snapshot(context, record, args, api, tree_cache=tree_cache, frozen_refs=branch_refs)
                native = record["native"]
                preview = native.get("merge_preview", {})
                if (native["head_sha"] == native["merge_base"] and not native["changed_files"]
                        and preview.get("status") == "clean"
                        and preview.get("tree_sha") == record["sources"]["base"]["tree_sha"]):
                    api.capture_phase("ancestor-proof")
                    record["ancestor_path"] = capture_ancestor_path(actual, native["base_sha"], native["head_sha"], api)
                    api.capture_phase("snapshot-capture")
            else:
                record["review_source"] = "head"
                add_source(context, record, "head", record["head_sha"], api, tree_cache=tree_cache)
            if candidate != seed:
                require(record["before"] == api.snapshot(actual), "Repo changed during capture; rebuild context")
            seal_snapshot(context, record, api, tree_cache=tree_cache,
                          extra_commit_objects=record.get("ancestor_path", ()))
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
    api.capture_phase("manifest-finalize")
    manifest["required_coverage"] = required_coverage(manifest)
    selected = repository(manifest, manifest["selected_repo"])
    native = selected["native"]
    final = selected["sources"].get("merge-preview", selected["sources"]["head"])
    effective = [] if final["tree_sha"] == selected["sources"]["base"]["tree_sha"] else selected["changed_files"]
    manifest["merge_parent_policy"] = merge_parent_coverage.POLICY
    manifest["required_merge_views"] = merge_parent_coverage.required(effective, native["merge_commits"],
        {entry["path"] for entry in final["files"] if entry["kind"] == "blob"})
    manifest["context_complete"] = not manifest["context_gaps"]
    manifest["capture_completed_at"] = datetime.now(timezone.utc).isoformat()
    save(context, manifest)
    return manifest

def ancestor_noop(manifest: dict) -> bool:
    """Recognize an already-contained head without trusting an authored flag."""
    if "coverage_policy" not in manifest:
        return False  # Existing v3 contexts keep their original coverage contract.
    policy = manifest["coverage_policy"]
    require(policy == COVERAGE_POLICY, "unsupported coverage policy")
    selected = [repo for repo in manifest["repositories"] if repo["id"] == manifest["selected_repo"]]
    require(len(selected) == 1, "coverage policy needs one selected Repo")
    repo = selected[0]
    require(repo.get("state") == "captured", "coverage policy needs a captured selected Repo")
    native, sources = repo.get("native"), repo.get("sources")
    require(isinstance(native, dict) and isinstance(sources, dict), "coverage policy needs captured branch metadata")
    for name, key in (("base", "base_sha"), ("head", "head_sha"), ("merge-base", "merge_base")):
        source = sources.get(name)
        reference = native.get(key)
        require(isinstance(reference, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", reference)
                and isinstance(source, dict) and source.get("ref") == reference,
                "coverage policy branch identity differs: " + name)
    require(repo.get("diff_base") == native.get("diff_base") == native["merge_base"],
            "coverage policy comparison baseline differs")
    preview = native.get("merge_preview")
    require(isinstance(preview, dict) and preview.get("status") in ("clean", "conflicts", "unavailable"),
            "coverage policy needs a merge preview outcome")
    status, tree = preview["status"], preview.get("tree_sha")
    if status in ("clean", "conflicts"):
        require(type(preview.get("exit_code")) is int and preview["exit_code"] == (0 if status == "clean" else 1),
                "coverage policy preview outcome differs")
        source = sources.get("merge-preview")
        require(isinstance(tree, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tree)
                and isinstance(source, dict) and source.get("ref") == source.get("tree_sha") == tree,
                "coverage policy preview tree identity differs")
    else:
        require(tree is None and "merge-preview" not in sources,
                "unavailable merge preview exposes a tree")
    if native["head_sha"] != native["merge_base"]:
        return False
    require(native.get("changed_files") == [], "ancestor head has inconsistent committed changes")
    require(isinstance(repo.get("changed_files"), list)
            and all(isinstance(change, dict) and change.get("source") == "merge-preview"
                    for change in repo["changed_files"]),
            "ancestor coverage exposes inconsistent head changes")
    if status != "clean":
        return False
    require(tree == sources["base"].get("tree_sha"), "ancestor preview differs from the base tree")
    if not (native.get("context_complete") is True and native.get("review_complete") is True
            and not (repo.get("limitations") or native.get("context_gaps") or native.get("review_limitations")
                     or manifest.get("context_gaps"))):
        return False
    path = repo.get("ancestor_path")
    require(isinstance(path, list) and path
            and all(isinstance(reference, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", reference)
                    for reference in path) and len(path) == len(set(path))
            and path[0] == native["base_sha"] and path[-1] == native["head_sha"],
            "ancestor coverage needs a fixed base-to-head commit path")
    return True


def required_coverage(manifest: dict) -> list[dict]:
    if ancestor_noop(manifest):
        return []
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
    require(isinstance(evidence, dict), "contract needs one frozen evidence anchor; evidence must be an object, not an array")
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


def batch_register_contract(context, manifest, requests, api):
    require(isinstance(requests, list), "batch requests must be an array")
    values = []
    for item in requests:
        values.append(register_contract(context, manifest, item, api))
    return {"contracts": values}


def search(context: Path, manifest: dict, query: str, repo_ids: list[str] | None, api, source_name=None,
           contract_id=None, *, cache=None) -> dict:
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
            if cache is None:
                env = source_environment(context, repo, source, api)
                code, output, error = api.run_git_capture(evidence_repository(context, repo),
                    [*SEARCH_FLAGS, query, source["ref"], "--"], env=env)
            else:
                code, output, error = cache.run(repo, name, query)
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


def read_bounds(line_start, line_end):
    if line_start is None and line_end is None:
        return
    require(type(line_start) is int and type(line_end) is int,
            "line_start and line_end must be supplied together as integers")
    require(1 <= line_start <= line_end,
            "line range must satisfy 1 <= line_start <= line_end")


def read(context: Path, manifest: dict, repo_id: str, path: str, api, source_name=None, *, cache=None,
         line_start=None, line_end=None) -> dict:
    require(isinstance(repo_id, str) and bool(repo_id) and path, "read needs one --repo and --path")
    read_bounds(line_start, line_end)
    repo = repository(manifest, repo_id)
    name = source_name or repo["review_source"]
    content = cache.read(repo, name, path) if cache is not None else fixed_bytes(context, repo, name, path, api)
    require(b"\0" not in content, "binary blob; record a coverage limitation")
    lines = content.decode("utf-8", errors="replace").splitlines()
    result = {"repo": repo["id"], "source": name, "ref": repo["sources"][name]["ref"], "path": path,
              "sha256": hashlib.sha256(content).hexdigest()}
    if line_start is not None:
        require(line_end <= len(lines), f"line_end exceeds fixed file line count ({len(lines)})")
        result.update(line_start=line_start, line_end=line_end, total_lines=len(lines))
        result["lines"] = [{"line": i, "text": lines[i - 1]} for i in range(line_start, line_end + 1)]
    else:
        result["lines"] = [{"line": i, "text": text} for i, text in enumerate(lines, 1)]
    return result


def validate_read_request(request):
    require(isinstance(request, dict), "batch request must be an object")
    require(set(request) <= {"repo", "source", "path", "line_start", "line_end"},
            "unknown batch-read request argument")
    require(request.get("source") is None or isinstance(request["source"], str), "source must be text")
    require(("line_start" in request) == ("line_end" in request),
            "line_start and line_end must be supplied together as integers")
    if "line_start" in request:
        require(type(request["line_start"]) is int and type(request["line_end"]) is int,
                "line_start and line_end must be supplied together as integers")
        read_bounds(request["line_start"], request["line_end"])


def batch_read(context: Path, manifest: dict, requests: list[dict], api) -> dict:
    require(isinstance(requests, list), "batch requests must be an array")
    results = []
    cache = FixedSourceCache(context, api)
    for request in requests:
        validate_read_request(request)
    # Preserve ordered failure behavior: prefetch only the valid prefix.
    prefix = []
    for request in requests:
        try:
            repo = repository(manifest, request.get("repo"))
            source = repo["sources"][request.get("source") or repo["review_source"]]
            require(any(entry["path"] == request.get("path") and entry["kind"] == "blob" for entry in source["files"]), "path is not a fixed blob")
            prefix.append(request)
        except (ValueError, KeyError):
            break
    cache.prefetch(manifest, prefix)
    for request in requests:
        results.append(read(context, manifest, request.get("repo"), request.get("path"), api,
                            request.get("source"), cache=cache,
                            line_start=request.get("line_start"), line_end=request.get("line_end")))
    return {"reads": results}


def batch_search(context: Path, manifest: dict, requests: list[dict], api) -> dict:
    require(isinstance(requests, list), "batch requests must be an array")
    results = []
    cache = FixedSearchCache(context, api)
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
                       request.get("source"), request.get("contract"), cache=cache)
        results.extend(value["searches"])
    return {"searches": results}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("read", "search", "batch-read", "batch-search", "inventory", "view",
                                           "register-contract", "batch-register-contract", "instructions"))
    parser.add_argument("--context-dir", type=Path, required=True)
    parser.add_argument("--repo", action="append")
    parser.add_argument("--source")
    parser.add_argument("--path")
    parser.add_argument("--line-start", type=int, help="First fixed file line (1-based; requires --line-end)")
    parser.add_argument("--line-end", type=int, help="Last fixed file line (inclusive; requires --line-start)")
    parser.add_argument("--query")
    parser.add_argument("--contract", help="Previously registered changed/propagated contract ID")
    parser.add_argument("--contract-file", type=Path, help="One fixed-anchor contract JSON object")
    parser.add_argument("--requests-file", type=Path, help="Ordered JSON array for a batch command")
    parser.add_argument("--git-timeout", type=float, default=180.0)
    parser.add_argument("--shared-content", action="store_true")
    parser.add_argument("--auto-content", action="store_true")
    parser.add_argument("--page-size", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--artifact")
    parser.add_argument("--search-id", action="append", help="Select a saved search ID for view; repeatable")
    args = parser.parse_args(argv)
    import git_review_context as api
    context = args.context_dir.resolve()
    token = api.GIT_TIMEOUT_SECONDS.set(api.positive_timeout(str(args.git_timeout)))
    try:
        return _execute(args, context, api)
    except (ValueError, OSError, api.ReviewContextError) as exc:
        print(f"workspace-review: {exc}", file=sys.stderr)
        return 2
    finally:
        api.GIT_TIMEOUT_SECONDS.reset(token)


def _execute(args, context, api):
    mutation = args.command in ("search", "batch-search", "register-contract", "batch-register-contract")
    with review_runtime.writer(context, args.git_timeout) if mutation else nullcontext():
        manifest = load(context)
        if args.line_start is not None or args.line_end is not None:
            require(args.command == "read", "line arguments require read; use JSON fields for batch-read")
            read_bounds(args.line_start, args.line_end)
        require(args.search_id is None or args.command == "view", "--search-id requires view")
        if args.shared_content or args.auto_content:
            require(args.command == "batch-read" and not (args.shared_content and args.auto_content),
                    "content format requires batch-read and one format flag")
        if args.command == "view":
            require(args.artifact and args.page_size, "view needs --artifact and --page-size")
            saved = review_runtime.load_view(context, args.artifact)
            if args.search_id is not None:
                saved = review_runtime.select_searches(saved, args.search_id)
            value = review_runtime.page(saved, context,
                                        size=args.page_size, offset=args.offset, artifact=args.artifact)
        elif args.command in ("batch-read", "batch-search", "batch-register-contract"):
            require(args.requests_file, f"{args.command} needs --requests-file")
            requests = json.loads(args.requests_file.read_text(encoding="utf-8-sig"))
            operation = {"batch-read": batch_read, "batch-search": batch_search,
                         "batch-register-contract": batch_register_contract}[args.command]
            value = operation(context, manifest, requests, api)
        elif args.command == "search":
            value = search(context, manifest, args.query, args.repo, api, args.source, args.contract)
        elif args.command == "register-contract":
            require(args.contract_file, "register-contract needs --contract-file")
            value = register_contract(context, manifest, json.loads(args.contract_file.read_text(encoding="utf-8-sig")), api)
        elif args.command == "read":
            require(args.repo and len(args.repo) == 1 and args.path, "read needs one --repo and --path")
            value = read(context, manifest, args.repo[0], args.path, api, args.source,
                         line_start=args.line_start, line_end=args.line_end)
        elif args.command == "instructions":
            require(args.repo and len(args.repo) == 1, "instructions needs one --repo")
            value = instructions(context, manifest, args.repo[0], args.path, api)
        else:
            value = {"repositories": [{"repo": r["id"], "name": r["name"], "path": r["path"], "role": r["role"],
                       "sources": {k: {"ref": v["ref"], "files": v["files"]} for k, v in r["sources"].items()}}
                      for r in manifest["repositories"] if not args.repo or r["id"] in args.repo]}
        if args.command == "batch-read" and (args.shared_content or args.auto_content):
            shared = review_runtime.shared_reads(value)
            if args.shared_content or len(review_runtime.canonical(shared)) < len(review_runtime.canonical(value)):
                value = shared
        if args.page_size and args.command != "view":
            value = review_runtime.page(value, context, size=args.page_size, offset=args.offset)
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")) if args.page_size or args.shared_content or args.auto_content
              else json.dumps(value, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
