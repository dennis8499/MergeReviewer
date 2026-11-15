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
        "author": {"expected_draft_sha256": "<current draft SHA-256>", "updates": {},
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
                            checkpoint_fields=["completed_operation_ids", "unread_evidence", "open_questions", "next_action", "paging"],
                            paging_requires="expected_checkpoint_revision from status, or 0 before first checkpoint")
        if kind == "view":
            contract["note"] = "Use native view --requests-file for paging; this schema preserves the historical step operation."
        contracts[kind] = contract
    return {"schema": "MergeReviewOperationSchema/v1", "operations": contracts,
            "request_shape": "nonempty array, including a single operation",
            "validation": "Shape metadata is not fixed-anchor, source, line-range or semantic validation."}
