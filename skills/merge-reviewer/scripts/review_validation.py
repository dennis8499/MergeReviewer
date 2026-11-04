"""Pure authoring rules shared by cheap diagnostics and final validation."""
from __future__ import annotations

import hashlib
import review_runtime


def comparison_left_sources(repo, key):
    """Captured names for the actual left side, including divergent branches."""
    source, path = key[1:]
    baseline = (repo["sources"].get("head", {}).get("ref") if source == "merge-preview"
                else repo.get("diff_base"))
    if not isinstance(baseline, str) or not baseline:
        return set()
    return {name for name, version in repo["sources"].items()
            if version.get("ref") == baseline and any(
                entry["path"] == path and entry["kind"] == "blob" for entry in version.get("files", []))}


def contract_anchor_sources(repo, key):
    """Contract anchors can describe removed symbols in a surviving file.

    Registration still verifies the symbol against removed lines. This broader
    source set must never replace the final-content coverage requirement.
    """
    source, path = key[1:]
    current = repo["sources"].get(source, {})
    right = {source} if any(entry["path"] == path and entry["kind"] == "blob"
                            for entry in current.get("files", [])) else set()
    return right | comparison_left_sources(repo, key)


def contract_has_impact_anchor(contract, nodes):
    anchor = contract.get("evidence", {})
    if not isinstance(anchor, dict):
        return False
    key = tuple(anchor.get(name) for name in ("repo", "source", "path"))
    return all(isinstance(value, str) and value for value in key) and any(
        tuple(node.get(name) for name in ("repo", "source", "path")) == key
        and contract["id"] in node.get("contract_ids", []) for node in nodes)


def coverage_impact_errors(key, matches, identifiers, nodes, contracts, *, needs_contracts):
    roots = [nodes[identifier] for identifier in identifiers if identifier in nodes]
    errors = []
    if any(tuple(node.get(name) for name in ("repo", "source", "path")) not in matches for node in roots):
        errors.append("change's impact roots must cover its own frozen evidence")
    if needs_contracts:
        applicable = {contract["id"] for contract in contracts
                      if isinstance(contract.get("root"), dict)
                      and tuple(contract["root"].get(name) for name in ("repo", "source", "path")) == key
                      and not contract.get("parent_ids")}
        if not applicable:
            errors.append("each reviewed Group change needs an anchored changed contract")
        if not applicable <= {identifier for node in roots for identifier in node.get("contract_ids", [])}:
            errors.append("change impact roots must cover their registered contracts")
    return errors


def fixed_line_count_key(manifest, request):
    repo = next((item for item in manifest.get("repositories", [])
                 if item.get("id") == request.get("repo")), None)
    source_name = request.get("source") or (repo or {}).get("review_source")
    source = (repo or {}).get("sources", {}).get(source_name, {})
    path = request.get("path")
    entry = next((item for item in source.get("files", [])
                  if item.get("path") == path and item.get("kind") == "blob"), None)
    if not entry:
        return None
    identity = {"repo": repo["id"], "source": source_name, "ref": source.get("ref"),
                "path": path, "blob_sha": entry.get("blob_sha")}
    return hashlib.sha256(review_runtime.canonical(identity)).hexdigest()


def repo_disposition_error(repo_id, state, seed_repos, involved):
    if repo_id in seed_repos:
        return None if state in ("seed", "unresolved") else "seed cannot be excluded"
    if repo_id in involved:
        return None if state in ("related", "unresolved") else "reachable Repo cannot be excluded"
    return (None if state in ("not-related", "unresolved")
            else "unrelated Repo cannot be expanded without evidence")


def line_interval_valid(first, last, count=None):
    return (type(first) is int and type(last) is int and 1 <= first <= last
            and (count is None or last <= count))


def evidence_within_involved_repos(keys, involved):
    return all(key[0] in involved for key in keys)


def finding_evidence_errors(keys, reviewed_anchors, involved):
    errors = []
    if not any(key in reviewed_anchors for key in keys):
        errors.append("finding must cite a reviewed seed change in its correct version")
    if not evidence_within_involved_repos(keys, involved):
        errors.append("finding evidence cannot silently expand excluded Repos")
    return errors
