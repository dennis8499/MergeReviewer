"""Pure authoring rules shared by cheap diagnostics and final validation."""
from __future__ import annotations

import hashlib
import review_runtime


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
