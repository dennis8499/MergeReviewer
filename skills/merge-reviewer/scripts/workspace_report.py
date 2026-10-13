"""Validate change coverage and connected impact evidence; publish local reports."""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from review_workspace import (CONTEXT, RESULT, LEGACY_CONTEXT, LEGACY_RESULT, fixed_bytes,
                              repository, required_coverage, require, safe_path, source_environment,
                              validate_contract)

PRIORITIES = ("P0", "P1", "P2", "P3")
OWNERS = {"開發", "維運設定", "QA"}
LABELS = {"P0": "必須立即處理", "P1": "合併前必修", "P2": "近期排程修正", "P3": "可後續改善"}
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
    version2 = manifest.get("schema") == CONTEXT
    require(isinstance(draft, dict) and draft.get("schema") == (RESULT if version2 else LEGACY_RESULT), "workspace result schema is invalid")
    require(isinstance(manifest, dict) and manifest.get("schema") in (CONTEXT, LEGACY_CONTEXT), "workspace context schema is invalid")
    require(manifest.get("scope") in ("repo", "group") and manifest.get("target") in ("branch", "staged", "uncommitted", "quick"),
            "invalid review scope/target")
    if manifest.get("megin_binding"):
        from delivery_binding import verify_manifest_binding
        verify_manifest_binding(manifest)
    extension = api._workspace_extension()
    if manifest.get("mr_binding"):
        require(extension is not None and callable(getattr(extension, "verify_manifest_binding", None)),
                "MR binding requires its verification adapter")
        extension.verify_manifest_binding(manifest)
    require(isinstance(draft.get("summary"), str) and draft["summary"].strip(), "summary is required")
    for field in ("coverage", "impact_coverage", "repository_coverage", "findings", "limitations",
                  "next_steps", "tests_executed", "tests_not_executed"):
        require(isinstance(draft.get(field), list), f"{field} must be an array")
    for field in ("limitations", "tests_executed", "tests_not_executed"):
        require(all(isinstance(s, str) for s in draft[field]), f"{field} must contain text")
    require(1 <= len(draft["next_steps"]) <= 3, "next_steps needs 1-3 actions")
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
    if version2:
        contract_manifest = {**manifest, "changed_contracts": []}
        for contract in manifest.get("changed_contracts", []):
            validated = validate_contract(context, contract_manifest, contract, api)
            require(contract == validated, "registered contract anchor bytes changed")
            contracts[contract["id"]] = contract
            contract_manifest["changed_contracts"].append(contract)
    verified = set()
    content_cache = {}

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
        if key not in content_cache:
            content_cache[key] = fixed_bytes(context, repo, name, path, api)
        raw = content_cache[key]
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
    if version2 and manifest["scope"] == "group":
        workspace_file = Path(manifest["workspace_file"]) if manifest.get("workspace_file") else None
        try:
            if workspace_file is not None:
                require(hashlib.sha256(workspace_file.read_bytes()).hexdigest() == manifest.get("workspace_file_sha256"),
                        "declared workspace folders changed after capture")
            current = set(api.discover_repositories(Path(manifest["workspace"]), workspace_file))
            captured = {Path(repo["path"]).resolve() for repo in repos.values()}
            require(current == captured, "Group repository inventory changed after capture; review again")
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
        if version2:
            ids = node.get("contract_ids", [])
            require(isinstance(ids, list) and all(i in contracts for i in ids), "impact contracts must be registered")
            if manifest["scope"] == "group":
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
            ids = row.get("impact_ids")
            require(isinstance(ids, list) and ids and all(i in nodes for i in ids), "each reviewed change needs impact nodes")
            require(all(location(nodes[i]) in matches for i in ids), "change's impact roots must cover its own frozen evidence")
            root_nodes.update(ids)
            reviewed_anchors.update(matches)
            if version2 and manifest["scope"] == "group":
                applicable = {c["id"] for c in contracts.values() if location(c["root"]) == key}
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
    if version2 and manifest["scope"] == "group":
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
            if version2:
                identifier = record.get("contract_id")
                if manifest["scope"] == "group" or identifier is not None:
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
        if expected and version2 and manifest["scope"] == "group" and state != "unresolved":
            source_names = ("index", "working-tree") if manifest["target"] == "quick" else (repo["review_source"],)
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
                    if manifest["target"] == "quick" and state == "related" and any(s.get("matches") for s in bound):
                        inspected = any(n["repo"] == repo_id and n["source"] == source_name and
                                        contract_id in n.get("contract_ids", []) and n["status"] == "reviewed"
                                        for n in nodes.values())
                        if not inspected:
                            incomplete = True
                            gaps.append(f"{repo_id}/{source_name}/{contract_id}: 已找到相關來源但尚未完成此版本的影響審查。")
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
            fields = ("head", "branch") if manifest["target"] == "branch" else (("head", "index_digest")
                     if manifest["target"] == "staged" else ("head", "index_digest", "working_tree_digest", "dirty_submodules"))
            if any(now[f] != repo["before"][f] for f in fields):
                incomplete = True
                gaps.append(f"{repo_id}: 工作區版本於擷取後改變，請重做審查。")
        dispositions[repo_id] = row
    require(set(dispositions) == set(repos), "every candidate Repo needs an explicit disposition")

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
    counts = {p: sum(f["priority"] == p for f in draft["findings"]) for p in PRIORITIES}
    state = "審查未完成" if incomplete else ("沒有差異" if not expected else
            ("發現具體問題" if draft["findings"] else "未發現具體問題"))
    recommendation = "需補做審查" if incomplete else ("暫緩合併" if counts["P0"] else
                     ("修正後再合併" if counts["P1"] else "可以合併"))
    if manifest["target"] != "branch":
        recommendation = {"暫緩合併": "暫緩提交", "修正後再合併": "修正後再提交", "可以合併": "可以提交"}.get(recommendation, recommendation)
    return {**draft, "review_status": state, "review_complete": not incomplete,
            "recommendation": recommendation, "priority_counts": counts,
            "limitations": list(dict.fromkeys(gaps)), "scope": manifest["scope"], "target": manifest["target"],
            "involved_repos": sorted(involved), "versions": [{"repo": r["id"], "path": r["path"],
                 "sources": {k: v["ref"] for k, v in r["sources"].items()}} for r in repos.values()],
            "searches": manifest["searches"],
            "changed_contracts": manifest.get("changed_contracts", []),
            "coverage_schema": "group-v2" if version2 and manifest["scope"] == "group" else "historical" if not version2 else "repo-v2",
            **({"megin_binding": manifest["megin_binding"]} if manifest.get("megin_binding") else {}),
            **({"mr_binding": manifest["mr_binding"]} if manifest.get("mr_binding") else {}),
            "context_sha256": hashlib.sha256((context / "manifest.json").read_bytes()).hexdigest()}


