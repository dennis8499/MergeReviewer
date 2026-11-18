"""Installed operation contracts shared by CLI discovery and request preflight."""
from __future__ import annotations

ARGUMENTS = {
    "read": {"repo", "source", "path", "line_start", "line_end", "around_line", "context_lines"},
    "instructions": {"repo", "path"}, "inventory": {"repo", "source"},
    "patch": {"repo", "path", "line_start", "line_end"},
    "register-contract": {"contract"},
    "search": {"query", "repo", "source", "contract", "change_id"},
    "author": {"expected_draft_sha256", "expected_checkpoint_revision", "updates", "checkpoint"},
    "view": {"artifact", "pointer", "offset", "page_size", "string_offset"},
}
REQUIRED = {"read": {"repo", "path"}, "instructions": {"repo"},
            "inventory": set(), "patch": {"repo", "path"}, "register-contract": {"contract"},
            "search": {"query"}, "author": {"expected_draft_sha256", "updates"}, "view": {"artifact"}}
TYPES = {"line_start": "integer", "line_end": "integer", "around_line": "integer",
         "context_lines": "integer", "offset": "integer", "page_size": "integer",
         "string_offset": "integer", "expected_checkpoint_revision": "integer",
         "updates": "object", "checkpoint": "object"}


def author_update_schema():
    """Describe keyed partial updates, with explicit complete-row examples."""
    import review_author_operation as author
    from workspace_report import OWNERS, PRIORITIES

    text = {"type": "string"}
    texts = {"type": "array", "items": text}
    interval = {"line_start": {"type": "integer", "minimum": 1},
                "line_end": {"type": "integer", "minimum": 1}}
    location = {"type": "object", "required": ["repo", "source", "path"], "additionalProperties": False,
                "properties": {key: text for key in ("repo", "source", "path")}}
    compact_citation = {"type": "object", "required": ["read_id", "line_start", "line_end"],
                "additionalProperties": False,
                "properties": {"read_id": text, **interval}}
    full_citation = {"type": "object", "required": ["repo", "source", "ref", "path"],
                     "additionalProperties": False,
                     "dependentRequired": {"line_start": ["line_end"], "line_end": ["line_start"]},
                     "properties": {**location["properties"], "ref": text,
                                    **{key: {**value, "type": ["integer", "null"]} for key, value in interval.items()}},
                     "note": "Empty fixed files omit both line bounds (or use null); nonempty finding evidence still needs actual read bounds."}
    citation = {"oneOf": [compact_citation, full_citation]}
    evidence = {"type": "array", "items": citation}
    properties = {
        "coverage": {"change_id": text, "status": {"enum": ["reviewed", "metadata-only", "not-reviewed"]},
                     "reason": text, "evidence": evidence, "impact_ids": texts},
        "boundary_reviews": {"change_id": text, "outcome": {"enum": ["contained", "propagates", "unresolved"]},
                             "reason": text, "evidence": evidence, "search_ids": texts, "boundary_contract_ids": texts},
        "repository_coverage": {"repo": text, "status": {"enum": ["seed", "related", "not-related", "unresolved"]},
                                "reason": text, "search_ids": texts},
        "merge_parent_review": {"id": text, "status": {"enum": ["reviewed", "not-reviewed"]},
                                 "reason": text, "evidence": evidence, "final_ref": text},
        "impact_coverage": {"id": text, "repo": text, "source": text, "path": text,
                             "status": {"enum": ["reviewed", "not-reviewed"]}, "reason": text,
                             "evidence": evidence, "related_to": texts, "contract_ids": texts},
        "findings": {**{key: text for key in ("id", "title", "scenario", "trigger", "explanation", "expected", "actual", "impact", "fix",
                       "operation_scenario", "technical_trigger", "technical_explanation", "impact_scope",
                       "evidence_summary", "engineering_fix", "recommendation")},
                     "priority": {"enum": list(PRIORITIES)}, "owner": {"enum": sorted(OWNERS)},
                     "scenario_source": {"enum": ["code-derived", "reproduced"]}, "evidence": evidence},
    }
    updates = {field: {"type": "array", "merge_key": key, "items": {"type": "object", "required": [key],
               "properties": properties[field]}} for field, key in author.ROW_KEYS.items()}
    updates.update({field: texts for field in author.TEXT_LISTS})
    updates["impact_coverage"]["description"] = "Cross-Repo graph only when extension_enabled=true. Keep [] for a single Repo; local supporting reads belong in coverage/finding evidence."
    updates["next_steps"] = {"type": "array", "items": {"type": "object", "required": ["action", "owner"],
        "properties": {"action": text, "owner": {"enum": sorted(OWNERS)}}}}
    updates["pending_confirmations"] = {"type": "array", "items": {"type": "object",
        "required": ["id", "title", "reason", "missing_evidence", "verification", "owner", "related_changes"],
        "properties": {**{key: text for key in ("id", "title", "reason", "missing_evidence", "verification")},
                       "owner": {"enum": sorted(OWNERS)},
                       "related_changes": {"type": "array", "items": {"oneOf": [text, location]}}, "evidence": evidence}}}
    updates["summary"] = text
    return {"type": "object", "additionalProperties": False, "properties": updates,
            "note": "Keyed rows merge into existing rows; new rows still require complete semantic validation. Lists replace the saved list. Prefer compact findings (scenario, explanation, fix); expanded fields remain accepted but must agree. Citations use completed read IDs or full fixed-source anchors and actual returned line ranges."}


