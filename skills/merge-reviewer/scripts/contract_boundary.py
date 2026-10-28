"""Versioned search obligations; semantic containment remains the reviewer's judgment."""
from __future__ import annotations

POLICY = "boundary-contracts/v1"
V2_POLICY = "boundary-contracts/v2"
SCOPES = {"repo-local", "workspace"}


def enabled(manifest, require):
    policy = manifest.get("contract_scope_policy")
    require(policy in (None, POLICY, V2_POLICY), "unsupported contract scope policy")
    return policy in (POLICY, V2_POLICY)


def scope(manifest, contract):
    # Existing v3 evidence retains its original obligations even if an extra
    # field was authored by an older client.
    return contract.get("search_scope", "workspace") if manifest.get("contract_scope_policy") in (POLICY, V2_POLICY) else "workspace"


def changes(manifest):
    return [{"id": f"CH-{index:04}", "root": row}
            for index, row in enumerate(manifest.get("required_coverage", []), 1)]


def validate_v2(draft, manifest, contracts, nodes, graph, searches, evidence_list, require, *, only=None):
    """Require an explicit, evidence-backed disposition for each changed path."""
    expected = {row["id"]: row["root"] for row in changes(manifest)} if manifest["extension_enabled"] else {}
    require(manifest.get("boundary_changes") == changes(manifest), "boundary change index differs from fixed coverage")
    reviews = draft.get("boundary_reviews", [])
    require(isinstance(reviews, list), "boundary_reviews must be an array")
    found, gaps = {}, []
    coverage = {(row.get("repo"), row.get("source"), row.get("path")): row
                for row in draft.get("coverage", []) if isinstance(row, dict)}
    for review in reviews:
        require(isinstance(review, dict), "boundary review must be an object")
        identifier = review.get("change_id")
        require(identifier in expected and identifier not in found and (only is None or identifier in only),
                "boundary review needs a unique required change")
        root = expected[identifier]
        outcome = review.get("outcome")
        require(outcome in {"contained", "propagates", "unresolved"}, "invalid boundary outcome")
        require(isinstance(review.get("reason"), str) and review["reason"].strip(), "boundary review needs a data-flow reason")
        ids = review.get("search_ids", [])
        require(isinstance(ids, list) and all(isinstance(i, str) for i in ids)
                and len(ids) == len(set(ids)) and all(i in searches for i in ids), "invalid boundary search_ids")
        require(all(searches[i].get("change_id") == identifier and searches[i].get("repo") == root["repo"]
                    and searches[i].get("status") == "complete" for i in ids),
                "boundary searches must be completed searches of this change in its own Repo")
        child_ids = review.get("boundary_contract_ids", [])
        require(isinstance(child_ids, list) and all(isinstance(i, str) for i in child_ids)
                and len(child_ids) == len(set(child_ids)), "boundary_contract_ids must be unique")
        keys = evidence_list(review, outcome != "unresolved")
        require(all(key[0] == root["repo"] for key in keys), "boundary evidence must use its own Repo")
        relevant = {i for i, contract in contracts.items() if contract.get("root") == root
                    and scope(manifest, contract) == "workspace"}
        if outcome == "unresolved":
            require(set(child_ids) <= relevant, "unresolved boundary names unknown workspace contracts")
            gaps.append(identifier + ": " + review["reason"])
        else:
            row = coverage.get((root["repo"], root["source"], root["path"]))
            require(row and row.get("status") == "reviewed", "resolved boundary needs a reviewed change")
            coverage_keys = evidence_list(row)
            require(any(key in coverage_keys and key[0] == root["repo"] and key[2] == root["path"] for key in keys),
                    "boundary evidence must include fixed changed-path evidence")
            if outcome == "contained":
                require(not relevant and not child_ids, "contained boundary cannot hide workspace contracts")
                local = {i for i, contract in contracts.items() if contract.get("root") == root}
                require(all(node["repo"] == root["repo"] for node in nodes.values()
                            if local.intersection(node.get("contract_ids", []))),
                        "contained boundary cannot reach another Repo")
            else:
                require(relevant and set(child_ids) == relevant, "propagation needs every workspace contract")
        found[identifier] = review
    required = set(expected) if only is None else set(expected) & set(only)
    require(set(found) == required, "every required change needs a boundary review")
    return list(found.values()), gaps


def obligations(manifest, contracts, repo_id):
    return {identifier: contract for identifier, contract in contracts.items()
            if scope(manifest, contract) == "workspace" and contract["evidence"]["repo"] != repo_id}


