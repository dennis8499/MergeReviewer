"""Validate change coverage and connected impact evidence; publish one Markdown Merge Report."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import tempfile

from review_workspace import (CONTEXT, RESULT, FixedSourceCache,
                              repository, required_coverage, require, safe_path, source_environment,
                              validate_contract)

PRIORITIES = ("P0", "P1", "P2", "P3")
OWNERS = {"開發", "維運設定", "QA"}
SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


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


def _validate(draft, manifest, context, api):
    require(isinstance(manifest, dict) and manifest.get("schema") == CONTEXT, "workspace context v3 is required")
    require(isinstance(draft, dict) and draft.get("schema") == RESULT, "workspace result v3 is required")
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
    require(isinstance(draft.get("summary"), str) and draft["summary"].strip(), "summary is required")
    for field in ("coverage", "impact_coverage", "repository_coverage", "findings", "limitations",
                  "next_steps", "tests_executed", "tests_not_executed"):
        require(isinstance(draft.get(field), list), f"{field} must be an array")
    for field in ("limitations", "tests_executed", "tests_not_executed"):
        require(all(isinstance(s, str) for s in draft[field]), f"{field} must contain text")
    require(len(draft["next_steps"]) <= 3, "next_steps allows up to 3 actions")
    for step in draft["next_steps"]:
        require(isinstance(step, dict) and step.get("owner") in OWNERS
                and isinstance(step.get("action"), str) and step["action"].strip(), "invalid next step")
    repos = {r["id"]: r for r in manifest["repositories"]}
    require(len(repos) == len(manifest["repositories"]), "duplicate Repo IDs")
    require(len({r["path"] for r in repos.values()}) == len(repos), "duplicate canonical Repo paths")
    require(set(manifest["seed_repos"]) <= set(repos), "unknown seed Repo")
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
        require(isinstance(e, dict) and e.get("repo") in repos, "evidence must name a captured Repo ID")
        repo = repos[e["repo"]]
        name, path = e.get("source"), e.get("path")
        require(name in repo["sources"], "evidence source was not captured")
        safe_path(context, path)
        source = repo["sources"][name]
        require(isinstance(e.get("ref"), str) and SHA.fullmatch(e["ref"])
                and e["ref"] == source["ref"], "evidence ref must match its frozen version")
        version_key = (repo["id"], name)
        if version_key not in verified:
            env = source_environment(context, repo, source, api)
            actual = str(api.run_git(Path(repo["path"]), ["rev-parse", f"{source['ref']}^{{tree}}"], env=env)).strip()
            require(actual == source["tree_sha"], "fixed tree identity mismatch")
            verified.add(version_key)
        key = location(e)
        raw = source_cache.read(repo, name, path)
        start, end = e.get("line_start"), e.get("line_end")
        require((start is None and end is None) or
                (type(start) is int and type(end) is int and 1 <= start <= end <= len(raw.splitlines())),
                "evidence line interval is outside the frozen file")
        return key

    def evidence_list(row, required=True):
        values = row.get("evidence", [])
        require(isinstance(values, list) and (bool(values) or not required), "fixed evidence is required")
        return [evidence(e) for e in values]

    nodes = {}
    incomplete = bool(manifest["context_gaps"] or draft["limitations"])
    gaps = list(manifest["context_gaps"]) + draft["limitations"][:]
    workspace_file = Path(manifest["workspace_file"]) if manifest.get("workspace_file") else None
    try:
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
        require(isinstance(node, dict), "impact node must be an object")
        identifier = node.get("id")
        require(isinstance(identifier, str) and identifier and identifier not in nodes, "duplicate/empty impact ID")
        require(node.get("repo") in repos and node.get("source") in repos[node["repo"]]["sources"],
                "impact node must name a fixed Repo/source")
        require(isinstance(node.get("reason"), str) and node["reason"].strip(), "impact relation/disposition needs a reason")
        require(node.get("status") in ("reviewed", "not-reviewed"), "invalid impact status")
        require(isinstance(node.get("related_to"), list) and all(isinstance(s, str) for s in node["related_to"])
                and len(set(node["related_to"])) == len(node["related_to"]), "related_to must contain unique impact IDs")
        safe_path(context, node.get("path"))
        ids = node.get("contract_ids", [])
        require(isinstance(ids, list) and all(i in contracts for i in ids), "impact contracts must be registered")
        if enabled:
            require(ids, "Group impact nodes must identify their changed/propagated contracts")
        keys = evidence_list(node, node["status"] == "reviewed")
        if node["status"] == "reviewed":
            require(location(node) in keys, "impact evidence must cover its own fixed path/source")
        else:
            incomplete = True
            gaps.append(f"{identifier}: {node['reason']}")
        nodes[identifier] = node
    graph = {identifier: set() for identifier in nodes}
    for identifier, node in nodes.items():
        for neighbor in node["related_to"]:
            require(neighbor in nodes, "impact edge references an unknown node")
            require(neighbor != identifier, "self-link is not an impact edge")
            graph[identifier].add(neighbor)
            graph[neighbor].add(identifier)

    coverage, root_nodes, reviewed_anchors = {}, set(), set()
    for row in draft["coverage"]:
        require(isinstance(row, dict), "coverage row must be an object")
        key = location(row)
        require(key in expected and key not in coverage, "coverage is extra, duplicated, or outside seed changes")
        require(row.get("status") in ("reviewed", "metadata-only", "not-reviewed"), "invalid coverage status")
        keys = evidence_list(row, row["status"] == "reviewed")
        if row["status"] == "reviewed":
            changes = repos[key[0]]["changed_files"]
            require(not any(c["source"] == key[1] and key[2] in (c["path"], c.get("old_path"))
                            and (c.get("binary") or "160000" in (c.get("old_mode"), c.get("new_mode")))
                            for c in changes), "binary/submodule change requires explicit incomplete coverage")
            allowed = allowed_sources(repos[key[0]], key)
            matches = [k for k in keys if k[0] == key[0] and k[2] == key[2] and k[1] in allowed]
            require(matches, "change coverage cites the wrong version/source")
            if enabled:
                ids = row.get("impact_ids")
                require(isinstance(ids, list) and ids and all(i in nodes for i in ids), "each extended change needs impact nodes")
                require(all(location(nodes[i]) in matches for i in ids), "change's impact roots must cover its own frozen evidence")
                root_nodes.update(ids)
            reviewed_anchors.update(matches)
            if enabled:
                applicable = {c["id"] for c in contracts.values()
                              if location(c["root"]) == key and not c["parent_ids"]}
                require(applicable, "each reviewed Group change needs an anchored changed contract")
                require(applicable <= {c for i in ids for c in nodes[i].get("contract_ids", [])},
                        "change impact roots must cover their registered contracts")
        else:
            require(isinstance(row.get("reason"), str) and row["reason"].strip(), "unreviewed change needs a reason")
            incomplete = True
            gaps.append(f"{key}: {row['reason']}")
        coverage[key] = row
    require(set(coverage) == expected, "coverage must include every changed/old/merge-preview path")
    visited, queue = set(), deque(root_nodes)
    while queue:
        current = queue.popleft()
        if current not in visited:
            visited.add(current)
            queue.extend(graph[current] - visited)
    require(set(nodes) == visited, "impact graph includes nodes disconnected from the selected changes")
    if enabled:
        for contract in contracts.values():
            anchor = location(contract["evidence"])
            require(any(location(n) == anchor and contract["id"] in n.get("contract_ids", []) for n in nodes.values()),
                    "every changed/propagated contract needs its own connected impact anchor")

    searches = {s["id"]: s for s in manifest["searches"]}
    require(len(searches) == len(manifest["searches"]), "duplicate search IDs")
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
            env = source_environment(context, repo, source, api)
            code, output, error = api.run_git_capture(Path(repo["path"]),
                ["grep", "-n", "-I", "--no-textconv", "-F", "-e", record["query"], source["ref"], "--"], env=env)
            require(code in (0, 1) and output == record.get("output") and output.splitlines() == record.get("matches"),
                    "fixed search ledger differs from captured objects: " + error)
    dispositions = {}
    involved = {n["repo"] for n in nodes.values()} | set(manifest["seed_repos"])
    for row in draft["repository_coverage"]:
        require(isinstance(row, dict) and row.get("repo") in repos and row["repo"] not in dispositions,
                "repository_coverage must contain unique captured Repo IDs")
        repo_id, state = row["repo"], row.get("status")
        require(state in ("seed", "related", "not-related", "unresolved"), "invalid Repo disposition")
        require(isinstance(row.get("reason"), str) and row["reason"].strip(), "Repo disposition needs a reason")
        ids = row.get("search_ids", [])
        require(isinstance(ids, list) and all(s in searches and searches[s]["repo"] == repo_id for s in ids),
                "search IDs must come from that captured Repo")
        repo = repos[repo_id]
        if repo_id in manifest["seed_repos"]:
            require(state in ("seed", "unresolved"), "seed cannot be excluded")
        elif repo_id in involved:
            require(state in ("related", "unresolved"), "reachable Repo cannot be excluded")
        else:
            require(state in ("not-related", "unresolved"), "unrelated Repo cannot be expanded without evidence")
        if repo["state"] == "error":
            require(state == "unresolved", "failed Repo capture cannot be claimed reviewed/excluded")
        if expected and repo_id not in manifest["seed_repos"] and state != "unresolved":
            require(ids and any(searches[s]["status"] == "complete" and
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
                    require(bound, "candidate must search every changed/propagated contract in each review source")
                    if state == "not-related":
                        require(len({s["query"] for s in bound}) >= 2,
                                "exclusion needs alternate names or contract terms; one negative search is insufficient")
            if state == "related":
                require(any(searches[s].get("matches") for s in ids), "related Repo needs a fixed contract search match")
        if state == "unresolved":
            incomplete = True
            gaps.append(f"{repo_id}: {row['reason']}")
        if repo_id in involved:
            gaps.extend(f"{repo_id}: {g}" for g in repo["limitations"])
            incomplete = incomplete or bool(repo["limitations"])
        # An excluded candidate can gain a consumer while review is running.
        # Its exclusion is version-specific, so freshness applies to every Repo.
        if repo.get("before"):
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
    require(set(dispositions) == set(repos), "every candidate Repo needs an explicit disposition")

    findings = []
    fingerprints, last_priority = set(), -1
    for index, finding in enumerate(draft["findings"], 1):
        require(isinstance(finding, dict) and finding.get("id") == f"F-{index:03}", "finding IDs must be sequential")
        priority = finding.get("priority")
        require(priority in PRIORITIES and PRIORITIES.index(priority) >= last_priority, "findings must be ordered P0-P3")
        last_priority = PRIORITIES.index(priority)
        for field in ("title", "impact", "trigger", "expected", "actual", "recommendation"):
            require(isinstance(finding.get(field), str) and finding[field].strip(), f"finding needs {field}")
        require(finding.get("owner") in OWNERS and finding.get("scenario_source") in ("code-derived", "reproduced"),
                "finding needs a valid owner and scenario_source")
        keys = evidence_list(finding)
        require(any(k in reviewed_anchors for k in keys), "finding must cite a reviewed seed change in its correct version")
        require(all(k[0] in involved for k in keys), "finding evidence cannot silently expand excluded Repos")
        fingerprint = tuple(finding[f].strip() for f in ("title", "trigger", "expected", "actual"))
        require(fingerprint not in fingerprints, "duplicate finding")
        fingerprints.add(fingerprint)
        excerpts = []
        for item in finding["evidence"]:
            raw_lines = source_cache.read(repos[item["repo"]], item["source"], item["path"]).splitlines()
            start, end = item.get("line_start"), item.get("line_end")
            require(not raw_lines or (start is not None and end is not None),
                    "nonempty finding evidence needs explicit line_start and line_end")
            # Match the byte-line intervals checked above before decoding Unicode.
            selected_lines = b"\n".join(raw_lines[start - 1:end]) if raw_lines else None
            excerpt = selected_lines.decode("utf-8", errors="replace") if selected_lines is not None else None
            excerpts.append({**item, "excerpt": excerpt, "excerpt_replaced_bytes":
                             excerpt is not None and excerpt.encode("utf-8") != selected_lines})
        findings.append({**finding, "evidence": excerpts})
    counts = {p: sum(f["priority"] == p for f in draft["findings"]) for p in PRIORITIES}
    state = "審查未完成" if incomplete else ("沒有差異" if not expected else
            ("發現具體問題" if draft["findings"] else "未發現具體問題"))
    recommendation = "需補做審查" if incomplete else ("暫緩合併" if counts["P0"] else
                     ("修正後再合併" if counts["P1"] else "可以合併"))
    return {**draft, "findings": findings, "review_status": state, "review_complete": not incomplete,
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
             "report_style": "merge-report-concise/v1",
            "context_sha256": hashlib.sha256((context / "manifest.json").read_bytes()).hexdigest()}


def render(result, manifest):
    def mark(value):
        return chr(96) + str(value) + chr(96)

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    seed = repository(manifest, manifest["selected_repo"])
    native = seed["native"]
    lines = [f"# Merge Report: {seed['name']}", "",
             f"**{result['recommendation']}**｜{result['review_status']}。{result['summary']}", "",
             "問題數量：" + "／".join(f"{p} {result['priority_counts'][p]}" for p in PRIORITIES), ""]
    lines.extend(["## 比較資訊", "",
                  f"- Repo：{mark(seed['name'])}",
                  f"- 本機路徑：{mark(seed['path'])}", "",
                  "| 角色 | 輸入分支 | 實際 ref | Commit（前 8 碼） |",
                  "| --- | --- | --- | --- |"])
    for role, label in (("base", "基礎分支"), ("head", "比較分支")):
        lines.append(f"| {label} | {cell(mark(native[f'{role}_input']))} | "
                     f"{cell(mark(native[f'{role}_resolved_ref']))} | {mark(seed['sources'][role]['ref'][:8])} |")
    lines.extend(["", f"- 合併方向：{mark(native['head_input'])} → {mark(native['base_input'])}",
                  f"- 主要差異範圍（共同祖先 → 比較分支）：{mark(native['diff_base'][:8])} → "
                  f"{mark(native['review_right'][:8])}", ""])
    if result["findings"]:
        lines.extend(["## 問題", ""])
        for finding in result["findings"]:
            label = "依程式推導，未實際重現" if finding["scenario_source"] == "code-derived" else "已實際重現"
            lines.extend([f"### {finding['id']}｜[{finding['priority']}] {finding['title']}", "",
                          "**問題描述**", "", finding["impact"], "",
                          "**重現原因**", "", finding["trigger"], "", f"{label}。", "",
                          "**預期結果**", "", finding["expected"], "",
                          "**實際結果**", "", finding["actual"], "",
                          "**證據**", ""])
            if finding.get("evidence_summary"):
                lines.extend([finding["evidence_summary"], ""])
            for evidence in finding["evidence"]:
                span = "" if evidence.get("line_start") is None else f":{evidence['line_start']}–{evidence['line_end']}"
                name = repository(manifest, evidence["repo"])["name"]
                position = name + "/" + evidence["path"] + span
                lines.extend([f"{mark(position)}（來源：{mark(evidence['source'])}；"
                              f"固定版本：{mark(evidence['ref'])}）", ""])
                excerpt = evidence["excerpt"]
                if excerpt is None:
                    lines.extend(["固定版本內容為空（0 行）", ""])
                else:
                    fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", excerpt)), default=0))
                    lines.extend([fence, excerpt, fence, ""])
                    if evidence.get("excerpt_replaced_bytes"):
                        lines.extend(["片段以 UTF-8 顯示；無法解碼的位元組以 � 取代。", ""])
            lines.extend(["**建議改善方式**", "",
                          f"{finding['recommendation']}（{finding['owner']}）", ""])
            if finding.get("engineering_fix"):
                lines.extend([finding["engineering_fix"], ""])
    lines.extend(["## 範圍與限制", "",
                  f"- 跨 Repo 延伸：{result['extension_status']}；{manifest['extension_reason']}",
                  "- 已執行檢查：" + ("；".join(result["tests_executed"]) or "無"),
                  "- 未執行檢查：" + ("；".join(result["tests_not_executed"]) or "未記錄其他檢查")])
    lines.extend(f"- 檢查缺口：{gap}" for gap in result["limitations"])
    if result["next_steps"]:
        lines.extend(["", "## 下一步", ""])
        lines.extend(f"- {step['action']}（{step['owner']}）" for step in result["next_steps"])
    return "\n".join(lines) + "\n"


def publish(context, draft, manifest, report_dir, *, timeout=180.0):
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
            try:
                # Link the complete file atomically; an existing destination is never replaced.
                os.link(stage, output)
            except FileExistsError:
                continue
            return output
    raise ValueError("report filename collisions exceeded limit")
