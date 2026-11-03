"""Expand concise authoring into the same v3 validator input, without judgments."""
from __future__ import annotations

import copy

import contract_boundary
import review_runtime

SCHEMA = "merge-reviewer-authoring/v1"
RESULT = "merge-reviewer-workspace-result/v3"


class AuthoringError(ValueError):
    """Independent compact-input errors with exact JSON pointers."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(item["message"] for item in errors))


def expand(draft, manifest, context):
    if not isinstance(draft, dict) or draft.get("schema") != SCHEMA:
        return draft
    result = copy.deepcopy(draft)
    result["schema"] = RESULT
    result["report_layout"] = "engineering-review/v1"
    changes = {row["id"]: row["root"] for row in contract_boundary.changes(manifest)}
    errors = []

    def fail(message, pointer, **details):
        errors.append({"code": "authoring.compact-" + details.pop("kind"),
                       "pointer": pointer, "path": "draft" + pointer,
                       "message": message, **details})

    def derived(row, field, value, pointer):
        if field in row and row[field] != value:
            fail("compact authoring conflicts with " + field, pointer + "/" + field,
                 kind="field-conflict", field=field,
                 hint="Write the compact field once; remove the conflicting expanded field.")
            return
        row[field] = value

    def citation(item, pointer):
        if not isinstance(item, dict) or "read_id" not in item:
            return item
        if set(item) != {"read_id", "line_start", "line_end"}:
            fail("compact citation needs read_id, line_start and line_end", pointer,
                 kind="citation-fields", expected_fields=["read_id", "line_start", "line_end"])
            return item
        identifier = item["read_id"]
        checkpoint = manifest.get("operations", {}).get(identifier) if isinstance(identifier, str) else None
        if not isinstance(checkpoint, dict) or checkpoint.get("op") != "read":
            available = [key for key, value in manifest.get("operations", {}).items()
                         if isinstance(value, dict) and value.get("op") == "read"]
            fail("compact citation needs a completed read operation", pointer + "/read_id",
                 kind="citation-read-id", available_read_ids=available[:20],
                 available_read_count=len(available),
                 hint="Use a completed read operation ID, rather than an artifact hash or search ID.")
            return item
        value = review_runtime.load_view(context, checkpoint["artifact"])
        first, last = item["line_start"], item["line_end"]
        if (type(first) is not int or type(last) is not int or first < 1 or first > last
                or not isinstance(value, dict) or not isinstance(value.get("lines"), list)):
            fail("compact citation has an invalid line interval", pointer,
                 kind="citation-interval", read_id=identifier)
            return item
        available = {line.get("line") for line in value["lines"]
                     if isinstance(line, dict) and type(line.get("line")) is int}
        if last - first + 1 > len(available) or any(line not in available for line in range(first, last + 1)):
            ranges = []
            for line in sorted(available):
                if ranges and ranges[-1][1] + 1 == line:
                    ranges[-1][1] = line
                else:
                    ranges.append([line, line])
            fail("compact citation lines were not included in its read operation", pointer,
                 kind="citation-unread-lines", read_id=identifier, available_line_ranges=ranges[:20],
                 available_range_count=len(ranges),
                 hint="Cite the returned line ranges or read the missing lines with a new operation ID.")
            return item
        if not all(isinstance(value.get(key), str) and value[key] for key in ("repo", "source", "ref", "path", "sha256")):
            fail("compact citation read has invalid fixed-source metadata", pointer,
                 kind="citation-source", read_id=identifier)
            return item
        return {**{key: value[key] for key in ("repo", "source", "ref", "path")},
                "line_start": first, "line_end": last}

    for field in ("coverage", "impact_coverage", "boundary_reviews", "merge_parent_review",
                  "findings", "pending_confirmations"):
        rows = result.get(field, [])
        if not isinstance(rows, list):
            continue  # The common validator reports the ordinary shape error.
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            pointer = f"/{field}/{index}"
            if field == "coverage" and "change_id" in row:
                identifier = row.pop("change_id")
                if not isinstance(identifier, str) or identifier not in changes:
                    fail("compact coverage names an unknown change_id", pointer + "/change_id",
                         kind="change-id")
                else:
                    for key, value in changes[identifier].items():
                        if key in row and row[key] != value:
                            fail("compact coverage conflicts with its fixed change", pointer + "/" + key,
                                 kind="change-conflict")
                        row[key] = value
            if isinstance(row.get("evidence"), list):
                row["evidence"] = [citation(item, pointer + f"/evidence/{i}")
                                   for i, item in enumerate(row["evidence"])]
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
                    derived(row, "operation_scenario", row.pop("scenario"), pointer)
                if "explanation" in row:
                    derived(row, "technical_explanation", row.pop("explanation"), pointer)
                if "trigger" in row:
                    derived(row, "technical_trigger", row["trigger"], pointer)
                if "impact" in row:
                    derived(row, "impact_scope", row["impact"], pointer)
                if "fix" in row:
                    value = row.pop("fix")
                    derived(row, "recommendation", value, pointer)
                    derived(row, "engineering_fix", value, pointer)
    if errors:
        raise AuthoringError(errors)
    return result
