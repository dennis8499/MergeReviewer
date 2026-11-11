"""Compare-and-save compact authoring; semantic judgments remain agent supplied."""
from __future__ import annotations

import copy
import re

import review_coordinator as coordination

ROW_KEYS = {"coverage": "change_id", "boundary_reviews": "change_id", "merge_parent_review": "id",
            "repository_coverage": "repo", "impact_coverage": "id", "findings": "id"}
TEXT_LISTS = {"pending_confirmations", "next_steps", "tests_executed", "tests_not_executed", "limitations", "scope_notes"}


def apply(context, manifest, operation):
    from review_flow import diagnostics, now
    from review_workspace import require, safe_path

    require(set(operation) <= {"id", "op", "expected_draft_sha256", "updates", "checkpoint"}, "unknown author argument")
    expected = operation.get("expected_draft_sha256")
    require(isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected), "author needs expected_draft_sha256")
    identifier = operation["id"]
    draft_path = safe_path(context, "draft.json")
    journal_path = safe_path(context, "author-" + identifier + ".json")
    fingerprint = coordination.digest(operation)
    if journal_path.exists():
        journal = coordination.read_json(journal_path)
        require(journal["fingerprint"] == fingerprint, "operation id already has different input")
        current = coordination.file_digest(draft_path)
        require(current in (journal["before"], journal["after"]), "draft changed since author transaction")
        if current == journal["before"]:
            coordination.atomic(draft_path, journal["draft"])
        return journal["result"]

    require(coordination.file_digest(draft_path) == expected, "draft hash conflict; read current draft before authoring")
    draft = coordination.read_json(draft_path)
    require(draft.get("schema") == "merge-reviewer-authoring/v1", "author requires a compact draft")
    updates = operation.get("updates")
    require(isinstance(updates, dict) and set(updates) <= set(ROW_KEYS) | TEXT_LISTS | {"summary"}, "unsupported author updates")
    candidate = copy.deepcopy(draft)
    for field, values in updates.items():
        if field in ROW_KEYS:
            key = ROW_KEYS[field]
            require(isinstance(values, list) and all(isinstance(row, dict) and isinstance(row.get(key), str)
                    and row[key] for row in values), field + " needs rows with " + key)
            require(len({row[key] for row in values}) == len(values), "duplicate author row ID")
            rows = {row[key]: row for row in candidate[field]}
            for row in values:
                rows[row[key]] = {**rows.get(row[key], {}), **row}
            candidate[field] = list(rows.values())
        elif field == "summary":
            require(isinstance(values, str), "summary must be text")
            candidate[field] = values
        else:
            require(isinstance(values, list), field + " must be an array")
            candidate[field] = values
    checkpoint = operation.get("checkpoint", {})
    require(isinstance(checkpoint, dict) and set(checkpoint) <= {
        "completed_operation_ids", "unread_evidence", "open_questions", "next_action"}, "invalid checkpoint")
    for field in ("completed_operation_ids", "unread_evidence", "open_questions"):
        values = checkpoint.get(field, [])
        require(isinstance(values, list) and len(values) <= 1000 and all(
            isinstance(v, str) and 0 < len(v) <= 2000 for v in values), "invalid checkpoint " + field)
    require(all(value in manifest.get("operations", {}) for value in checkpoint.get("completed_operation_ids", [])),
            "checkpoint names an unknown completed operation")
    require(isinstance(checkpoint.get("next_action", ""), str) and len(checkpoint.get("next_action", "")) <= 4000,
            "invalid checkpoint next_action")
    inspected = diagnostics(candidate, manifest, context)
    require(not inspected.get("errors"), "invalid author updates: " + str(inspected.get("errors")))
    # Journal first: a process interruption can finish this exact save on retry.
    # Both draft and checkpoint are committed by the same durable journal record.
    import json
    import hashlib
    import os
    # Match git_network_lease._atomic_json, including platform newline translation.
    after = hashlib.sha256((json.dumps(candidate, separators=(",", ":")) + os.linesep).encode("utf-8")).hexdigest()
    journal = {"fingerprint": fingerprint, "before": expected, "draft": candidate,
               "checkpoint": checkpoint, "at": now()}
    result = {"saved": True, "draft_sha256": after, "updated_fields": list(updates),
              "diagnostics": inspected, "checkpoint": {**checkpoint, "request_id": context.name,
              "draft_sha256": after, "source_identity": coordination.digest([
                  {"id": row["id"], "sources": row.get("sources", {})} for row in manifest["repositories"]]),
              "at": journal["at"]}}
    coordination.atomic(journal_path, {**journal, "after": after, "result": result})
    coordination.atomic(draft_path, candidate)
    return result
