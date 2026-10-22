"""Validate change coverage and connected impact evidence; publish one Markdown Merge Report."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import tempfile
import merge_parent_coverage

from review_workspace import (CONTEXT, RESULT, FixedSourceCache, FixedSearchCache, ancestor_noop,
                              SNAPSHOT_POLICY, evidence_repository, instructions,
                              repository, required_coverage, require, safe_path, source_environment,
                              validate_commit_path, validate_contract)

PRIORITIES = ("P0", "P1", "P2", "P3")
ENGINEERING_FIELDS = ("operation_scenario", "technical_trigger", "technical_explanation", "impact_scope", "engineering_fix")
REPORT_LAYOUT = "engineering-review/v1"
OWNERS = {"開發", "維運設定", "QA"}
SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def redact(value):
    """Omit common credentials from authored prose and report metadata too."""
    value = re.sub(r"(?is)-----BEGIN[^\r\n]*PRIVATE KEY-----.*?-----END[^\r\n]*PRIVATE KEY-----",
                   "[私鑰內容已省略]", str(value))
    value = re.sub(r"(?i)(\b(?:password|passwd|pwd|token|api[_-]?key|secret|access[_-]?key)\b"
                   r"\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^;,\s&]+)", r"\1[已遮蔽]", value)
    value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", r"\1[已遮蔽]@", value)
    return re.sub(r"(?i)(\bBearer\s+)\S+", r"\1[已遮蔽]", value)


def safe_excerpt(raw, path, start, end):
    """Classify the full frozen file before displaying its cited line range."""
    decoded = raw.decode("utf-8", errors="replace")
    suffix = Path(path).suffix.lower()
    if suffix in {".pem", ".key", ".pfx", ".p12"} or re.search(r"(?i)BEGIN.*PRIVATE.*KEY", decoded):
        return None, "認證／私鑰內容已省略；保留固定來源、行號與內容 SHA-256。"
    if suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".config", ".env"} or Path(path).name.startswith(".env"):
        return None, "設定值已省略；保留固定來源、行號與內容 SHA-256。"
    selected = b"\n".join(raw.splitlines()[start - 1:end]).decode("utf-8", errors="replace") if raw.splitlines() else None
    if selected is None:
        return None, None
    sensitive = re.compile(r"(?i)password|passwd|pwd|token|secret|api.?key|access.?key|private.?key|"
                           r"credential|authorization|connection.?string|data\s*source|initial\s*catalog|server\s*=|host\s*=")
    cleaned = "\n".join("[含認證或連線設定的行已省略]" if sensitive.search(line) else redact(line)
                        for line in selected.split("\n"))
    return cleaned, "敏感內容已遮蔽或省略；行號仍對應固定原始內容。" if cleaned != selected else None


class DraftValidationError(ValueError):
    """A repairable authoring error, never a failure of captured evidence."""


def draft_require(condition, message):
    if not condition:
        raise DraftValidationError(message)


def location(item):
    return item.get("repo"), item.get("source"), item.get("path")


def allowed_sources(repo, key):
    source, path = key[1:]
    current = repo["sources"].get(source, {})
    if any(f["path"] == path and f["kind"] == "blob" for f in current.get("files", [])):
        return {source}
    if source == "merge-preview":
        return {"head"}
    # Deleted/renamed old paths must come from this comparison's actual left side.
    return {name for name in ("base", "merge-base")
            if name in repo["sources"] and repo["sources"][name]["ref"] == repo["diff_base"]}


def validate(draft: dict, manifest: dict, context: Path, *, timeout=180.0) -> dict:
    import git_review_context as api
    from review_session import validate_session
    context = validate_session(context)
    token = api.GIT_TIMEOUT_SECONDS.set(api.positive_timeout(str(timeout)))
    try:
        return _validate(draft, manifest, context, api)
    finally:
        api.GIT_TIMEOUT_SECONDS.reset(token)


def validate_coverage_policy(manifest, context, api):
    """Empty coverage needs independently verified owned commit ancestry and trees."""
    if not ancestor_noop(manifest):
        return False
    repo = repository(manifest, manifest["selected_repo"])
    require(isinstance(repo.get("snapshot"), dict), "ancestor proof needs an owned snapshot")
    git_repo = evidence_repository(context, repo)
    path = repo["ancestor_path"]
    bodies = api.run_git(git_repo, ["cat-file", "--batch"], binary=True,
                         input_bytes=("\n".join(path) + "\n").encode("ascii"),
                         env=source_environment(context, repo, repo["sources"]["base"], api))
    validate_commit_path(bodies, path, repo["snapshot"].get("object_format"))
    for name in ("base", "head", "merge-base", "merge-preview"):
        source = repo["sources"][name]
        actual = str(api.run_git(git_repo, ["rev-parse", f"{source['ref']}^{{tree}}"],
                                 env=source_environment(context, repo, source, api))).strip()
        require(actual == source["tree_sha"], "ancestor coverage fixed tree identity mismatch: " + name)
    return True


def validate_captured_evidence(manifest: dict, context: Path, *, timeout=180.0):
    """Before retaining a broken draft, independently reject damaged evidence.

    This guard runs only on the opt-in retry error path. It never marks coverage
    reviewed or replaces the full validation required by the next publication.
    """
    import git_review_context as api
    from review_session import validate_session
    context = validate_session(context)
    token = api.GIT_TIMEOUT_SECONDS.set(api.positive_timeout(str(timeout)))
    try:
        require(manifest.get("schema") == CONTEXT, "workspace context v3 is required")
        require(manifest.get("target") == "branch" and manifest.get("review_basis") == "codex-native",
                "only Codex-native branch reviews are supported")
        repos = {repo["id"]: repo for repo in manifest["repositories"]}
        require(len(repos) == len(manifest["repositories"]), "duplicate Repo IDs")
        require(len({repo["path"] for repo in repos.values()}) == len(repos), "duplicate canonical Repo paths")
        enabled = manifest.get("extension_enabled")
        require(type(enabled) is bool and enabled == (len(repos) > 1), "extension must match the captured Repo count")
        require(manifest.get("scope") == ("group" if enabled else "repo"), "scope differs from detected environment")
        selected = manifest.get("selected_repo")
        require(selected in repos and manifest.get("seed_repos") == [selected], "exactly one selected Repo is required")
        require(repos[selected].get("state") == "captured", "selected Repo snapshot was not captured")
        require(all(repo.get("role") == ("seed" if repo["id"] == selected else "candidate")
                    for repo in repos.values()), "Repo roles differ from the single selected Repo")
        validate_coverage_policy(manifest, context, api)
        require(manifest.get("required_coverage") == required_coverage(manifest), "changed-path contract differs from captured changes")
        frozen = manifest.get("snapshot_policy") is not None
        if frozen:
            require(manifest["snapshot_policy"] == SNAPSHOT_POLICY, "unsupported snapshot policy")
            require(isinstance(manifest.get("capture_completed_at"), str), "snapshot completion time is required")
        for repo in repos.values():
            require(repo.get("state") in ("captured", "error"), "invalid captured Repo state")
            if repo["state"] == "error":
                require(repo.get("sources") == {} and repo.get("instructions") == [] and
                        repo.get("snapshot") is None, "failed candidate exposes partial frozen evidence")
                continue
            if frozen:
                require(repo.get("snapshot") is not None, "frozen context needs an owned snapshot for every captured Repo")
            git_repo = evidence_repository(context, repo)
            verified_trees, objects = {}, set()
            for source in repo["sources"].values():
                environment = source_environment(context, repo, source, api)
                actual = str(api.run_git(git_repo, ["rev-parse", f"{source['ref']}^{{tree}}"], env=environment)).strip()
                require(actual == source["tree_sha"], "fixed tree identity mismatch")
                if actual not in verified_trees:
                    raw = api.run_git(git_repo, ["ls-tree", "-r", "-t", "-z", actual], binary=True, env=environment)
                    entries = []
                    for item in raw.split(b"\0"):
                        if not item:
                            continue
                        metadata, name = item.split(b"\t", 1)
                        mode, kind, object_id = metadata.decode("ascii").split()
                        if kind in ("tree", "blob"):
                            objects.add(object_id)
                        if kind != "tree":
                            entries.append({"path": name.decode("utf-8", errors="strict"),
                                            "mode": mode, "kind": kind, "blob_sha": object_id})
                    verified_trees[actual] = entries
                require(verified_trees[actual] == source["files"], "frozen snapshot inventory differs")
                objects.update((source["ref"], actual))
            if frozen:
                require(repo["sources"], "captured Repo has no frozen sources")
                environment = source_environment(context, repo, next(iter(repo["sources"].values())), api)
                checked = str(api.run_git(git_repo, ["cat-file", "--batch-check"], env=environment,
                                          input_bytes=("\n".join(sorted(objects)) + "\n").encode("ascii")))
                require(len(checked.splitlines()) == len(objects) and
                        all(len(line.split()) == 3 for line in checked.splitlines()),
                        "frozen snapshot object closure is incomplete")
                for index in sorted((git_repo / "objects/pack").glob("*.idx")):
                    api.run_git(git_repo, ["verify-pack", str(index)], env=environment)
                instructions(context, manifest, repo["id"], None, api)
        source_cache = FixedSourceCache(context, api)
        prefix = {**manifest, "changed_contracts": []}
        contracts = {}
        for contract in manifest.get("changed_contracts", []):
            checked = validate_contract(context, prefix, contract, api, cache=source_cache)
            require(checked == contract, "registered contract anchor bytes changed")
            prefix["changed_contracts"].append(contract)
            contracts[contract["id"]] = contract
        searches = {row["id"]: row for row in manifest["searches"]}
        require(len(searches) == len(manifest["searches"]), "duplicate search IDs")
        replay = FixedSearchCache(context, api)
        for row in searches.values():
            require(row.get("repo") in repos and row.get("status") in ("complete", "unresolved"), "invalid search record")
            if row["status"] != "complete":
                continue
            source = repos[row["repo"]]["sources"].get(row.get("source"))
            require(source and row.get("ref") == source["ref"], "search version differs from the selected snapshot")
            contract_id = row.get("contract_id")
            if enabled or contract_id is not None:
                require(contract_id in contracts and row.get("query") in
                        {term["query"] for term in contracts[contract_id]["search_terms"]},
                        "search is not bound to a relevant changed contract")
            code, output, error = replay.run(repos[row["repo"]], row["source"], row["query"])
            require(code in (0, 1) and output == row.get("output") and output.splitlines() == row.get("matches"),
                    "fixed search ledger differs from captured objects: " + error)
    except (api.ReviewContextError, KeyError, TypeError, AttributeError) as exc:
        # This guard is only the new retry path; retain legacy CLI failures in
        # the normal validation path while reporting a fatal retry as exit 2.
        raise ValueError(str(exc)) from exc
    finally:
        api.GIT_TIMEOUT_SECONDS.reset(token)


def _validate(draft, manifest, context, api):
    require(isinstance(manifest, dict) and manifest.get("schema") == CONTEXT, "workspace context v3 is required")
    draft_require(isinstance(draft, dict) and draft.get("schema") == RESULT, "workspace result v3 is required")
    draft_require("report_layout" not in draft or draft["report_layout"] == REPORT_LAYOUT,
                  "unsupported report_layout")
    require(manifest.get("target") == "branch" and manifest.get("review_basis") == "codex-native",
            "only Codex-native branch reviews are supported")
    enabled = manifest.get("extension_enabled")
    require(type(enabled) is bool and enabled == (len(manifest.get("repositories", [])) > 1),
            "extension must match the captured Repo count")
    require(manifest.get("scope") == ("group" if enabled else "repo"), "scope differs from detected environment")
    selected = manifest.get("selected_repo")
    require(isinstance(selected, str) and manifest.get("seed_repos") == [selected],
            "exactly one selected Repo is required")
    require(all(r.get("role") == ("seed" if r.get("id") == selected else "candidate")
                for r in manifest["repositories"]), "Repo roles differ from the single selected Repo")
    if not enabled:
        require(not manifest.get("changed_contracts") and not draft.get("impact_coverage"),
                "single-Repo review does not use cross-Repo impact coverage")
        draft = {**draft, "impact_coverage": [],
                 "repository_coverage": draft.get("repository_coverage", [
                     {"repo": selected, "status": "seed", "reason": "使用者選定的唯一受審 Repo。", "search_ids": []}
                 ])}
    draft_require(isinstance(draft.get("summary"), str) and draft["summary"].strip(), "summary is required")
    draft = {**draft, "pending_confirmations": draft.get("pending_confirmations", [])}
    for field in ("coverage", "impact_coverage", "repository_coverage", "findings", "limitations",
                  "next_steps", "tests_executed", "tests_not_executed", "pending_confirmations"):
        draft_require(isinstance(draft.get(field), list), f"{field} must be an array")
    for field in ("limitations", "tests_executed", "tests_not_executed"):
        draft_require(all(isinstance(s, str) for s in draft[field]), f"{field} must contain text")
    draft_require(len(draft["next_steps"]) <= 3, "next_steps allows up to 3 actions")
    for step in draft["next_steps"]:
        draft_require(isinstance(step, dict) and step.get("owner") in OWNERS
                and isinstance(step.get("action"), str) and step["action"].strip(), "invalid next step")
    repos = {r["id"]: r for r in manifest["repositories"]}
    require(len(repos) == len(manifest["repositories"]), "duplicate Repo IDs")
    require(len({r["path"] for r in repos.values()}) == len(repos), "duplicate canonical Repo paths")
    require(set(manifest["seed_repos"]) <= set(repos), "unknown seed Repo")
    frozen = manifest.get("snapshot_policy") is not None
    if frozen:
        require(manifest["snapshot_policy"] == SNAPSHOT_POLICY, "unsupported snapshot policy")
        require(isinstance(manifest.get("capture_completed_at"), str), "snapshot completion time is required")
        for repo in repos.values():
            if repo["state"] != "error":
                require(repo.get("snapshot") is not None, "frozen context needs an owned snapshot for every captured Repo")
                evidence_repository(context, repo)
                instructions(context, manifest, repo["id"], None, api)
    validate_coverage_policy(manifest, context, api)
    require(manifest.get("required_coverage") == required_coverage(manifest), "changed-path contract differs from captured changes")
    expected = {location(c) for c in manifest["required_coverage"]}
    contracts = {}
    source_cache = FixedSourceCache(context, api)
    contract_manifest = {**manifest, "changed_contracts": []}
    for contract in manifest.get("changed_contracts", []):
        validated = validate_contract(context, contract_manifest, contract, api, cache=source_cache)
        require(contract == validated, "registered contract anchor bytes changed")
        contracts[contract["id"]] = contract
        contract_manifest["changed_contracts"].append(contract)
    verified = set()

    def evidence(e):
        draft_require(isinstance(e, dict) and e.get("repo") in repos, "evidence must name a captured Repo ID")
        repo = repos[e["repo"]]
        name, path = e.get("source"), e.get("path")
        draft_require(name in repo["sources"], "evidence source was not captured")
        safe_path(context, path)
        source = repo["sources"][name]
        draft_require(isinstance(e.get("ref"), str) and SHA.fullmatch(e["ref"])
                and e["ref"] == source["ref"], "evidence ref must match its frozen version")
        version_key = (repo["id"], name)
        if version_key not in verified:
            env = source_environment(context, repo, source, api)
            actual = str(api.run_git(evidence_repository(context, repo), ["rev-parse", f"{source['ref']}^{{tree}}"], env=env)).strip()
            require(actual == source["tree_sha"], "fixed tree identity mismatch")
            verified.add(version_key)
        key = location(e)
        raw = source_cache.read(repo, name, path)
        start, end = e.get("line_start"), e.get("line_end")
        draft_require((start is None and end is None) or
                (type(start) is int and type(end) is int and 1 <= start <= end <= len(raw.splitlines())),
                "evidence line interval is outside the frozen file")
        return key

    def evidence_list(row, required=True):
        values = row.get("evidence", [])
        draft_require(isinstance(values, list) and (bool(values) or not required), "fixed evidence is required")
        return [evidence(e) for e in values]

    parents_complete = True
    if manifest.get("merge_parent_policy") is not None:
        require(manifest["merge_parent_policy"] == merge_parent_coverage.POLICY, "unsupported merge-parent policy")
        selected_repo = repos[selected]
        final = selected_repo["sources"].get("merge-preview", selected_repo["sources"]["head"])
        effective = [] if final["tree_sha"] == selected_repo["sources"]["base"]["tree_sha"] else selected_repo["changed_files"]
        views = merge_parent_coverage.required(effective, selected_repo["native"]["merge_commits"],
            {entry["path"] for entry in final["files"] if entry["kind"] == "blob"})
        require(manifest.get("required_merge_views") == views, "necessary merge-parent views differ")
        parents_complete = merge_parent_coverage.validate(views, draft.get("merge_parent_review", []),
            lambda items: [evidence(item) for item in items], final_ref=final["ref"], parent_repo=selected,
            require=draft_require)

    nodes = {}
    incomplete = bool(manifest["context_gaps"] or draft["limitations"] or not parents_complete)
    gaps = list(manifest["context_gaps"]) + draft["limitations"][:]
    if not parents_complete:
        gaps.append("必要 merge-parent 尚未對照最終版本。")
    workspace_file = Path(manifest["workspace_file"]) if manifest.get("workspace_file") else None
    try:
        # Legacy v3 contexts used the live inventory as their review boundary.
        # Frozen sessions are bounded by their captured inventory and objects.
        if frozen:
            current = captured = set()
        else:
            if workspace_file is not None:
                require(hashlib.sha256(workspace_file.read_bytes()).hexdigest() == manifest.get("workspace_file_sha256"),
                        "declared workspace folders changed after capture")
            current = set(api.discover_repositories(Path(manifest["workspace"]), workspace_file))
            captured = {Path(repo["path"]).resolve() for repo in repos.values()}
        require(current == captured, "declared Repo inventory changed after capture; review again")
    except (OSError, ValueError, api.ReviewContextError) as exc:
        incomplete = True
        gaps.append(str(exc))
    for node in draft["impact_coverage"]:
        draft_require(isinstance(node, dict), "impact node must be an object")
        identifier = node.get("id")
        draft_require(isinstance(identifier, str) and identifier and identifier not in nodes, "duplicate/empty impact ID")
        draft_require(node.get("repo") in repos and node.get("source") in repos[node["repo"]]["sources"],
                "impact node must name a fixed Repo/source")
        draft_require(isinstance(node.get("reason"), str) and node["reason"].strip(), "impact relation/disposition needs a reason")
        draft_require(node.get("status") in ("reviewed", "not-reviewed"), "invalid impact status")
        draft_require(isinstance(node.get("related_to"), list) and all(isinstance(s, str) for s in node["related_to"])
                and len(set(node["related_to"])) == len(node["related_to"]), "related_to must contain unique impact IDs")
        safe_path(context, node.get("path"))
        ids = node.get("contract_ids", [])
        draft_require(isinstance(ids, list) and all(i in contracts for i in ids), "impact contracts must be registered")
        if enabled:
            draft_require(ids, "Group impact nodes must identify their changed/propagated contracts")
        keys = evidence_list(node, node["status"] == "reviewed")
        if node["status"] == "reviewed":
            draft_require(location(node) in keys, "impact evidence must cover its own fixed path/source")
        else:
            incomplete = True
            gaps.append(f"{identifier}: {node['reason']}")
        nodes[identifier] = node
    graph = {identifier: set() for identifier in nodes}
    for identifier, node in nodes.items():
        for neighbor in node["related_to"]:
            draft_require(neighbor in nodes, "impact edge references an unknown node")
            draft_require(neighbor != identifier, "self-link is not an impact edge")
            graph[identifier].add(neighbor)
            graph[neighbor].add(identifier)

    coverage, root_nodes, reviewed_anchors = {}, set(), set()
    for row in draft["coverage"]:
        draft_require(isinstance(row, dict), "coverage row must be an object")
        key = location(row)
        draft_require(key in expected and key not in coverage, "coverage is extra, duplicated, or outside seed changes")
        draft_require(row.get("status") in ("reviewed", "metadata-only", "not-reviewed"), "invalid coverage status")
        keys = evidence_list(row, row["status"] == "reviewed")
        if row["status"] == "reviewed":
            changes = repos[key[0]]["changed_files"]
            draft_require(not any(c["source"] == key[1] and key[2] in (c["path"], c.get("old_path"))
                            and (c.get("binary") or "160000" in (c.get("old_mode"), c.get("new_mode")))
                            for c in changes), "binary/submodule change requires explicit incomplete coverage")
            allowed = allowed_sources(repos[key[0]], key)
            matches = [k for k in keys if k[0] == key[0] and k[2] == key[2] and k[1] in allowed]
            draft_require(matches, "change coverage cites the wrong version/source")
            if enabled:
                ids = row.get("impact_ids")
                draft_require(isinstance(ids, list) and ids and all(i in nodes for i in ids), "each extended change needs impact nodes")
                draft_require(all(location(nodes[i]) in matches for i in ids), "change's impact roots must cover its own frozen evidence")
                root_nodes.update(ids)
            reviewed_anchors.update(matches)
            if enabled:
                applicable = {c["id"] for c in contracts.values()
                              if location(c["root"]) == key and not c["parent_ids"]}
                draft_require(applicable, "each reviewed Group change needs an anchored changed contract")
                draft_require(applicable <= {c for i in ids for c in nodes[i].get("contract_ids", [])},
                        "change impact roots must cover their registered contracts")
        else:
            draft_require(isinstance(row.get("reason"), str) and row["reason"].strip(), "unreviewed change needs a reason")
            incomplete = True
            gaps.append(f"{key}: {row['reason']}")
        coverage[key] = row
    draft_require(set(coverage) == expected, "coverage must include every changed/old/merge-preview path")
    visited, queue = set(), deque(root_nodes)
    while queue:
        current = queue.popleft()
        if current not in visited:
            visited.add(current)
            queue.extend(graph[current] - visited)
    draft_require(set(nodes) == visited, "impact graph includes nodes disconnected from the selected changes")
    if enabled:
        for contract in contracts.values():
            anchor = location(contract["evidence"])
            draft_require(any(location(n) == anchor and contract["id"] in n.get("contract_ids", []) for n in nodes.values()),
                    "every changed/propagated contract needs its own connected impact anchor")

    searches = {s["id"]: s for s in manifest["searches"]}
    require(len(searches) == len(manifest["searches"]), "duplicate search IDs")
    search_cache = FixedSearchCache(context, api)
    for record in searches.values():
        require(record.get("repo") in repos and record.get("status") in ("complete", "unresolved"), "invalid search record")
        if record["status"] == "complete":
            source = repos[record["repo"]]["sources"].get(record.get("source"))
            require(source and record.get("ref") == source["ref"], "search version differs from the selected snapshot")
            identifier = record.get("contract_id")
            if enabled or identifier is not None:
                require(identifier in contracts and record.get("query") in {t["query"] for t in contracts[identifier]["search_terms"]},
                        "search is not bound to a relevant changed contract")
            repo = repos[record["repo"]]
            code, output, error = search_cache.run(repo, record["source"], record["query"])
            require(code in (0, 1) and output == record.get("output") and output.splitlines() == record.get("matches"),
                    "fixed search ledger differs from captured objects: " + error)
    dispositions = {}
    involved = {n["repo"] for n in nodes.values()} | set(manifest["seed_repos"])
    for row in draft["repository_coverage"]:
        draft_require(isinstance(row, dict) and row.get("repo") in repos and row["repo"] not in dispositions,
                "repository_coverage must contain unique captured Repo IDs")
        repo_id, state = row["repo"], row.get("status")
        draft_require(state in ("seed", "related", "not-related", "unresolved"), "invalid Repo disposition")
        draft_require(isinstance(row.get("reason"), str) and row["reason"].strip(), "Repo disposition needs a reason")
        ids = row.get("search_ids", [])
        draft_require(isinstance(ids, list) and all(s in searches and searches[s]["repo"] == repo_id for s in ids),
                "search IDs must come from that captured Repo")
        repo = repos[repo_id]
        if repo_id in manifest["seed_repos"]:
            draft_require(state in ("seed", "unresolved"), "seed cannot be excluded")
        elif repo_id in involved:
            draft_require(state in ("related", "unresolved"), "reachable Repo cannot be excluded")
        else:
            draft_require(state in ("not-related", "unresolved"), "unrelated Repo cannot be expanded without evidence")
        if repo["state"] == "error":
            draft_require(state == "unresolved", "failed Repo capture cannot be claimed reviewed/excluded")
        if expected and repo_id not in manifest["seed_repos"] and state != "unresolved":
            draft_require(ids and any(searches[s]["status"] == "complete" and
                                searches[s]["source"] == repo["review_source"] for s in ids),
                    "candidate Repo needs completed searches against its selected fixed version")
        if expected and enabled and state != "unresolved":
            source_names = (repo["review_source"],)
            for contract_id, contract in contracts.items():
                if contract["evidence"]["repo"] == repo_id:
                    continue
                for source_name in source_names:
                    bound = [searches[s] for s in ids if searches[s]["status"] == "complete" and
                             searches[s]["source"] == source_name and searches[s].get("contract_id") == contract_id]
                    draft_require(bound, "candidate must search every changed/propagated contract in each review source")
                    if state == "not-related":
                        draft_require(len({s["query"] for s in bound}) >= 2,
                                "exclusion needs alternate names or contract terms; one negative search is insufficient")
            if state == "related":
                draft_require(any(searches[s].get("matches") for s in ids), "related Repo needs a fixed contract search match")
        if state == "unresolved":
            incomplete = True
            gaps.append(f"{repo_id}: {row['reason']}")
        if repo_id in involved:
            gaps.extend(f"{repo_id}: {g}" for g in repo["limitations"])
            incomplete = incomplete or bool(repo["limitations"])
        # An excluded candidate can gain a consumer while review is running.
        # Its exclusion is version-specific, so freshness applies to every Repo.
        if not frozen and repo.get("before"):
            now = api.snapshot(Path(repo["path"]))
            native = repo.get("native")
            if native:
                for role in ("base", "head"):
                    current_ref = api.verified_commit(Path(repo["path"]), native[f"{role}_resolved_ref"])
                    if current_ref != native[f"{role}_sha"]:
                        incomplete = True
                        gaps.append(f"{repo_id}: {role} 分支於擷取後改變，請重做審查。")
            fields = ("head", "branch")
            if any(now[f] != repo["before"][f] for f in fields):
                incomplete = True
                gaps.append(f"{repo_id}: 工作區版本於擷取後改變，請重做審查。")
        dispositions[repo_id] = row
    draft_require(set(dispositions) == set(repos), "every candidate Repo needs an explicit disposition")

    pending_fingerprints = set()
    for index, item in enumerate(draft["pending_confirmations"], 1):
        draft_require(isinstance(item, dict) and item.get("id") == f"Q-{index:03}",
                "pending confirmation IDs must be sequential Q-001")
        draft_require(set(item) <= {"id", "title", "reason", "missing_evidence", "verification", "owner", "related_changes", "evidence"},
                "unknown pending confirmation field; human confirmation is recorded only in the report")
        for field in ("title", "reason", "missing_evidence", "verification"):
            draft_require(isinstance(item.get(field), str) and item[field].strip(), f"pending confirmation needs {field}")
        draft_require(item.get("owner") in OWNERS, "pending confirmation needs a valid owner")
        related = item.get("related_changes")
        draft_require(isinstance(related, list) and related and all(isinstance(row, dict) and set(row) == {"repo", "source", "path"}
                    and all(isinstance(row[field], str) and row[field] for field in ("repo", "source", "path"))
                    and location(row) in expected for row in related),
                "pending confirmation must identify captured selected changes")
        draft_require(len({location(row) for row in related}) == len(related), "duplicate pending related change")
        keys = evidence_list(item, required=False)
        draft_require(all(key[0] in involved for key in keys), "pending evidence cannot expand excluded Repos")
        fingerprint = tuple(item[field].strip() for field in ("title", "reason", "missing_evidence"))
        draft_require(fingerprint not in pending_fingerprints, "duplicate pending confirmation")
        pending_fingerprints.add(fingerprint)

    findings = []
    fingerprints, last_priority = set(), -1
    for index, finding in enumerate(draft["findings"], 1):
        draft_require(isinstance(finding, dict) and finding.get("id") == f"F-{index:03}", "finding IDs must be sequential")
        priority = finding.get("priority")
        draft_require(priority in PRIORITIES and PRIORITIES.index(priority) >= last_priority, "findings must be ordered P0-P3")
        last_priority = PRIORITIES.index(priority)
        for field in ("title", "impact", "trigger", "expected", "actual", "recommendation"):
            draft_require(isinstance(finding.get(field), str) and finding[field].strip(), f"finding needs {field}")
        for field in ("operation_scenario", "technical_trigger", "technical_explanation", "impact_scope",
                      "evidence_summary", "engineering_fix"):
            draft_require(field not in finding or isinstance(finding[field], str) and finding[field].strip(),
                          f"finding {field} must be nonempty text when supplied")
        if draft.get("report_layout") == REPORT_LAYOUT:
            for field in ENGINEERING_FIELDS:
                draft_require(isinstance(finding.get(field), str) and finding[field].strip()
                              and finding[field].strip() != "未提供",
                              f"report_layout={REPORT_LAYOUT} finding needs complete {field}")
        draft_require(finding.get("owner") in OWNERS and finding.get("scenario_source") in ("code-derived", "reproduced"),
                "finding needs a valid owner and scenario_source")
        keys = evidence_list(finding)
        draft_require(any(k in reviewed_anchors for k in keys), "finding must cite a reviewed seed change in its correct version")
        draft_require(all(k[0] in involved for k in keys), "finding evidence cannot silently expand excluded Repos")
        fingerprint = tuple(finding[f].strip() for f in ("title", "trigger", "expected", "actual"))
        draft_require(fingerprint not in fingerprints, "duplicate finding")
        fingerprints.add(fingerprint)
        excerpts = []
        for item in finding["evidence"]:
            raw = source_cache.read(repos[item["repo"]], item["source"], item["path"])
            raw_lines = raw.splitlines()
            start, end = item.get("line_start"), item.get("line_end")
            draft_require(not raw_lines or (start is not None and end is not None),
                    "nonempty finding evidence needs explicit line_start and line_end")
            # Match the byte-line intervals checked above before decoding Unicode.
            selected_lines = b"\n".join(raw_lines[start - 1:end]) if raw_lines else None
            excerpt, note = safe_excerpt(raw, item["path"], start, end)
            excerpts.append({**item, "excerpt": excerpt, "snapshot_note": note,
                             "content_sha256": hashlib.sha256(raw).hexdigest(), "excerpt_replaced_bytes":
                             selected_lines is not None and selected_lines.decode("utf-8", errors="replace").encode("utf-8") != selected_lines})
        findings.append({**finding, "evidence": excerpts})
    counts = {p: sum(f["priority"] == p for f in draft["findings"]) for p in PRIORITIES}
    pending = bool(draft["pending_confirmations"])
    state = "審查未完成" if incomplete else ("待人工確認" if pending else ("沒有差異" if not expected else
            ("發現具體問題" if draft["findings"] else "未發現具體問題")))
    recommendation = "需補做審查" if incomplete else ("暫緩合併" if counts["P0"] else
                     ("修正後再合併" if counts["P1"] else ("需人工確認" if pending else "可以合併")))
    return {**draft, "findings": findings, "review_status": state, "review_complete": not (incomplete or pending),
            "pending_confirmation_count": len(draft["pending_confirmations"]),
            "recommendation": recommendation, "priority_counts": counts,
            "limitations": list(dict.fromkeys(gaps)), "scope": manifest["scope"], "target": manifest["target"],
            "involved_repos": sorted(involved), "versions": [{"repo": r["id"], "path": r["path"],
                 "sources": {k: v["ref"] for k, v in r["sources"].items()}} for r in repos.values()],
            "searches": manifest["searches"],
            "changed_contracts": manifest.get("changed_contracts", []),
             "coverage_schema": "branch-v3",
             "selected_repo": selected, "review_basis": "codex-native",
             "extension_enabled": enabled,
             "extension_status": ("incomplete" if incomplete else "complete") if enabled else "disabled",
             "report_style": "review-report-zh-TW/v1",
             "snapshot_policy": manifest.get("snapshot_policy"),
            "context_sha256": hashlib.sha256((context / "manifest.json").read_bytes()).hexdigest()}


def render(result, manifest):
    def mark(value):
        text = redact(value)
        fence = "`" * max(1, max((len(run) + 1 for run in re.findall(r"`+", text)), default=1))
        return fence + (" " if text.startswith("`") else "") + text + (" " if text.endswith("`") else "") + fence

    def cell(value):
        return redact(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    def optional(finding, field):
        return finding.get(field) or "未提供"

    def paragraphs(*values, marker=None):
        """Keep distinct authored facts while omitting exact repeated paragraphs."""
        seen, output = set(), []
        for value in values:
            for paragraph in re.split(r"\n[ \t]*\n", value.strip()):
                if marker and paragraph.rstrip("。.") == marker:
                    continue
                if paragraph and paragraph not in seen:
                    seen.add(paragraph)
                    output.extend([paragraph, ""])
        if marker:
            output.extend([marker + "。", ""])
        return output

    seed = repository(manifest, manifest["selected_repo"])
    native = seed["native"]
    engineering = result.get("report_layout") == REPORT_LAYOUT
    lines = [f"**{result['recommendation']}**｜{result['review_status']}。{result['summary']}", "",
             "問題數量：" + "／".join(f"{p} {result['priority_counts'][p]}" for p in PRIORITIES), ""]
    if engineering:
        lines[:0] = [f"# 合併審查: {seed['name']}", "", "## 審查結論", ""]
    if result["pending_confirmation_count"]:
        lines.extend([f"待人工確認：{result['pending_confirmation_count']} 項。", ""])
    comparison_start = len(lines)
    lines.extend(["### 比較資訊" if engineering else "## 比較資訊", "", f"- Repo：{mark(seed['name'])}", f"- 本機路徑：{mark(seed['path'])}", "",
                  "| 角色 | 輸入分支 | 實際 ref | Commit（前 8 碼） |", "| --- | --- | --- | --- |"])
    for role, label in (("base", "基礎分支"), ("head", "比較分支")):
        lines.append(f"| {label} | {cell(mark(native[f'{role}_input']))} | "
                     f"{cell(mark(native[f'{role}_resolved_ref']))} | {mark(seed['sources'][role]['ref'][:8])} |")
    lines.extend(["", f"- 合併方向：{mark(native['head_input'])} → {mark(native['base_input'])}",
                  f"- 主要差異範圍（共同祖先 → 比較分支）：{mark(native['diff_base'][:8])} → "
                  f"{mark(native['review_right'][:8])}", ""])
    if ancestor_noop(manifest):
        lines.extend(["- 比較分支已包含於基礎分支；合併預覽與基礎版本相同，沒有新增程式碼差異。", ""])
    if manifest.get("snapshot_policy") == SNAPSHOT_POLICY:
        lines.extend([f"- 快照擷取：{manifest['generated_at']} → {manifest['capture_completed_at']}（UTC）。",
                      f"- 全部 Repo SHA 固定完成：{manifest.get('refs_pinned_at', '未提供')}（UTC）。",
                      "- 適用版本：本報告僅適用於上述固定 SHA 及本次擷取的相關 Repo；後續分支更新不改變本次結論。", ""])
    comparison = []
    if engineering:
        comparison, lines = lines[comparison_start:], lines[:comparison_start]
        lines.extend(["## 問題總攬", "", "| 問題 | 優先程度 | 名稱 | 影響 |",
                      "| --- | --- | --- | --- |"])
        lines.extend(f"| {cell(f['id'])} | {cell(f['priority'])} | {cell(f['title'])} | {cell(f['impact'])} |"
                     for f in result["findings"])
        if not result["findings"]:
            lines.append("| 無已確認問題 | — | — | — |")
        lines.extend(["", "## 問題詳情（工程師參考）", ""])
    else:
        lines.extend(["## 問題", ""])
    if not result["findings"]:
        lines.extend(["無已確認問題。", ""])
    for finding in result["findings"]:
        label = "依程式推導，未實際重現" if finding["scenario_source"] == "code-derived" else "已實際重現"
        lines.extend([f"<a id=\"finding-{finding['id']}\"></a>", "",
                      f"### {finding['id']}｜[{finding['priority']}] {finding['title']}", "",
                      "**問題描述**", "",
                      *paragraphs(finding["impact"], optional(finding, "impact_scope")),
                      "**重現原因**", "",
                      *paragraphs(optional(finding, "operation_scenario"), finding["trigger"],
                                  optional(finding, "technical_trigger"), marker=label),
                      "**預期結果**", "", finding["expected"], "",
                      "**實際結果**", "", finding["actual"], "", "**證據**", "",
                      *paragraphs(optional(finding, "technical_explanation"), finding.get("evidence_summary", ""))])
        for evidence in finding["evidence"]:
            span = "" if evidence.get("line_start") is None else f":{evidence['line_start']}–{evidence['line_end']}"
            name = repository(manifest, evidence["repo"])["name"]
            lines.extend([f"{mark(name + '/' + evidence['path'] + span)}（來源：{mark(evidence['source'])}；"
                          f"固定版本：{mark(evidence['ref'])}）", ""])
            excerpt = evidence["excerpt"]
            if evidence.get("snapshot_note"):
                lines.extend([evidence["snapshot_note"], "", f"內容 SHA-256：{mark(evidence['content_sha256'])}", ""])
            if excerpt is None:
                if not evidence.get("snapshot_note"):
                    lines.extend(["固定版本內容為空（0 行）", ""])
            else:
                fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", excerpt)), default=0))
                lines.extend([fence, excerpt, fence, ""])
                if evidence.get("excerpt_replaced_bytes"):
                    lines.extend(["片段以 UTF-8 顯示；無法解碼的位元組以 � 取代。", ""])
        fixes = paragraphs(finding["recommendation"], optional(finding, "engineering_fix"))
        fixes[-2] += f"（{finding['owner']}）"
        lines.extend(["**建議改善方式**", "", *fixes])
    lines.extend(["## 範圍與限制", "",
                  "- 審查起點：所選 Repo 的分支差異；延伸僅追查這些變更的契約與消費端。",
                  f"- 跨 Repo 延伸：{result['extension_status']}；{manifest['extension_reason']}",
                  "- 已執行檢查：" + ("；".join(result["tests_executed"]) or "無"),
                  "- 未執行檢查：" + ("；".join(result["tests_not_executed"]) or "未記錄其他檢查")])
    lines.extend(f"- 檢查缺口：{gap}" for gap in result["limitations"])
    lines.append("")
    if result.get("pending_confirmations"):
        lines.extend(["### 待人工確認", "",
                      f"共 {result['pending_confirmation_count']} 項，未列為已確認問題或計入 P0–P3；完成核對前不視為通過。", ""])
        for item in result["pending_confirmations"]:
            changes = "、".join(repository(manifest, row["repo"])["name"] + "/" + row["path"]
                               for row in item["related_changes"])
            lines.extend([f"### {item['id']}｜{item['title']}", "",
                          f"- 關聯變更：{changes}", f"- 待確認原因：{item['reason']}",
                          f"- 缺少證據：{item['missing_evidence']}",
                          f"- 確認方式：{item['verification']}（{item['owner']}）"])
            for entry in item.get("evidence", []):
                name = repository(manifest, entry["repo"])["name"]
                span = f":{entry['line_start']}–{entry['line_end']}" if entry.get("line_start") is not None else ""
                lines.append(f"- 已有證據：{mark(name + '/' + entry['path'] + span)}；來源 {mark(entry['source'])}；固定版本 {mark(entry['ref'])}")
            lines.append("")
        lines.extend(["僅編輯下列人工核對紀錄；保留上方固定判斷、缺口與證據。勾選不會自動重算審查結論。", "",
                      "<!-- merge-confirmations:start -->", ""])
        for item in result["pending_confirmations"]:
            lines.extend([f"- [ ] {item['id']} 已完成核對", "", "核對人：＿＿；日期：＿＿；確認結果與證據：＿＿", ""])
        lines.extend(["<!-- merge-confirmations:end -->", ""])
    lines.extend(["### 下一步" if engineering else "## 下一步", ""])
    if result["next_steps"]:
        lines.extend(f"- {step['action']}（{step['owner']}）" for step in result["next_steps"])
    else:
        lines.append("無額外下一步。")
    lines.extend(["", "## 技術審查紀錄（工程師參考）" if engineering else "## 技術審查紀錄",
                  "", "<details>", "<summary>展開完整技術審查紀錄</summary>", "", *comparison])
    lines.extend(["### 固定版本", "", "| Repo | 角色 | 來源 | 完整 SHA |", "| --- | --- | --- | --- |"])
    for version in result["versions"]:
        repo = repository(manifest, version["repo"])
        for source, reference in version["sources"].items():
            lines.append(f"| {cell(repo['name'])} | {repo['role']} | {cell(source)} | {mark(reference)} |")
        if not version["sources"]:
            lines.append(f"| {cell(repo['name'])} | {repo['role']} | 未擷取 | 未提供 |")
    lines.extend(["", "### 逐檔覆蓋", "", "| Repo／路徑 | 來源 | 狀態 | 判定與證據 |", "| --- | --- | --- | --- |"])
    for row in result["coverage"]:
        repo = repository(manifest, row["repo"])
        evidence = "; ".join(f"{entry['source']}@{entry['ref']}" +
                             (f":{entry['line_start']}–{entry['line_end']}" if entry.get("line_start") is not None else "")
                             for entry in row.get("evidence", []))
        lines.append("| " + " | ".join(cell(value) for value in
                     (repo["name"] + "/" + row["path"], row["source"], row["status"],
                      (row.get("reason", "") + " " + evidence).strip() or "未提供")) + " |")
    if not result["coverage"]:
        lines.append("| 無必要變更路徑 | — | — | — |")
    lines.extend(["", "### 功能鏈與 Repo 判定", ""])
    for row in result["repository_coverage"]:
        repo = repository(manifest, row["repo"])
        lines.append(f"- {mark(repo['name'])}：{row['status']}；{row['reason']}；搜尋 {', '.join(row.get('search_ids', [])) or '無'}")
    for node in result["impact_coverage"]:
        repo = repository(manifest, node["repo"])
        lines.append(f"- {node['id']}：{mark(repo['name'] + '/' + node['path'])}；{node['source']}；{node['status']}；"
                     f"契約 {', '.join(node.get('contract_ids', [])) or '無'}；連線 {', '.join(node['related_to']) or '起點'}；{node['reason']}")
    if result.get("merge_parent_review"):
        lines.extend(["", "### 必要合併父版本對照", ""])
        for item in result["merge_parent_review"]:
            lines.append(f"- {mark(item['id'])}：{item['status']}；{redact(item['reason'])}")
    lines.extend(["", "### 變更與延伸契約", ""])
    for contract in result["changed_contracts"]:
        root = contract["root"]
        repo = repository(manifest, root["repo"])
        lines.append(f"- {contract['id']}：{contract['kind']}／{mark(contract['symbol'])}；"
                     f"起點 {mark(repo['name'] + '/' + root['path'])}；父契約 {', '.join(contract.get('parent_ids', [])) or '無'}")
    if not result["changed_contracts"]:
        lines.append("無跨 Repo 契約。")
    lines.extend(["", "### 固定版本搜尋紀錄", "",
                  "僅列搜尋識別、契約、固定來源、狀態及命中數；原始搜尋輸出與設定值不複製到報告。", "",
                  "| 搜尋 | Repo | 契約 | 來源／完整 SHA | 狀態 | 命中數 |", "| --- | --- | --- | --- | --- | --- |"])
    for row in result["searches"]:
        repo = repository(manifest, row["repo"])
        lines.append("| " + " | ".join(cell(value) for value in
                     (row["id"], repo["name"], row.get("contract_id") or "無", row.get("source", "") + "@" + row.get("ref", ""),
                      row["status"], len(row.get("matches", [])))) + " |")
    if not result["searches"]:
        lines.append("| 無搜尋紀錄 | — | — | — | — | 0 |")
    lines.extend(["", "</details>"])
    return redact("\n".join(lines)) + "\n"


def publish(context, draft, manifest, report_dir, *, timeout=180.0, on_stage=None):
    result = validate(draft, manifest, context, timeout=timeout)
    seed = repository(manifest, manifest["selected_repo"])
    base = seed["sources"]["base"]["ref"]
    head = seed["sources"]["head"]["ref"]
    require(isinstance(base, str) and SHA.fullmatch(base)
            and isinstance(head, str) and SHA.fullmatch(head), "report name requires full branch commit SHAs")
    report_dir = Path(report_dir or Path(seed["path"]) / "review-reports").expanduser().resolve()
    require(not report_dir.is_relative_to(context.resolve()), "report directory cannot be inside owned temporary context")
    report_dir.mkdir(parents=True, exist_ok=True)
    published_at = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix=".merge-review-", dir=report_dir) as temporary:
        stage = Path(temporary) / "report.md"
        stage.write_text(render(result, manifest), encoding="utf-8")
        for offset in range(1000):
            timestamp = (published_at + timedelta(microseconds=offset)).strftime("%Y%m%dT%H%M%S%fZ")
            output = report_dir / f"merge-review-{timestamp}-{base[:8]}-{head[:8]}.md"
            if on_stage:
                on_stage(output, hashlib.sha256(stage.read_bytes()).hexdigest())
            try:
                # Link the complete file atomically; an existing destination is never replaced.
                os.link(stage, output)
            except FileExistsError:
                continue
            return output
    raise ValueError("report filename collisions exceeded limit")
