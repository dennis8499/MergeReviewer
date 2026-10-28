"""Expand concise authoring into the same v3 validator input, without judgments."""
from __future__ import annotations

import copy

import contract_boundary
import review_runtime

SCHEMA = "merge-reviewer-authoring/v1"
RESULT = "merge-reviewer-workspace-result/v3"


def expand(draft, manifest, context):
    if not isinstance(draft, dict) or draft.get("schema") != SCHEMA:
        return draft
    result = copy.deepcopy(draft)
    result["schema"] = RESULT
    result["report_layout"] = "engineering-review/v1"
    changes = {row["id"]: row["root"] for row in contract_boundary.changes(manifest)}

    def derived(row, field, value):
        if field in row and row[field] != value:
            raise ValueError("compact authoring conflicts with " + field)
        row[field] = value

    def citation(item):
        if not isinstance(item, dict) or "read_id" not in item:
            return item
        if set(item) != {"read_id", "line_start", "line_end"}:
            raise ValueError("compact citation needs read_id, line_start and line_end")
        identifier = item["read_id"]
        checkpoint = manifest.get("operations", {}).get(identifier) if isinstance(identifier, str) else None
        if not isinstance(checkpoint, dict) or checkpoint.get("op") != "read":
            raise ValueError("compact citation needs a completed read operation")
        value = review_runtime.load_view(context, checkpoint["artifact"])
        first, last = item["line_start"], item["line_end"]
        if (type(first) is not int or type(last) is not int or first < 1 or first > last
                or not isinstance(value, dict) or not isinstance(value.get("lines"), list)):
            raise ValueError("compact citation has an invalid line interval")
        available = {line.get("line") for line in value["lines"] if isinstance(line, dict)}
        if last - first + 1 > len(available) or any(line not in available for line in range(first, last + 1)):
            raise ValueError("compact citation lines were not included in its read operation")
        if not all(isinstance(value.get(key), str) and value[key] for key in ("repo", "source", "ref", "path", "sha256")):
            raise ValueError("compact citation read has invalid fixed-source metadata")
        return {**{key: value[key] for key in ("repo", "source", "ref", "path")},
                "line_start": first, "line_end": last}

    for field in ("coverage", "impact_coverage", "boundary_reviews", "merge_parent_review",
                  "findings", "pending_confirmations"):
        rows = result.get(field, [])
        if not isinstance(rows, list):
            continue  # The common validator reports the ordinary shape error.
        for row in rows:
            if not isinstance(row, dict):
                continue
            if field == "coverage" and "change_id" in row:
                identifier = row.pop("change_id")
                if identifier not in changes:
                    raise ValueError("compact coverage names an unknown change_id")
                for key, value in changes[identifier].items():
                    if key in row and row[key] != value:
                        raise ValueError("compact coverage conflicts with its fixed change")
                    row[key] = value
            if isinstance(row.get("evidence"), list):
                row["evidence"] = [citation(item) for item in row["evidence"]]
            if field == "impact_coverage" and row.get("evidence"):
                first = row["evidence"][0]
                if isinstance(first, dict):
                    for key in ("repo", "source", "path"):
                        row.setdefault(key, first.get(key))
            if field == "merge_parent_review" and "final_ref" not in row:
                selected = next(repo for repo in manifest["repositories"] if repo["id"] == manifest["selected_repo"])
                row["final_ref"] = selected["sources"].get("merge-preview", selected["sources"]["head"])["ref"]
            if field == "pending_confirmations" and isinstance(row.get("related_changes"), list):
                row["related_changes"] = [changes[item] if isinstance(item, str) and item in changes else item
                                          for item in row["related_changes"]]
            if field == "findings":
                if "scenario" in row:
                    derived(row, "operation_scenario", row.pop("scenario"))
                if "explanation" in row:
                    derived(row, "technical_explanation", row.pop("explanation"))
                if "trigger" in row:
                    derived(row, "technical_trigger", row["trigger"])
                if "impact" in row:
                    derived(row, "impact_scope", row["impact"])
                if "fix" in row:
                    value = row.pop("fix")
                    derived(row, "recommendation", value)
                    derived(row, "engineering_fix", value)
    return result
