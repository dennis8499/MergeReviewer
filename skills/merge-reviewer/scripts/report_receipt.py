"""Verify a saved publication while allowing only the marked human record to change."""
from __future__ import annotations

import hashlib
from pathlib import Path

START = "<!-- merge-confirmations:start -->"
END = "<!-- merge-confirmations:end -->"
POLICY = "merge-report-static-body/v1"
OUTCOME = "MergeReviewOutcome/v1"


def summarize_review(result):
    """Retain semantic state from the one authoritative publication validation."""
    from workspace_report import redact
    def unfinished(field, predicate, keys):
        return [{key: row[key] for key in keys if key in row}
                for row in result.get(field, []) if predicate(row)]
    pending = result["pending_confirmation_count"]
    remaining = {
        "changes": unfinished("coverage", lambda row: row["status"] != "reviewed",
                              ("repo", "source", "path", "status", "reason")),
        "parents": unfinished("merge_parent_review", lambda row: row["status"] != "reviewed",
                              ("id", "status", "reason")),
        "impacts": unfinished("impact_coverage", lambda row: row["status"] != "reviewed",
                              ("id", "repo", "source", "path", "status", "reason")),
        "repositories": unfinished("repository_coverage", lambda row: row["status"] == "unresolved",
                                   ("repo", "status", "reason")),
        "boundaries": unfinished("boundary_reviews", lambda row: row["outcome"] == "unresolved",
                                 ("change_id", "contract_id", "outcome", "reason")),
        "pending_confirmation_count": pending}
    # These are the validator's final gaps, including captured-context and graph
    # gaps, rather than just the author's draft limitations.
    gaps = list(result["limitations"])
    for kind, rows in remaining.items():
        if isinstance(rows, list):
            for row in rows:
                identifier = row.get("id", row.get("change_id", row.get("contract_id", row.get("path", row.get("repo", "")))))
                gaps.append(f"{kind}/{identifier}: {row.get('reason', '尚未完成。')}")
    if pending:
        gaps.append(f"待人工確認：{pending} 項；人工紀錄不會重算初次審查判斷。")
    def clean(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value
    return clean({"schema": OUTCOME, "outcome_available": True,
                  "review_complete": result["review_complete"], "review_status": result["review_status"],
                  "recommendation": result["recommendation"], "extension_status": result["extension_status"],
                  "pending_confirmation_count": pending, "gaps": list(dict.fromkeys(gaps)),
                  "remaining": remaining})


def saved_review(job):
    """Read saved publication semantics; an older receipt cannot prove completeness."""
    receipt = job.get("report_receipt")
    receipt_value = receipt.get("review_outcome") if isinstance(receipt, dict) else None
    value = job.get("review_outcome", receipt_value)
    if value is None:
        return {"schema": OUTCOME, "outcome_available": False, "review_complete": None,
                "review_status": "unknown", "recommendation": None, "extension_status": "unknown",
                "pending_confirmation_count": None, "remaining": None,
                "gaps": ["舊發布紀錄未保存審查結果摘要，無法確認初次審查完整性。"]}
    if (not isinstance(value, dict) or value.get("schema") != OUTCOME
            or value.get("outcome_available") is not True or type(value.get("review_complete")) is not bool
            or not isinstance(value.get("review_status"), str)
            or not isinstance(value.get("gaps"), list) or not all(isinstance(gap, str) for gap in value["gaps"])
            or not isinstance(value.get("remaining"), dict)
            or type(value.get("pending_confirmation_count")) is not int):
        raise ValueError("Saved publication review outcome is invalid.")
    if receipt_value is not None and receipt_value != value:
        raise ValueError("Saved publication review outcome differs from its staged receipt.")
    return value


def static_body(body):
    starts, ends = body.count(START), body.count(END)
    if starts == ends == 0:
        return body
    if starts != 1 or ends != 1 or body.index(START) >= body.index(END):
        raise ValueError("Manual confirmation markers are missing, repeated or reordered.")
    first, last = body.index(START), body.index(END)
    # Markers must remain separate complete lines, not parts of edited prose.
    if (first and body[first - 1] != "\n") or (last and body[last - 1] != "\n"):
        raise ValueError("Manual confirmation marker boundaries changed.")
    return body[:first + len(START)] + "\n<editable-human-confirmations>\n" + body[last:]


def create(body, *, review_outcome=None):
    raw = body.encode("utf-8")
    value = {"policy": POLICY, "report_sha256": hashlib.sha256(raw).hexdigest(),
             "static_body_sha256": hashlib.sha256(static_body(body).encode("utf-8")).hexdigest()}
    if review_outcome is not None:
        value["review_outcome"] = review_outcome
    return value


def verify(job):
    import review_coordinator as coordination
    value = job.get("report_path") or job.get("planned_report")
    if not isinstance(value, str) or not value:
        raise ValueError("Publication receipt has no report path.")
    path = coordination.plain(Path(value))
    if not path.is_absolute() or not path.is_file():
        raise ValueError("The published report is missing.")
    project = job.get("inputs", {}).get("project")
    if project and path.parent != Path(project) / "review-reports":
        raise ValueError("Publication report path differs from the selected Repo.")
    raw = path.read_bytes()
    receipt = job.get("report_receipt")
    if receipt is None:
        if hashlib.sha256(raw).hexdigest() != job.get("report_sha256"):
            raise ValueError("The previously staged report changed; it was not replaced.")
        return {"verified": True, "policy": "legacy-full-report", "manual_record_changed": False}
    if not isinstance(receipt, dict) or receipt.get("policy") != POLICY:
        raise ValueError("Unsupported publication receipt policy.")
    actual = create(raw.decode("utf-8"))
    if actual["static_body_sha256"] != receipt.get("static_body_sha256"):
        raise ValueError("The published report's fixed body changed; it was not replaced.")
    return {"verified": True, "policy": POLICY,
            "manual_record_changed": actual["report_sha256"] != receipt.get("report_sha256")}
