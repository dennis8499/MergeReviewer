"""Cheap authoring diagnostics from saved JSON; no Git, source reads or semantic pass."""
from __future__ import annotations

import re
import copy
import contract_boundary as boundary

FIELDS = ("operation_scenario", "technical_trigger", "technical_explanation", "impact_scope", "engineering_fix")


def inspect(draft, manifest):
    errors, gaps = [], []

    def add(path, message):
        tokens = re.findall(r"[^.\[\]]+", path)
        if tokens and tokens[0] == "draft":
            tokens.pop(0)
        pointer = "/" + "/".join(token.replace("~", "~0").replace("/", "~1") for token in tokens) if tokens else ""
        # Messages have stable leading clauses; IDs and source values follow ':'.
        clause = message.split(":", 1)[0]
        code = "authoring." + re.sub(r"[^a-z0-9]+", "-", clause.lower()).strip("-")
        errors.append({"code": code, "pointer": pointer, "path": path, "message": message})

    def need(condition, path, message):
        if not condition:
            add(path, message)
        return bool(condition)

    def text(value, path):
        return need(isinstance(value, str) and value.strip(), path, "nonempty text is required")

    def array(obj, key, path="draft"):
        value = obj.get(key, [])
        if not isinstance(value, list):
            add(path + "." + key, "must be an array")
            return []
        return value

    def rows(obj, key, path="draft"):
        found = []
        for index, row in enumerate(array(obj, key, path)):
            if need(isinstance(row, dict), f"{path}.{key}[{index}]", "must be an object"):
                for name in ("id", "repo", "source", "path", "contract_id", "status", "outcome", "symbol", "search_scope"):
                    if name in row and not isinstance(row[name], str):
                        add(f"{path}.{key}[{index}].{name}", "must be a string")
                        row[name] = None
                for name in ("related_to", "contract_ids", "impact_ids", "search_ids", "boundary_contract_ids"):
                    if name not in row:
                        continue
                    value = row[name]
                    if not isinstance(value, list):
                        add(f"{path}.{key}[{index}].{name}", "must be an array")
                        row[name] = []
                    else:
                        clean = []
                        for offset, identifier in enumerate(value):
                            if need(isinstance(identifier, str), f"{path}.{key}[{index}].{name}[{offset}]", "must be a string"):
                                clean.append(identifier)
                        row[name] = clean
                found.append(row)
        return found

    if not isinstance(draft, dict):
        add("draft", "must be an object")
        return {"errors": errors, "gaps": [], "ready_for_finish": False,
                "evidence_verified": False, "evidence_validation": "deferred-to-finish", "git_commands": 0}
    # Normalize invalid authoring shapes only in this disposable copy. This lets
    # independent diagnostics continue after an unhashable ID or malformed list.
    draft = copy.deepcopy(draft)
    need(draft.get("schema") == "merge-reviewer-workspace-result/v3", "draft.schema", "workspace result v3 is required")
    need(draft.get("report_layout") == "engineering-review/v1", "draft.report_layout", "engineering-review/v1 is required")
    need(isinstance(draft.get("summary"), str) and draft["summary"].strip(), "draft.summary", "summary is required")
    for field in ("coverage", "impact_coverage", "repository_coverage", "findings", "merge_parent_review",
                  "next_steps", "tests_executed", "tests_not_executed", "limitations"):
        need(isinstance(draft.get(field), list), "draft." + field, "must be an array")
    need(manifest.get("contract_scope_policy") in (None, boundary.POLICY, boundary.V2_POLICY), "manifest.contract_scope_policy", "unsupported policy")
    repos = {row["id"]: row for row in manifest.get("repositories", [])}
    contracts = {row["id"]: row for row in manifest.get("changed_contracts", [])}
    searches = {row["id"]: row for row in manifest.get("searches", [])}
    nodes = {row.get("id"): row for row in rows(draft, "impact_coverage")}
    need(len(nodes) == len(rows(draft, "impact_coverage")), "draft.impact_coverage", "impact IDs must be unique")
    graph = {identifier: set() for identifier in nodes}
    expected = {(row["repo"], row["source"], row["path"]) for row in manifest.get("required_coverage", [])}
    change_ids = {(row["root"]["repo"], row["root"]["source"], row["root"]["path"]): row["id"]
                  for row in boundary.changes(manifest)}
    outcomes = {row.get("change_id"): row.get("outcome") for row in rows(draft, "boundary_reviews")}
    seen = set()

    def row_path(field, row):
        return "draft." + field + "[" + str(next(index for index, item in enumerate(draft[field]) if item is row)) + "]"

    def evidence(items, path, *, indexed=True):
        keys = []
        if not isinstance(items, list):
            add(path, "evidence must be an array")
            return keys
        for index, item in enumerate(items):
            place = f"{path}[{index}]" if indexed else path
            if not need(isinstance(item, dict), place, "evidence must be an object"):
                continue
            if not all(need(isinstance(item.get(name), str), place + "." + name, "must be a string")
                       for name in ("repo", "source", "path", "ref")):
                continue
            repo = repos.get(item.get("repo"), {})
            source = repo.get("sources", {}).get(item.get("source"), {})
            fixed = need(bool(source) and item.get("ref") == source.get("ref"), place,
                         "evidence needs its fixed Repo/source/ref")
            blob = need(any(entry.get("path") == item.get("path") and entry.get("kind") == "blob"
                            for entry in source.get("files", [])), place,
                        "evidence path is not a captured blob")
            start, end = item.get("line_start"), item.get("line_end")
            interval = need(start is None and end is None or type(start) is int and type(end) is int and 1 <= start <= end,
                            place, "invalid line interval; upper bound is checked at finish")
            if fixed and blob and interval:
                keys.append((item.get("repo"), item.get("source"), item.get("path")))
        return keys

    for row in rows(draft, "coverage"):
        place = row_path("coverage", row)
        key = (row.get("repo"), row.get("source"), row.get("path"))
        need(key in expected and key not in seen, place, "coverage is duplicated or outside required changes")
        seen.add(key)
        need(row.get("status") in ("reviewed", "metadata-only", "not-reviewed"), place + ".status", "invalid coverage status")
        if row.get("status") != "reviewed":
            text(row.get("reason"), place + ".reason")
            gaps.append({"kind": "change", "id": list(key), "reason": row.get("reason", "not reviewed")})
        else:
            need(bool(row.get("evidence")), place + ".evidence", "reviewed change needs fixed evidence")
            evidence(row.get("evidence", []), place + ".evidence")
            if (manifest.get("extension_enabled") and
                    (manifest.get("contract_scope_policy") != boundary.V2_POLICY or
                     outcomes.get(change_ids.get(key)) == "propagates")):
                need(bool(row.get("impact_ids")) and all(i in nodes for i in row.get("impact_ids", [])),
                     place + ".impact_ids", "reviewed Group change needs impact anchors")
                need(any(contract.get("root") == {"repo": key[0], "source": key[1], "path": key[2]}
                         and not contract.get("parent_ids") for contract in contracts.values()),
                     place, "reviewed Group change needs an initial anchored contract")
    for key in expected - seen:
        add("draft.coverage", "missing required change: " + repr(key))
    for identifier, node in nodes.items():
        place = row_path("impact_coverage", node)
        need(bool(identifier), place + ".id", "impact id is required")
        need(node.get("status") in ("reviewed", "not-reviewed"), place + ".status", "invalid impact status")
        text(node.get("reason"), place + ".reason")
        if node.get("status") != "reviewed":
            gaps.append({"kind": "impact", "id": identifier, "reason": node.get("reason", "not reviewed")})
        keys = evidence(node.get("evidence", []), place + ".evidence")
        if node.get("status") == "reviewed":
            need(bool(node.get("evidence")), place + ".evidence", "reviewed impact needs fixed evidence")
            need((node.get("repo"), node.get("source"), node.get("path")) in keys,
                 place + ".evidence", "impact evidence must cover its own fixed path/source")
        if manifest.get("extension_enabled"):
            need(bool(node.get("contract_ids")), place + ".contract_ids", "Group impact needs registered contracts")
        need(all(i in contracts for i in node.get("contract_ids", [])), place + ".contract_ids", "unknown contract")
        for neighbor in node.get("related_to", []):
            if need(neighbor in nodes and neighbor != identifier, place + ".related_to", "unknown/self impact edge"):
                graph[identifier].add(neighbor)
                graph[neighbor].add(identifier)
    roots = {identifier for row in rows(draft, "coverage") for identifier in row.get("impact_ids", []) if identifier in nodes}
    reached, pending = set(), list(roots)
    while pending:
        current = pending.pop()
        if current not in reached:
            reached.add(current)
            pending.extend(graph[current] - reached)
    need(set(nodes) == reached, "draft.impact_coverage", "impact graph has disconnected nodes")
    for identifier, contract in contracts.items():
        contract_place = "manifest.changed_contracts[" + str(next(i for i, item in enumerate(manifest["changed_contracts"]) if item is contract)) + "]"
        need(isinstance(boundary.scope(manifest, contract), str) and boundary.scope(manifest, contract) in boundary.SCOPES,
             contract_place + ".search_scope", "invalid search_scope")
        evidence([contract.get("evidence", {})], contract_place + ".evidence", indexed=False)
        need(any(identifier in node.get("contract_ids", []) for node in nodes.values()),
             "draft.impact_coverage", "contract has no impact anchor: " + identifier)
    # Run the same policy validator once per local contract to collect independent
    # boundary errors together. Descendants remain available for propagation checks.
    reviews = rows(draft, "boundary_reviews")
    if manifest.get("contract_scope_policy") == boundary.V2_POLICY:
        required_changes = boundary.changes(manifest) if manifest.get("extension_enabled") else []
        for change in required_changes:
            identifier = change["id"]
            selected_reviews = [row for row in reviews if row.get("change_id") == identifier]
            place = row_path("boundary_reviews", selected_reviews[0]) if selected_reviews else "draft.boundary_reviews"
            def strict(condition, message):
                if not condition:
                    raise ValueError(message)
            def cite(row, required=True):
                values = row.get("evidence", [])
                strict(isinstance(values, list) and (values or not required), "fixed boundary evidence is required")
                return evidence(values, place + ".evidence")
            try:
                _, missing = boundary.validate({**draft, "boundary_reviews": selected_reviews}, manifest, contracts,
                                               nodes, graph, searches, cite, strict, only={identifier})
                gaps.extend({"kind": "boundary", "id": identifier, "reason": reason} for reason in missing)
            except (ValueError, KeyError, TypeError) as exc:
                add(place, str(exc))
        need(len(reviews) == len(required_changes), "draft.boundary_reviews", "every change needs one boundary review")
    elif manifest.get("contract_scope_policy") == boundary.POLICY:
        local = {i: c for i, c in contracts.items() if boundary.scope(manifest, c) == "repo-local"}
        for identifier in local:
            selected_reviews = [row for row in reviews if row.get("contract_id") == identifier]
            review_place = row_path("boundary_reviews", selected_reviews[0]) if selected_reviews else "draft.boundary_reviews"
            def strict(condition, message):
                if not condition:
                    raise ValueError(message)
            def cite(row, required=True):
                values = row.get("evidence", [])
                strict(isinstance(values, list) and (values or not required), "fixed boundary evidence is required")
                return evidence(values, review_place + ".evidence")
            try:
                _, missing = boundary.validate({"boundary_reviews": selected_reviews}, manifest, contracts,
                                               nodes, graph, searches, cite, strict, only={identifier})
                gaps.extend({"kind": "boundary", "id": identifier, "reason": reason} for reason in missing)
            except (ValueError, KeyError, TypeError) as exc:
                add(review_place, str(exc))
        need(all(row.get("contract_id") in local for row in reviews), "draft.boundary_reviews", "unknown/nonlocal boundary contract")
    else:
        need(not reviews, "draft.boundary_reviews", "boundary reviews require boundary-contracts/v1")
    dispositions = rows(draft, "repository_coverage")
    need(len(dispositions) == len(repos) and {row.get("repo") for row in dispositions} == set(repos),
         "draft.repository_coverage", "every captured Repo needs one disposition")
    for row in dispositions:
        place = row_path("repository_coverage", row)
        repo_id = row.get("repo")
        ids = row.get("search_ids", [])
        need(row.get("status") in ("seed", "related", "not-related", "unresolved"), place + ".status", "invalid Repo status")
        text(row.get("reason"), place + ".reason")
        need(repo_id not in manifest.get("seed_repos", []) or row.get("status") in ("seed", "unresolved"),
             place + ".status", "seed cannot be excluded")
        need(repos.get(repo_id, {}).get("state") != "error" or row.get("status") == "unresolved",
             place + ".status", "failed Repo capture must remain unresolved")
        if row.get("status") == "unresolved":
            gaps.append({"kind": "repo", "id": repo_id, "reason": row.get("reason", "unresolved")})
            continue
        need(isinstance(ids, list) and all(i in searches and searches[i]["repo"] == repo_id for i in ids),
             place + ".search_ids", "invalid search IDs")
        for identifier, contract in boundary.obligations(manifest, contracts, repo_id).items():
            bound = [searches[i] for i in ids if i in searches and searches[i].get("contract_id") == identifier
                     and searches[i].get("status") == "complete" and searches[i].get("source") == repos.get(repo_id, {}).get("review_source")]
            need(bool(bound), place + ".search_ids", "missing contract search: " + identifier)
            if row.get("status") == "not-related":
                need(len({item["query"] for item in bound}) >= 2, place + ".search_ids", "exclusion needs alternate terms: " + identifier)
    required_parents = {item["id"] for item in manifest.get("required_merge_views", [])}
    parents = rows(draft, "merge_parent_review")
    need({row.get("id") for row in parents} == required_parents, "draft.merge_parent_review", "required parent views differ")
    for row in parents:
        need(row.get("status") in ("reviewed", "not-reviewed"), row_path("merge_parent_review", row) + ".status", "invalid parent status")
        if row.get("status") != "reviewed":
            gaps.append({"kind": "parent", "id": row.get("id"), "reason": row.get("reason", "not reviewed")})
        evidence(row.get("evidence", []), row_path("merge_parent_review", row) + ".evidence")
    previous_priority = -1
    for index, finding in enumerate(rows(draft, "findings")):
        place = row_path("findings", finding)
        need(finding.get("id") == f"F-{index + 1:03}", place + ".id", "finding IDs must be sequential")
        priorities = ("P0", "P1", "P2", "P3")
        priority = finding.get("priority")
        if need(priority in priorities, place + ".priority", "invalid finding priority"):
            need(priorities.index(priority) >= previous_priority, place + ".priority", "findings must be ordered P0-P3")
            previous_priority = priorities.index(priority)
        for field in (*FIELDS, "title", "impact", "trigger", "expected", "actual", "recommendation"):
            need(isinstance(finding.get(field), str) and finding[field].strip() and finding[field].strip() != "未提供",
                 place + "." + field, "is required")
        need(finding.get("owner") in ("開發", "維運設定", "QA"), place + ".owner", "invalid owner")
        need(finding.get("scenario_source") in ("code-derived", "reproduced"), place + ".scenario_source", "invalid scenario source")
        need(bool(finding.get("evidence")), place + ".evidence", "finding needs fixed evidence")
        evidence(finding.get("evidence", []), place + ".evidence")
    for row in rows(draft, "next_steps"):
        place = row_path("next_steps", row)
        need(row.get("owner") in ("開發", "維運設定", "QA"), place + ".owner", "invalid owner")
        text(row.get("action"), place + ".action")
    need(len(array(draft, "next_steps")) <= 3, "draft.next_steps", "at most three actions are allowed")
    for field in ("tests_executed", "tests_not_executed", "limitations"):
        for index, value in enumerate(array(draft, field)):
            need(isinstance(value, str), f"draft.{field}[{index}]", "must be a string")
    return {"errors": errors, "gaps": gaps, "ready_for_finish": not errors and not gaps,
            "evidence_verified": False, "evidence_validation": "deferred-to-finish", "git_commands": 0}