def describe(operation=None):
    import review_author_operation as author
    if operation is not None and operation not in ARGUMENTS:
        raise ValueError("Unknown operation; choose " + ", ".join(ARGUMENTS))
    examples = {
        "read": {"repo": "repo-000", "source": "head", "path": "app.py", "around_line": 18, "context_lines": 15},
        "instructions": {"repo": "repo-000", "path": "app.py"},
        "inventory": {"repo": "repo-000", "source": "head"},
        "patch": {"repo": "repo-000", "path": "<path from captured patch index>"},
        "register-contract": {"contract": {"id": "C-001", "kind": "behavior", "symbol": "amountCents",
            "root": {"repo": "repo-000", "source": "head", "path": "app.py"}, "parent_ids": [],
            "search_scope": "repo-local", "evidence": {"repo": "repo-000", "source": "head",
                "ref": "<fixed SHA>", "path": "app.py", "line_start": 18, "line_end": 19},
            "search_terms": [{"query": "amountCents", "reason": "Changed field"}]}},
        "search": {"query": "amountCents", "repo": ["repo-000"], "change_id": "<captured change ID>"},
        "author": {"expected_draft_sha256": "<current draft SHA-256>", "updates": {
                       "summary": "<judgment after reading the necessary fixed evidence>",
                       "coverage": [{"change_id": "CH-0001", "status": "reviewed", "reason": "<actual comparison>",
                                     "evidence": [{"read_id": "init-000004", "line_start": 18, "line_end": 19}]}],
                       "tests_not_executed": ["<checks not executed>"]},
                   "checkpoint": {"next_action": "Read the next required chain."}},
        "view": {"artifact": "<saved artifact SHA-256>", "pointer": "/lines", "offset": 0, "page_size": 70},
    }
    contracts = {}
    for kind in ([operation] if operation else ARGUMENTS):
        fields = {}
        for name in sorted(ARGUMENTS[kind]):
            value_type = ("array[string]" if kind == "search" and name == "repo" else
                          "object" if kind == "register-contract" and name == "contract" else TYPES.get(name, "string"))
            fields[name] = {"type": value_type, "required": name in REQUIRED[kind]}
        contract = {"fields": fields, "example": [{"id": kind.replace("-", "_") + "_1", "op": kind, **examples[kind]}]}
        if kind == "author":
            contract.update(update_fields=sorted(set(author.ROW_KEYS) | author.TEXT_LISTS | {"summary"}),
                            update_schema=author_update_schema(),
                            checkpoint_fields=["completed_operation_ids", "unread_evidence", "open_questions", "next_action", "paging"],
                            paging_requires="expected_checkpoint_revision from status, or 0 before first checkpoint")
        if kind == "view":
            contract["note"] = "Use native view --requests-file for paging; this schema preserves the historical step operation."
        contracts[kind] = contract
    return {"schema": "MergeReviewOperationSchema/v1", "operations": contracts,
            "request_shape": "nonempty array, including a single operation",
            "serialization": {"powershell": "ConvertTo-Json -InputObject @($operations) -Depth 100",
                              "python": "json.dumps(operations, ensure_ascii=False); operations must be a list"},
            "validation": "Shape metadata is not fixed-anchor, source, line-range or semantic validation."}