def render(result, manifest, repo_id=None):
    title = "功能鏈審查" if repo_id is None else repository(manifest, repo_id)["name"]
    findings = result["findings"] if repo_id is None else [f for f in result["findings"] if any(e["repo"] == repo_id for e in f["evidence"])]
    lines = [f"# Merge Review: {title}", "", "## 審查結論", "",
             f"- 建議：{result['recommendation']}", f"- 審查狀態：{result['review_status']}",
             f"- 摘要：{result['summary']}",
             "- 整體問題數量：" + "／".join(f"{p} {LABELS[p]} {result['priority_counts'][p]}" for p in PRIORITIES),
             "- 下一步：" + "；".join(f"{s['action']}（{s['owner']}）" for s in result["next_steps"]),
             "- 缺口：" + ("；".join(result["limitations"]) or "無"), "", "## 問題總覽", "",
             "| 編號 | 優先程度 | 問題 | 影響 | 負責角色 |", "| --- | --- | --- | --- | --- |"]
    def cell(s):
        return str(s).replace("|", "\\|").replace("\n", " ")
    for f in findings:
        lines.append(f"| {f['id']} | {f['priority']} {LABELS[f['priority']]} | {cell(f['title'])} | {cell(f['impact'])} | {f['owner']} |")
    if not findings:
        lines.append("| — | — | 未建立有證據的問題 | — | — |")
    lines.extend(["", "## 問題詳情", ""])
    for f in findings:
        lines.extend([f"### {f['id']}｜{f['priority']} {LABELS[f['priority']]}｜{f['title']}", "",
                      f"**影響**：{f['impact']}", "", f"**觸發情境**：{f['trigger']}",
                      f"**預期**：{f['expected']}", f"**實際**：{f['actual']}",
                      f"**建議處理**：{f['recommendation']}（{f['owner']}）", "",
                      f"**技術證據**（{f['scenario_source']}）："])
        for e in f["evidence"]:
            lines.append(f"- `{e['repo']}`／`{e['source']}`／`{e['ref']}`／`{e['path']}`：{e.get('line_start', '—')}–{e.get('line_end', '—')}")
        if f.get("evidence_summary"):
            lines.append(f"- 證據說明：{f['evidence_summary']}")
        if f.get("engineering_fix"):
            lines.append(f"- 工程修正建議：{f['engineering_fix']}")
        lines.append("")
    lines.extend(["## 功能鏈與 Repo 覆蓋", "", "| Repo | 判定 | 原因 |", "| --- | --- | --- |"])
    for row in result["repository_coverage"]:
        r = repository(manifest, row["repo"])
        if repo_id is None or row["repo"] == repo_id:
            lines.append(f"| {cell(r['name'])} ({r['id']}) | {row['status']} | {cell(row['reason'])} |")
    for node in result["impact_coverage"]:
        if repo_id is None or node["repo"] == repo_id:
            lines.append(f"- `{node['id']}` `{node['repo']}/{node['source']}/{node['path']}`：{node['status']}；{node['reason']}；關聯 {', '.join(node['related_to']) or '無後續關聯'}")
            for e in node.get("evidence", []):
                lines.append(f"  - 證據：`{e['repo']}/{e['source']}/{e['path']}`；`{e['ref']}`；{e.get('line_start', '—')}–{e.get('line_end', '—')}")
    lines.extend(["", "## 範圍與限制", "", f"- 環境：{manifest['scope']}；目標：{manifest['target']}",
                  "- 已執行檢查：" + ("；".join(result["tests_executed"]) or "無"),
                  "- 未執行檢查：" + ("；".join(result["tests_not_executed"]) or "無"),
                  "", "## 技術審查紀錄（工程師參考）", ""])
    for version in result["versions"]:
        if repo_id is None or version["repo"] == repo_id:
            lines.append(f"- `{version['repo']}`：`{version['path']}`")
            lines.extend(f"  - `{source}`：`{ref}`" for source, ref in version["sources"].items())
            native = repository(manifest, version["repo"]).get("native")
            if native:
                lines.extend([f"  - 輸入分支／版本：`{native['base_input']}` → `{native['head_input']}`；模式 `{native['mode']}`",
                              f"  - 共同起點：`{native['merge_base']}`；同步遠端：`{native['fetch_status']}`；提交數 {native['commit_count']}"])
                lines.extend(f"  - 合併提交：`{merge['sha']}`；parents {', '.join(merge['parents'])}" for merge in native["merge_commits"])
    lines.extend(["", "### 逐檔覆蓋", ""])
    for row in result["coverage"]:
        if repo_id is None or row["repo"] == repo_id:
            lines.append(f"- `{row['repo']}/{row['source']}/{row['path']}`：{row['status']}" +
                         (f"；{row['reason']}" if row.get("reason") else ""))
    lines.extend(["", "### 固定版本搜尋紀錄", ""])
    for record in result["searches"]:
        if repo_id is None or record["repo"] == repo_id:
            lines.append(f"- `{record['id']}` `{record['repo']}/{record['source']}` `{record.get('ref', 'unavailable')}`：契約 `{record.get('contract_id', 'historical')}`；{record['query']!r}；{record['status']}；{len(record['matches'])} 筆")
    lines.extend(["", "### 變更與延伸契約", ""])
    for contract in result.get("changed_contracts", []):
        lines.append(f"- `{contract['id']}` `{contract['kind']}` `{contract['symbol']}`；變更起點 `{contract['root']['repo']}/{contract['root']['source']}/{contract['root']['path']}`；上游 {', '.join(contract['parent_ids']) or '直接變更'}")
        lines.extend(f"  - 搜尋 {term['query']!r}：{term['reason']}" for term in contract["search_terms"])
    if result.get("megin_binding"):
        binding = result["megin_binding"]
        receipt = binding["receipt"]
        lines.extend(["", "### Megin 交付來源", "",
                      f"- 工作：`{receipt['work_id']}`；計畫：`{receipt['plan_version']}`；收據：`{receipt['receipt_sha256']}`",
                      "- 此收據固定交付來源；本次跨 Repo 審查仍由以上變更、契約與逐 Repo 覆蓋決定。"])
    return "\n".join(lines) + "\n"


