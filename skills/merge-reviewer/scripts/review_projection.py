"""Transport-only projections: shared text, per-file patches and ready paging input."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re

import review_paging
import review_runtime
from review_views import view_continuation


def prepare(context, projected, raw_artifact, *, scope="context", source_identity=None):
    """Keep the raw artifact intact; all references retain their original sources."""
    context = Path(context)
    if context.parent.name not in ("sessions", "responses") or not re.fullmatch(r"[a-f0-9]{32}", context.name):
        return {}
    value = copy.deepcopy(projected)
    value.update(schema="MergeReviewReadProjection/v1", raw_artifact=raw_artifact)
    contents = value.setdefault("contents", {})
    for operation in value.get("operations", []):
        result = operation.get("result", {})
        lines = result.get("lines") if isinstance(result, dict) else None
        if not isinstance(lines, list):
            continue
        starts = [index for index, row in enumerate(lines) if isinstance(row, dict)
                  and isinstance(row.get("text"), str) and row["text"].startswith("diff --git ")]
        if not starts:
            continue
        if starts[0] != 0:
            starts.insert(0, 0)
        files = []
        for first, last in zip(starts, starts[1:] + [len(lines)]):
            selected = lines[first:last]
            content_id = hashlib.sha256(review_runtime.canonical({"patch_lines": selected})).hexdigest()
            contents.setdefault(content_id, {"lines": selected})
            files.append({"header": selected[0]["text"], "line_start": selected[0]["line"],
                          "line_end": selected[-1]["line"], "content_id": content_id})
        result.pop("lines")
        result["file_index"] = files
    artifact = review_runtime.store_view(context, value)
    links = [view_continuation(context, artifact, "/operations", 0, page_size=32, scope=scope)]
    links.extend(view_continuation(context, artifact, "/contents/" + identifier + "/lines", 0,
                                   page_size=200, scope=scope) for identifier in contents)
    state = review_paging.new_state(context.parent.parent, context.name, links, source_identity=source_identity)
    state_artifact = review_runtime.store_view(context, state)
    return {"paging_requests_file": str(context / "views" / (state_artifact + ".json")),
            "paging_projection": {"artifact": artifact, "raw_artifact": raw_artifact,
                                  "content_count": len(contents), "request_count": len(state["pending"]),
                                  "source_identity": source_identity},
            "paging_instruction": "Use paging_requests_file unchanged for view; acknowledge only received and reviewed pages, then save checkpoint."}