def validate(draft, manifest, contracts, nodes, graph, searches, evidence_list, require, *, only=None):
    """Validate closure evidence after all child contracts have been registered."""
    active = enabled(manifest, require)
    if manifest.get("contract_scope_policy") == V2_POLICY:
        return validate_v2(draft, manifest, contracts, nodes, graph, searches, evidence_list, require, only=only)
    reviews = draft.get("boundary_reviews", [])
    require(isinstance(reviews, list), "boundary_reviews must be an array")
    if not active:
        require(not reviews, "boundary_reviews require boundary-contracts/v1")
        return [], []
    local = {identifier: item for identifier, item in contracts.items()
             if scope(manifest, item) == "repo-local" and (only is None or identifier in only)}
    found, gaps = {}, []
    for review in reviews:
        require(isinstance(review, dict), "boundary review must be an object")
        identifier = review.get("contract_id")
        require(isinstance(identifier, str) and identifier in local and identifier not in found,
                "boundary review needs a unique repo-local contract")
        contract = local[identifier]
        owner = contract["evidence"]["repo"]
        require(isinstance(review.get("outcome"), str) and review["outcome"] in {"contained", "propagates", "unresolved"},
                "invalid boundary outcome")
        require(isinstance(review.get("reason"), str) and review["reason"].strip(), "boundary review needs a data-flow reason")
        ids = review.get("search_ids", [])
        require(isinstance(ids, list) and all(isinstance(i, str) for i in ids)
                and len(ids) == len(set(ids)) and all(i in searches for i in ids),
                "boundary search_ids must identify recorded searches")
        child_ids = review.get("boundary_contract_ids", [])
        require(isinstance(child_ids, list) and all(isinstance(i, str) for i in child_ids)
                and len(child_ids) == len(set(child_ids)), "boundary_contract_ids must be unique")
        outcome = review["outcome"]
        keys = evidence_list(review, outcome != "unresolved")
        require(all(key[0] == owner for key in keys), "boundary evidence must use its own Repo")
        if outcome == "unresolved":
            require(not child_ids, "unresolved boundary cannot claim completed propagation")
            gaps.append(identifier + ": " + review["reason"])
            found[identifier] = review
            continue
        anchor = contract["evidence"]
        require((owner, anchor["source"], anchor["path"]) in keys,
                "boundary evidence must include its local fixed anchor and reviewed use/output context")
        require(any(item.get("repo") == owner and item.get("source") == anchor["source"]
                    and item.get("path") == anchor["path"]
                    and (item.get("line_start") is None or item["line_start"] <= anchor["line_start"]
                         and item["line_end"] >= anchor["line_end"])
                    for item in review.get("evidence", [])), "boundary evidence must cover its registered anchor interval")
        own = [searches[i] for i in ids]
        require(own and all(row["repo"] == owner and row.get("contract_id") == identifier for row in own),
                "boundary searches must belong to its local contract and Repo")
        require(any(row.get("status") == "complete" and row.get("query") == contract["symbol"]
                    and row.get("source") == anchor["source"] for row in own),
                "boundary review needs a completed local exact-symbol search in its anchor source")
        descendants = set()
        pending = [identifier]
        while pending:
            parent = pending.pop()
            for child_id, child in contracts.items():
                if parent in child.get("parent_ids", []) and child_id not in descendants:
                    descendants.add(child_id)
                    pending.append(child_id)
        exported = {i for i in descendants if scope(manifest, contracts[i]) == "workspace"}
        if outcome == "contained":
            require(not child_ids and not exported, "contained boundary cannot hide workspace descendants")
            require(all(node["repo"] == owner for node in nodes.values() if identifier in node.get("contract_ids", [])),
                    "contained local contract cannot reach another Repo")
        else:
            require(child_ids and set(child_ids) <= exported, "propagation needs workspace descendant contracts")
            require(exported <= set(child_ids), "propagation must account for every workspace descendant")
            local_nodes = {i for i, node in nodes.items() if identifier in node.get("contract_ids", [])}
            reached, pending = set(), list(local_nodes)
            while pending:
                current = pending.pop()
                if current not in reached:
                    reached.add(current)
                    pending.extend(graph[current] - reached)
            require(all(any(i in reached and child in node.get("contract_ids", []) for i, node in nodes.items())
                        for child in child_ids), "boundary propagation needs connected child impact anchors")
        found[identifier] = review
    require(set(found) == set(local), "every repo-local contract needs a boundary review")
    return list(found.values()), gaps
