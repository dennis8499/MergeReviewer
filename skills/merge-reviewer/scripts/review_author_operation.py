"""Compare-and-save compact authoring; semantic judgments remain agent supplied."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re

import review_coordinator as coordination
import review_checkpoint

ROW_KEYS = {"coverage": "change_id", "boundary_reviews": "change_id", "merge_parent_review": "id",
            "repository_coverage": "repo", "impact_coverage": "id", "findings": "id"}
TEXT_LISTS = {"pending_confirmations", "next_steps", "tests_executed", "tests_not_executed", "limitations", "scope_notes"}


def draft_digest(draft):
    # Match the bytes written by the existing atomic JSON writer.
    return hashlib.sha256((json.dumps(draft, separators=(",", ":")) + os.linesep).encode("utf-8")).hexdigest()


def draft_candidate(draft, updates):
    """Build the compact draft from the supplied updates."""
    from review_workspace import require

    require(draft.get("schema") == "merge-reviewer-authoring/v1", "author requires a compact draft")
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
    return candidate


def checkpoint_only(operation):
    return (not operation.get("updates") and "checkpoint" in operation
            and operation.get("expected_checkpoint_revision") is not None)


def apply(context, manifest, operation):
    from review_flow import diagnostics, now
    from review_workspace import require, safe_path

    require(set(operation) <= {"id", "op", "expected_draft_sha256", "expected_checkpoint_revision", "updates", "checkpoint"}, "unknown author argument")
    expected = operation.get("expected_draft_sha256")
    require(isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected), "author needs expected_draft_sha256")
    identifier = operation["id"]
    draft_path = safe_path(context, "draft.json")
    journal_path = safe_path(context, "author-" + identifier + ".json")
    fingerprint = coordination.digest(operation)
    current_checkpoint = review_checkpoint.load(context)
    if journal_path.exists():
        journal = coordination.read_json(journal_path)
        require(journal["fingerprint"] == fingerprint, "operation id already has different input")
        finish_checkpoint = review_checkpoint.restore(context, journal, current_checkpoint)
        current = coordination.file_digest(draft_path)
        require(current in (journal["before"], journal["after"]), "draft changed since author transaction")
        if not finish_checkpoint and "checkpoint_before" in journal:
            require(current == journal["after"], "draft changed since committed author transaction")
        if current == journal["before"] and current != journal["after"]:
            coordination.atomic(draft_path, journal["draft"])
        if finish_checkpoint:
            coordination.atomic(review_checkpoint.location(context), journal["result"]["checkpoint"])
        return journal["result"]

    raw_draft = draft_path.read_bytes()
    require(hashlib.sha256(raw_draft).hexdigest() == expected, "draft hash conflict; read current draft before authoring")
    draft = json.loads(raw_draft.decode("utf-8-sig"))
    updates = operation.get("updates")
    candidate = draft_candidate(draft, updates)
    checkpoint = operation.get("checkpoint", {})
    require(isinstance(checkpoint, dict) and set(checkpoint) <= {
        "completed_operation_ids", "unread_evidence", "open_questions", "next_action", "paging"}, "invalid checkpoint")
    expected_revision = operation.get("expected_checkpoint_revision")
    navigation_only = checkpoint_only(operation)
    if expected_revision is not None or "paging" in checkpoint:
        require(type(expected_revision) is int and expected_revision >= 0,
                "paging author needs expected_checkpoint_revision as a nonnegative integer")
        require(expected_revision == review_checkpoint.revision(current_checkpoint),
                "checkpoint revision conflict; read current checkpoint before authoring")
    if "paging" in checkpoint:
        require(isinstance(checkpoint["paging"], dict), "checkpoint paging must be an object")
    for field in ("completed_operation_ids", "unread_evidence", "open_questions"):
        values = checkpoint.get(field, [])
        require(isinstance(values, list) and len(values) <= 1000 and all(
            isinstance(v, str) and 0 < len(v) <= 2000 for v in values), "invalid checkpoint " + field)
    require(all(value in manifest.get("operations", {}) for value in checkpoint.get("completed_operation_ids", [])),
            "checkpoint names an unknown completed operation")
    require(isinstance(checkpoint.get("next_action", ""), str) and len(checkpoint.get("next_action", "")) <= 4000,
            "invalid checkpoint next_action")
    if navigation_only:
        inspected = {}
        after = expected
    else:
        inspected = diagnostics(candidate, manifest, context)
        require(not inspected.get("errors"), "invalid author updates: " + str(inspected.get("errors")))
        after = draft_digest(candidate)
    # Journal first: a process interruption can finish this exact save on retry.
    # Both draft and checkpoint are committed by the same durable journal record.
    at = now()
    saved_checkpoint = review_checkpoint.prepare(context, manifest, checkpoint, current_checkpoint,
                                                 draft_sha256=after, at=at)
    journal = {"fingerprint": fingerprint, "before": expected,
               "checkpoint_before": current_checkpoint, "at": at}
    result = {"saved": True, "draft_sha256": after, "updated_fields": list(updates),
              "draft_changed": after != expected,
              "diagnostics": inspected, "checkpoint": saved_checkpoint}
    if after != expected:
        journal["draft"] = candidate
    coordination.atomic(journal_path, {**journal, "after": after, "result": result})
    if after != expected:
        coordination.atomic(draft_path, candidate)
    coordination.atomic(review_checkpoint.location(context), saved_checkpoint)
    return result
