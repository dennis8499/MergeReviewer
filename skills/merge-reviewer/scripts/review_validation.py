"""Pure authoring rules shared by cheap diagnostics and final validation."""
from __future__ import annotations

import hashlib
import review_runtime


def coverage_reason(row):
    """Preserve the v3 optional reviewed reason without coercing invalid JSON."""
    value = row.get("reason")
    if row.get("status") == "reviewed" and value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("coverage reason must be a string")
    if row.get("status") != "reviewed" and not value.strip():
        raise ValueError("unreviewed change needs a reason")
    return value


def completion_preview(draft, manifest, gaps=(), *, errors=(), complete_gaps=False):
    """One outcome policy for JSON-only check and verified publication.

    Callers supply their coverage/graph gaps. Fixed capture limitations and
    authored blocking limitations are always included; notes and unrun tests
    never establish missing evidence. No semantic judgment is inferred here.
    """
    blocking, seen = [], set()

    def add(kind, reason, identifier=None):
        if not isinstance(reason, str):
            return  # Editable shape errors are reported separately.
        key = (kind, reason, review_runtime.canonical(identifier))
        if key not in seen:
            seen.add(key)
            row = {"kind": kind, "reason": reason}
            if identifier is not None:
                row["id"] = identifier
            blocking.append(row)

    for gap in gaps:
        if isinstance(gap, dict):
            add(gap.get("kind", "evidence"), gap.get("reason"), gap.get("id"))
        elif isinstance(gap, str):
            add("evidence", gap)
    if not complete_gaps:
        for reason in manifest.get("context_gaps", []):
            add("capture", reason)
        impacts = draft.get("impact_coverage", [])
        impacts = impacts if isinstance(impacts, list) else []
        involved = set(manifest.get("seed_repos", [])) | {
            row.get("repo") for row in impacts if isinstance(row, dict) and isinstance(row.get("repo"), str)}
        for repo in manifest.get("repositories", []):
            if repo.get("id") not in involved:
                continue
            for reason in repo.get("limitations", []):
                text = f"{repo['id']}: {reason}"
                if text not in manifest.get("context_gaps", []):
                    add("capture", text, repo["id"])
        for reason in draft.get("limitations", []) if isinstance(draft.get("limitations", []), list) else []:
            add("limitation", reason)
    incomplete = bool(blocking)
    pending = draft.get("pending_confirmations", [])
    pending = [row for row in pending if isinstance(row, dict)] if isinstance(pending, list) else []
    for row in pending:
        add("pending", row.get("reason", "待人工確認。"), row.get("id"))
    findings = draft.get("findings", [])
    findings = [row for row in findings if isinstance(row, dict)] if isinstance(findings, list) else []
    counts = {priority: sum(row.get("priority") == priority for row in findings)
              for priority in ("P0", "P1", "P2", "P3")}
    state = "審查未完成" if incomplete else ("待人工確認" if pending else (
        "沒有差異" if not manifest.get("required_coverage") else (
            "發現具體問題" if findings else "未發現具體問題")))
    recommendation = "需補做審查" if incomplete else ("暫緩合併" if counts["P0"] else (
        "修正後再合併" if counts["P1"] else ("需人工確認" if pending else "可以合併")))
    outcome = {"review_complete": not (incomplete or pending), "review_status": state,
               "recommendation": recommendation, "pending_confirmation_count": len(pending),
               "extension_status": ("incomplete" if incomplete else "complete")
                                   if manifest.get("extension_enabled") else "disabled"}
    if errors:
        outcome.update(review_complete=None, review_status="unknown", recommendation="修正草稿後重新檢查")
    return {"expected_outcome": outcome, "blocking_gaps": blocking}


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