def publish(context, draft, manifest, report_dir, include_json, *, timeout=180.0):
    result = validate(draft, manifest, context, timeout=timeout)
    import git_review_context as api
    extension = api._workspace_extension()
    seed = repository(manifest, manifest["seed_repos"][0]) if manifest["seed_repos"] else None
    if report_dir is None:
        root = Path(seed["path"]) if manifest["scope"] == "repo" else Path(manifest["workspace"])
        report_dir = root / "review-reports"
    report_dir = Path(report_dir).expanduser().resolve()
    require(not report_dir.is_relative_to(context.resolve()), "report directory cannot be inside owned temporary context")
    report_dir.mkdir(parents=True, exist_ok=True)
    for suffix in range(1000):
        name = "merge-review-" + manifest["run_id"] + (f"-{suffix:02}" if suffix else "")
        output = report_dir / name
        if output.exists():
            continue
        # Entire report directory is published at once; failure leaves no partial reports.
        with tempfile.TemporaryDirectory(prefix=".merge-review-", dir=report_dir) as temporary:
            stage = Path(temporary) / "report"
            stage.mkdir()
            markdown = render(result, manifest)
            if extension and callable(getattr(extension, "adapt_report", None)):
                markdown = extension.adapt_report(markdown, manifest, result)
            (stage / "summary.md").write_text(markdown, encoding="utf-8")
            for repo_id in result["involved_repos"]:
                (stage / f"{repo_id}.md").write_text(render(result, manifest, repo_id), encoding="utf-8")
            if include_json:
                (stage / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            # Recheck receipt/MR bindings after rendering, immediately before publication.
            if manifest.get("megin_binding"):
                from delivery_binding import verify_manifest_binding
                verify_manifest_binding(manifest)
            if manifest.get("mr_binding"):
                extension.verify_manifest_binding(manifest)
            try:
                os.rename(stage, output)
            except FileExistsError:
                continue
        return output / "summary.md", output / "result.json" if include_json else None
    raise ValueError("report filename collisions exceeded limit")
