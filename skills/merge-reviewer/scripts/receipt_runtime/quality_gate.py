"""Read-only structural checks for a single-repository Megin workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True


WORK_ID_PATTERN = re.compile(r"work-[0-9]{8}-[a-z0-9-]+\Z")
PLAN_VERSION_PATTERN = re.compile(r"plan-[a-z0-9-]+\Z")
RESULT_STATUSES = {"passed", "failed", "blocked", "not_run"}


class InvalidEvidence(Exception):
    """An input cannot be interpreted as this version of the evidence format."""


def git(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, check=False,
    )
    if result.returncode:
        raise InvalidEvidence(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def line(repo: Path, *arguments: str) -> str:
    return git(repo, *arguments).decode("utf-8", errors="replace").strip()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise InvalidEvidence(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidEvidence(f"expected JSON object: {path}")
    return value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def workflow_fields(path: Path) -> dict[str, str]:
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidEvidence(f"cannot read workflow: {exc}") from exc
    header = contents.split("\n## ", maxsplit=1)[0]
    fields: dict[str, str] = {}
    for match in re.finditer(r"(?m)^- ([a-z0-9_]+): (.+)$", header):
        key, value = match.groups()
        if key in fields:
            raise InvalidEvidence(f"duplicate workflow header: {key}")
        fields[key] = value
    return fields


def canonical_relative(relative: object) -> str:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise InvalidEvidence(f"invalid relative path: {relative!r}")
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or pure.as_posix() != relative
            or any(part in ("", ".", "..") for part in pure.parts)
            or (pure.parts and ":" in pure.parts[0])):
        raise InvalidEvidence(f"invalid relative path: {relative!r}")
    return relative


def within_repo(repo: Path, relative: object) -> Path:
    canonical = canonical_relative(relative)
    path = (repo / canonical).resolve()
    if not path.is_relative_to(repo) or path == repo:
        raise InvalidEvidence(f"path escapes repository: {relative}")
    return path


def evidence_file(repo: Path, work_id: str, relative: object) -> Path:
    path = within_repo(repo, relative)
    root = (repo / "docs" / "work" / work_id / "evidence").resolve()
    if not path.is_relative_to(root) or path == root:
        raise InvalidEvidence(f"source must be in this Work ID's evidence directory: {relative}")
    return path


def valid_allowed_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    candidate = value[:-1] if value.endswith("/") else value
    try:
        return canonical_relative(candidate) == candidate
    except InvalidEvidence:
        return False


def load_contract(repo: Path, work_id: str) -> tuple[dict[str, str], dict, set[str]]:
    work = repo / "docs" / "work" / work_id
    fields = workflow_fields(work / "workflow.md")
    if fields.get("work_id") != work_id:
        raise InvalidEvidence("workflow Work ID mismatch")
    plan_version = fields.get("plan_version", "")
    if not PLAN_VERSION_PATTERN.fullmatch(plan_version):
        raise InvalidEvidence("workflow plan_version is invalid")
    contract = read_json(work / plan_version / "quality-contract.json")
    contract_schema = contract.get("schema")
    if contract_schema == "megin-repo-quality-contract/v3" or fields.get('schema') == 'megin-repo-workflow/v3':
        from unified_workflow import load_contract as load_unified
        fields, contract, _repos, records = load_unified(repo, work_id, group=False)
        return fields, contract, records
    if (contract_schema not in ("megin-quality-contract/v1", "megin-repo-quality-contract/v1", "megin-repo-quality-contract/v2")
            or contract.get("work_id") != work_id
            or contract.get("plan_version") != plan_version):
        raise InvalidEvidence("approved quality contract identity mismatch")

    checks, allowed = contract.get("checks"), contract.get("allowed_paths")
    records = contract.get("process_records")
    if (not isinstance(checks, list) or not checks or not isinstance(allowed, list)
            or not allowed or not isinstance(records, list) or not records):
        raise InvalidEvidence(
            "approved quality contract needs checks, allowed paths, and process records"
        )
    ids = [entry.get("id") for entry in checks if isinstance(entry, dict)]
    if (len(ids) != len(checks) or any(not isinstance(item, str) or not item for item in ids)
            or len(set(ids)) != len(ids)
            or any(entry.get("kind") not in ("test", "command")
                   or not isinstance(entry.get("command"), str) or not entry["command"]
                   for entry in checks)
            or any(not valid_allowed_path(rule) for rule in allowed)):
        raise InvalidEvidence("duplicate or invalid approved check IDs or paths")

    record_set: set[str] = set()
    for record in records:
        canonical = canonical_relative(record)
        evidence_file(repo, work_id, canonical)
        record_set.add(canonical)
    if len(record_set) != len(records):
        raise InvalidEvidence("duplicate process record paths")
    if contract_schema in ("megin-repo-quality-contract/v1", "megin-repo-quality-contract/v2"):
        if (not isinstance(contract.get("base_branch"), str)
                or not contract["base_branch"]
                or not isinstance(contract.get("base_commit"), str)
                or not re.fullmatch(r"[0-9a-f]{40,64}", contract["base_commit"])
                or not isinstance(contract.get("feature_branch"), str)
                or not contract["feature_branch"]):
            raise InvalidEvidence("single-repository contract needs a pinned base and feature branch")
        remote_name, remote_url = contract.get("remote_name"), contract.get("remote_url")
        if (remote_name is None) != (remote_url is None):
            raise InvalidEvidence("remote_name and remote_url must be selected together")
        if remote_name is not None and (
            not isinstance(remote_name, str) or not remote_name
            or not isinstance(remote_url, str) or not remote_url
        ):
            raise InvalidEvidence("selected remote identity is invalid")
        delivery_ref = contract.get("delivery_ref")
        if not isinstance(delivery_ref, str) or delivery_ref not in record_set:
            raise InvalidEvidence("single-repository contract needs a predeclared delivery_ref")
        if (not isinstance(contract.get("skills_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", contract["skills_sha256"])):
            raise InvalidEvidence("single-repository contract needs the approved Skills fingerprint")
        if fields.get("schema") in ("megin-repo-workflow/v1", "megin-repo-workflow/v2") and (
            fields.get("quality_ref") != contract.get("quality_ref")
            or fields.get("delivery_ref") != delivery_ref
        ):
            raise InvalidEvidence("workflow and contract evidence references differ")
    if ((contract_schema == "megin-repo-quality-contract/v2")
            != (fields.get("schema") == "megin-repo-workflow/v2")):
        raise InvalidEvidence("native v2 contract requires matching workflow v2")
    from behavior_trace import validate_trace
    from verification_inputs import validate_inputs
    validate_trace(contract)
    validate_inputs(repo, contract.get("verification_inputs"), set(ids))
    return fields, contract, record_set


def process_record(relative: str, work_id: str, records: set[str]) -> bool:
    return (relative == f"docs/work/{work_id}/workflow.md"
            or relative == ".megin/workspace.lock.json"
            or relative in records)


def digest_entries(entries: list[dict[str, str]]) -> str:
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def worktree_entries(repo: Path, work_id: str, records: set[str]) -> list[dict[str, str]]:
    paths = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    entries: list[dict[str, str]] = []
    for raw in sorted(set(paths.split(b"\0")) - {b""}):
        relative = raw.decode("utf-8")
        if process_record(relative, work_id, records):
            continue
        path = within_repo(repo, relative)
        if path.is_file():
            entries.append({
                "path": relative,
                "content": line(repo, "hash-object", f"--path={relative}", relative),
            })
    return entries


def index_entries(repo: Path, work_id: str, records: set[str]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for record in git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", maxsplit=1)
        _mode, object_id, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise InvalidEvidence("unmerged index cannot be checked")
        relative = raw_name.decode("utf-8")
        if process_record(relative, work_id, records):
            continue
        entries.append({"path": relative, "content": object_id})
    return sorted(entries, key=lambda item: item["path"])


def fixed_inputs(repo: Path, contract: dict) -> list[dict]:
    from verification_inputs import validate_inputs
    return validate_inputs(repo, contract.get("verification_inputs"), {c["id"] for c in contract["checks"]})


def with_inputs(entries: list[dict], inputs: list[dict]) -> list[dict]:
    from verification_inputs import snapshot_entries
    return entries + snapshot_entries(inputs)


def validate_input_evidence(repo, work_id, records, contract, evidence, reasons) -> None:
    if contract.get("verification_inputs") is None:
        return
    from verification_inputs import canonical_digest
    inputs = fixed_inputs(repo, contract)
    digest = canonical_digest(inputs)
    ids = {identifier for item in inputs for identifier in item["check_ids"]}
    for result in evidence.get("checks", []):
        if not isinstance(result, dict) or result.get("id") not in ids:
            continue
        if result.get("verification_inputs_sha256") != digest:
            reasons.append(f"{result['id']}: fixed input digest missing or changed")
        cited_claims(repo, work_id, records, result.get("output"), result["id"], reasons,
                     {"verification_inputs": f"Verification inputs SHA-256: {digest}"})


def snapshot(repo: Path, work_id: str) -> dict[str, object]:
    if _unified_repo(repo, work_id):
        from unified_workflow import snapshot as unified_snapshot
        return unified_snapshot(repo, work_id, group=False)
    _fields, contract, records = load_contract(repo, work_id)
    inputs = fixed_inputs(repo, contract)
    entries = with_inputs(worktree_entries(repo, work_id, records), inputs)
    result = {
        "schema": (("megin-repo-quality-snapshot/v2" if contract.get("schema") == "megin-repo-quality-contract/v2" else "megin-repo-quality-snapshot/v1")
                   if contract.get("schema") in ("megin-repo-quality-contract/v1", "megin-repo-quality-contract/v2")
                   else "megin-quality-snapshot/v1"),
        "branch": line(repo, "branch", "--show-current"),
        "head": line(repo, "rev-parse", "HEAD"),
        "product_sha256": digest_entries(entries),
        "path_count": len(entries),
    }
    if contract.get("verification_inputs") is not None:
        from verification_inputs import canonical_digest
        result.update(verification_inputs=inputs, verification_inputs_sha256=canonical_digest(inputs))
    return result


def cited_file(
    repo: Path, work_id: str, records: set[str], value: object, label: str,
    reasons: list[str], require_locator: bool = False,
) -> list[str] | None:
    if not isinstance(value, dict):
        reasons.append(f"{label}: missing source reference")
        return None
    relative, expected = value.get("path"), value.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        reasons.append(f"{label}: incomplete source reference")
        return None
    path = evidence_file(repo, work_id, relative)
    if relative not in records:
        raise InvalidEvidence(f"{label}: source is not an approved process record")
    if not path.is_file():
        reasons.append(f"{label}: source missing or digest changed")
        return None
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        reasons.append(f"{label}: source missing or digest changed")
        return None
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if require_locator:
        number, claimed = value.get("line"), value.get("text")
        if (type(number) is not int or number < 1 or number > len(lines)
                or not isinstance(claimed, str) or lines[number - 1] != claimed):
            reasons.append(f"{label}: raw output locator missing or changed")
    return lines


def cited_claims(
    repo: Path, work_id: str, records: set[str], value: object, label: str,
    reasons: list[str], expected: dict[str, str], require_locator: bool = False,
) -> None:
    lines = cited_file(
        repo, work_id, records, value, label, reasons, require_locator,
    )
    if lines is None or not isinstance(value, dict):
        return
    claims = value.get("claims")
    if not isinstance(claims, dict):
        reasons.append(f"{label}: raw claim locators missing")
        return
    for name, expected_text in expected.items():
        locator = claims.get(name)
        if not isinstance(locator, dict):
            reasons.append(f"{label}: {name} claim locator missing")
            continue
        number, claimed = locator.get("line"), locator.get("text")
        if (type(number) is not int or number < 1 or number > len(lines)
                or claimed != expected_text or lines[number - 1] != expected_text):
            reasons.append(f"{label}: raw {name} claim differs from structured evidence")


def path_set(repo: Path, *arguments: str) -> set[str]:
    return {
        value.decode("utf-8")
        for value in git(repo, *arguments, "-z").split(b"\0") if value
    }


def changed_paths(repo: Path, base_commit: str) -> set[str]:
    return (
        path_set(repo, "diff", "--name-only", base_commit)
        | path_set(repo, "ls-files", "--others", "--exclude-standard")
    )


def product_paths(paths: set[str], work_id: str, records: set[str]) -> set[str]:
    return {path for path in paths if not process_record(path, work_id, records)}


def validate_recorded_checks(
    repo: Path, work_id: str, records: set[str], obligations: list[dict],
    evidence: dict, digest: str, reasons: list[str], require_pass: bool,
) -> None:
    recorded = evidence.get("checks")
    valid_recorded = isinstance(recorded, list) and all(
        isinstance(entry, dict) and isinstance(entry.get("id"), str)
        for entry in recorded
    )
    by_id = {entry["id"]: entry for entry in recorded} if valid_recorded else {}
    if not valid_recorded or len(by_id) != len(recorded):
        reasons.append("duplicate or invalid recorded check IDs")
    approved_ids = {entry["id"] for entry in obligations}
    for extra in sorted(set(by_id) - approved_ids):
        reasons.append(f"{extra}: result is not an approved check")
    for obligation in obligations:
        identifier = obligation["id"]
        result = by_id.get(identifier)
        if not isinstance(result, dict):
            reasons.append(f"{identifier}: required result missing")
            continue
        status, exit_code = result.get("status"), result.get("exit_code")
        expected = {"command": f"Command: {obligation['command']}"}
        if "cwd" in obligation:
            expected["cwd"] = f"Working directory: {obligation['cwd']}"
        if type(exit_code) is int:
            expected["exit_code"] = f"Exit code: {exit_code}"
        cited_claims(
            repo, work_id, records, result.get("output"), identifier, reasons,
            expected, require_locator=True,
        )
        if result.get("snapshot") != digest:
            reasons.append(f"{identifier}: stale snapshot")
        if not isinstance(status, str) or status not in RESULT_STATUSES or type(exit_code) is not int:
            reasons.append(f"{identifier}: result status or exit code is unknown")
        if obligation.get("kind") == "test":
            counts = tuple(result.get(name) for name in ("executed", "failed", "skipped"))
            if any(type(value) is not int or value < 0 for value in counts):
                reasons.append(f"{identifier}: test counts are missing or invalid")
            elif require_pass and (counts[0] <= 0 or counts[1] != 0 or counts[2] != 0):
                reasons.append(f"{identifier}: test count is zero, failed, skipped, or unknown")
        if require_pass and (status != "passed" or exit_code != 0):
            reasons.append(f"{identifier}: required command did not pass")


def check(repo: Path, work_id: str, gate: str) -> dict[str, object]:
    if _unified_repo(repo, work_id):
        from unified_workflow import check as unified_check
        return unified_check(repo, work_id, gate, group=False)
    if gate == "completion":
        return check_completion(repo, work_id)
    fields, contract, records = load_contract(repo, work_id)
    checks, allowed = contract["checks"], contract["allowed_paths"]
    reference = canonical_relative(fields.get("quality_ref", ""))
    quality_path = evidence_file(repo, work_id, reference)
    if reference not in records:
        raise InvalidEvidence("workflow quality_ref is not an approved process record")
    evidence = read_json(quality_path)
    plan_version = fields["plan_version"]
    expected_evidence_schema = (
        ("megin-repo-quality-evidence/v2" if contract.get("schema") == "megin-repo-quality-contract/v2" else "megin-repo-quality-evidence/v1")
        if contract.get("schema") in ("megin-repo-quality-contract/v1", "megin-repo-quality-contract/v2")
        else "megin-quality-evidence/v1"
    )
    if (evidence.get("schema"), evidence.get("work_id"), evidence.get("plan_version")) != (
        expected_evidence_schema, work_id, plan_version,
    ):
        raise InvalidEvidence("quality evidence identity mismatch")

    current = snapshot(repo, work_id)
    digest = current["product_sha256"]
    reasons: list[str] = []
    if contract.get("schema") in ("megin-repo-quality-contract/v1", "megin-repo-quality-contract/v2"):
        if (contract["base_branch"] != fields.get("base_branch")
                or contract["base_commit"] != fields.get("base_commit")
                or contract["feature_branch"] != fields.get("feature_branch")):
            raise InvalidEvidence("workflow and single-repository contract branch identity differs")
    if current["branch"] != fields.get("feature_branch"):
        reasons.append("current branch differs from approved feature branch")
    base_branch, base_commit = fields.get("base_branch", ""), fields.get("base_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise InvalidEvidence("workflow base_commit is not a full Git SHA")
    if line(repo, "rev-parse", base_branch) != base_commit:
        reasons.append("base branch advanced")
    if gate == "delivery" and contract.get("schema") in ("megin-repo-quality-contract/v1", "megin-repo-quality-contract/v2"):
        _check_remote_base(repo, contract, reasons)
    if evidence.get("snapshot") != digest:
        reasons.append("product or approved-contract snapshot changed")
    for relative in sorted(changed_paths(repo, base_commit)):
        if process_record(relative, work_id, records):
            continue
        if not any(
            relative.startswith(rule) if rule.endswith("/") else relative == rule
            for rule in allowed
        ):
            reasons.append(f"changed path outside approved scope: {relative}")

    writer = evidence.get("writer")
    writer_context = writer.get("context") if isinstance(writer, dict) else None
    writer_snapshot = writer.get("snapshot") if isinstance(writer, dict) else None
    if (not isinstance(writer_context, str) or not writer_context
            or not isinstance(writer_snapshot, str) or writer_snapshot != digest):
        reasons.append("writer handoff context or snapshot missing")
    else:
        cited_claims(
            repo, work_id, records, writer.get("source"), "writer handoff", reasons,
            {
                "context": f"- context: {writer_context}",
                "snapshot": f"- snapshot: {writer_snapshot}",
            },
        )

    sources = evidence.get("sources")
    if not isinstance(sources, list):
        reasons.append("supporting source references missing")
    else:
        for index, source in enumerate(sources, start=1):
            cited_file(
                repo, work_id, records, source, f"supporting source {index}", reasons,
            )

    validate_recorded_checks(
        repo, work_id, records, checks, evidence, digest, reasons,
        require_pass=gate in ("acceptance", "delivery"),
    )
    validate_input_evidence(repo, work_id, records, contract, evidence, reasons)
    from quality_policy import validate_evidence
    validate_evidence(repo, work_id, fields, contract, records, evidence, digest, gate, reasons)

    if gate in ("review", "acceptance"):
        staged = product_paths(
            path_set(repo, "diff", "--cached", "--name-only", "HEAD"), work_id, records,
        )
        if staged:
            reasons.append("product paths are staged before human acceptance")

    if gate in ("acceptance", "delivery"):
        reviewer = evidence.get("review")
        if not isinstance(reviewer, dict):
            reasons.append("independent review missing")
        else:
            review_context = reviewer.get("context")
            review_verdict = reviewer.get("verdict")
            review_snapshot = reviewer.get("snapshot")
            valid_review_fields = all(
                isinstance(value, str) and bool(value)
                for value in (review_context, review_verdict, review_snapshot)
            )
            if valid_review_fields:
                cited_claims(
                    repo, work_id, records, reviewer.get("source"), "review", reasons,
                    {
                        "context": f"- context: {review_context}",
                        "verdict": f"- verdict: {review_verdict}",
                        "snapshot": f"- snapshot: {review_snapshot}",
                    },
                )
            if (not valid_review_fields or review_verdict != "APPROVED"
                    or review_snapshot != digest or review_context == writer_context):
                reasons.append("independent APPROVED review for current snapshot missing")

    staged_digest: str | None = None
    staged_paths: list[str] | None = None
    if gate == "delivery":
        acceptance = evidence.get("acceptance")
        if not isinstance(acceptance, dict):
            reasons.append("human acceptance missing")
        else:
            accepted_work = acceptance.get("work_id")
            version = acceptance.get("version")
            accepted_snapshot = acceptance.get("snapshot")
            acceptance_verdict = acceptance.get("verdict")
            valid_acceptance_fields = all(
                isinstance(value, str) and bool(value)
                for value in (
                    accepted_work, version, accepted_snapshot, acceptance_verdict,
                )
            )
            if valid_acceptance_fields:
                cited_claims(
                    repo, work_id, records, acceptance.get("source"),
                    "acceptance", reasons,
                    {
                        "work_id": f"- work_id: {accepted_work}",
                        "version": f"- version: {version}",
                        "snapshot": f"- snapshot: {accepted_snapshot}",
                        "verdict": f"- verdict: {acceptance_verdict}",
                    },
                )
            if (acceptance.get("actor_kind", "human") != "human"
                    or not valid_acceptance_fields or accepted_work != work_id
                    or acceptance_verdict != "ACCEPTED"
                    or accepted_snapshot != digest):
                reasons.append("human acceptance does not bind current Work ID and snapshot")

        unstaged = product_paths(
            path_set(repo, "diff", "--name-only")
            | path_set(repo, "ls-files", "--others", "--exclude-standard"),
            work_id, records,
        )
        if unstaged:
            reasons.append("unstaged or untracked product paths remain before delivery")
        staged_digest = digest_entries(with_inputs(index_entries(repo, work_id, records), fixed_inputs(repo, contract)))
        staged_paths = sorted(product_paths(
            path_set(repo, "diff", "--cached", "--name-only", base_commit),
            work_id, records,
        ))
        if staged_digest != digest:
            reasons.append("staged product bytes differ from accepted snapshot")

    result: dict[str, object] = {
        "gate": gate, "ok": not reasons, "snapshot": digest, "reasons": reasons,
    }
    if gate == "delivery":
        result.update(staged_snapshot=staged_digest, staged_paths=staged_paths)
    return result


def _check_remote_base(repo: Path, contract: dict, reasons: list[str]) -> None:
    """Check the frozen remote only when the approved plan explicitly selected one."""
    name, url = contract.get("remote_name"), contract.get("remote_url")
    if name is None and url is None:
        return
    try:
        actual_url = line(repo, "remote", "get-url", name)
        if actual_url != url:
            reasons.append("configured remote URL differs from approved remote_url")
            return
        ref = f"refs/heads/{contract['base_branch']}"
        remote_line = line(repo, "ls-remote", "--exit-code", name, ref)
        parts = remote_line.split()
        if len(parts) != 2 or parts[1] != ref or parts[0] != contract["base_commit"]:
            reasons.append("remote base no longer matches approved base_commit")
    except InvalidEvidence as exc:
        reasons.append(f"remote base check failed: {exc}")


def tree_entries(repo: Path, commit: str, records: set[str], work_id: str) -> list[dict]:
    entries: list[dict[str, str]] = []
    raw = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", maxsplit=1)
        _mode, _kind, object_id = metadata.decode("ascii").split()
        relative = raw_path.decode("utf-8")
        if not process_record(relative, work_id, records):
            entries.append({"path": relative, "content": object_id})
    return sorted(entries, key=lambda item: item["path"])


def _tree_digest(repo: Path, commit: str, records: set[str], work_id: str, inputs=None) -> str:
    return digest_entries(with_inputs(tree_entries(repo, commit, records, work_id), inputs or []))


def check_completion(repo: Path, work_id: str, *, historical: bool = False) -> dict[str, object]:
    if _unified_repo(repo, work_id):
        from unified_workflow import check as unified_check
        return unified_check(repo, work_id, 'completion', group=False, historical=historical)
    """Verify the accepted feature commit and its local --no-ff integration."""
    fields, contract, records = load_contract(repo, work_id)
    if contract.get("schema") not in ("megin-repo-quality-contract/v1", "megin-repo-quality-contract/v2"):
        raise InvalidEvidence("completion gate is available for megin-repo-quality-contract/v1")
    if fields.get("schema") != ("megin-repo-workflow/v2" if contract.get("schema") == "megin-repo-quality-contract/v2" else "megin-repo-workflow/v1") or fields.get("work_id") != work_id:
        raise InvalidEvidence("workflow is not the matching megin-repo-workflow/v1 record")
    if fields.get("phase") != "delivery" or fields.get("status") not in ("active", "complete"):
        raise InvalidEvidence("completion requires the delivery phase")
    if historical and fields.get("status") != "complete":
        raise InvalidEvidence("historical receipt requires completed local delivery")
    reference = canonical_relative(fields.get("quality_ref", ""))
    if reference not in records:
        raise InvalidEvidence("workflow quality_ref is not an approved process record")
    evidence = read_json(evidence_file(repo, work_id, reference))
    delivery_path = evidence_file(repo, work_id, contract["delivery_ref"])
    delivery = read_json(delivery_path)
    reasons: list[str] = []
    if (evidence.get("schema"), evidence.get("work_id"), evidence.get("plan_version")) != (
        ("megin-repo-quality-evidence/v2" if contract.get("schema") == "megin-repo-quality-contract/v2" else "megin-repo-quality-evidence/v1"), work_id, fields.get("plan_version"),
    ):
        raise InvalidEvidence("quality evidence identity mismatch")
    accepted_snapshot = evidence.get("snapshot")
    if not historical and snapshot(repo, work_id).get("product_sha256") != accepted_snapshot:
        reasons.append("product snapshot differs from accepted evidence")
    if (delivery.get("schema"), delivery.get("work_id"), delivery.get("plan_version")) != (
        "megin-repo-delivery-result/v1", work_id, fields.get("plan_version"),
    ):
        raise InvalidEvidence("delivery result identity mismatch")
    receipt = delivery.get("delivery_gate")
    if not isinstance(receipt, dict):
        reasons.append("passing delivery-gate receipt missing")
    else:
        stdout, exit_code, expected_sha = receipt.get("stdout"), receipt.get("exit_code"), receipt.get("sha256")
        if not isinstance(stdout, str) or type(exit_code) is not int or not isinstance(expected_sha, str):
            reasons.append("delivery-gate receipt is incomplete")
        else:
            if hashlib.sha256(stdout.encode("utf-8")).hexdigest() != expected_sha:
                reasons.append("delivery-gate receipt digest changed")
            try:
                gate_result = json.loads(stdout)
            except json.JSONDecodeError:
                gate_result = {}
            if (exit_code != 0 or gate_result.get("gate") != "delivery"
                    or gate_result.get("ok") is not True
                    or gate_result.get("snapshot") != accepted_snapshot):
                reasons.append("delivery-gate receipt does not pass for the accepted snapshot")
    if delivery.get("accepted_snapshot") != accepted_snapshot:
        reasons.append("delivery result does not bind the accepted snapshot")
    validate_recorded_checks(
        repo, work_id, records, contract["checks"], evidence,
        accepted_snapshot if isinstance(accepted_snapshot, str) else "", reasons,
        require_pass=True,
    )
    validate_input_evidence(repo, work_id, records, contract, evidence, reasons)
    from quality_policy import validate_evidence
    def delivered_test(relative):
        commit = delivery.get("feature_commit", "")
        rows = git(repo, "ls-tree", "-z", commit, "--", relative).split(b"\0")
        if len(rows) != 2 or not rows[0]:
            raise InvalidEvidence("delivered TDD test is missing")
        metadata, name = rows[0].split(b"\t", 1)
        mode, kind, _blob = metadata.decode("ascii").split()
        if name.decode("utf-8") != relative or kind != "blob" or mode not in ("100644", "100755"):
            raise InvalidEvidence("delivered TDD test is not a regular blob")
        return git(repo, "cat-file", "blob", commit + ":" + relative)

    validate_evidence(repo, work_id, fields, contract, records, evidence, accepted_snapshot, "completion", reasons,
                      test_reader=delivered_test if historical else None)
    writer = evidence.get("writer")
    writer_context = writer.get("context") if isinstance(writer, dict) else None
    writer_snapshot = writer.get("snapshot") if isinstance(writer, dict) else None
    if (not isinstance(writer_context, str) or not writer_context
            or writer_snapshot != accepted_snapshot):
        reasons.append("writer context does not bind accepted snapshot")
    if isinstance(writer, dict):
        cited_claims(
            repo, work_id, records, writer.get("source"), "writer handoff", reasons,
            {
                "context": f"- context: {writer_context}",
                "snapshot": f"- snapshot: {writer_snapshot}",
            },
        )
    sources = evidence.get("sources")
    if not isinstance(sources, list):
        reasons.append("supporting source references missing")
    else:
        for index, source in enumerate(sources, start=1):
            cited_file(repo, work_id, records, source, f"supporting source {index}", reasons)

    reviewer = evidence.get("review")
    reviewer_context = None
    if not isinstance(reviewer, dict):
        reasons.append("independent review missing")
    else:
        review_context = reviewer.get("context")
        reviewer_context = review_context
        review_verdict = reviewer.get("verdict")
        review_snapshot = reviewer.get("snapshot")
        if (not isinstance(review_context, str) or not review_context
                or review_context == writer_context
                or review_verdict != "APPROVED" or review_snapshot != accepted_snapshot):
            reasons.append("independent APPROVED review does not bind accepted snapshot")
        cited_claims(
            repo, work_id, records, reviewer.get("source"), "review", reasons,
            {
                "context": f"- context: {review_context}",
                "verdict": f"- verdict: {review_verdict}",
                "snapshot": f"- snapshot: {review_snapshot}",
            },
        )
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, dict):
        reasons.append("human acceptance missing")
    else:
        accepted_work = acceptance.get("work_id")
        version = acceptance.get("version")
        acceptance_snapshot = acceptance.get("snapshot")
        verdict = acceptance.get("verdict")
        if (acceptance.get("actor_kind", "human") != "human"
                or accepted_work != work_id or not isinstance(version, str) or not version
                or acceptance_snapshot != accepted_snapshot or verdict != "ACCEPTED"):
            reasons.append("human acceptance does not bind the accepted snapshot")
        cited_claims(
            repo, work_id, records, acceptance.get("source"), "acceptance", reasons,
            {
                "work_id": f"- work_id: {accepted_work}",
                "version": f"- version: {version}",
                "snapshot": f"- snapshot: {acceptance_snapshot}",
                "verdict": f"- verdict: {verdict}",
            },
        )
    feature_commit, merge_commit = delivery.get("feature_commit"), delivery.get("merge_commit")
    for label, value in (("feature_commit", feature_commit), ("merge_commit", merge_commit)):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40,64}", value):
            reasons.append(f"{label} is not a full Git SHA")
    base_commit = contract["base_commit"]
    if isinstance(feature_commit, str) and re.fullmatch(r"[0-9a-f]{40,64}", feature_commit):
        try:
            parents = line(repo, "show", "-s", "--format=%P", feature_commit).split()
            if parents != [base_commit]:
                reasons.append("feature commit must directly descend from approved base_commit")
            if _tree_digest(repo, feature_commit, records, work_id, fixed_inputs(repo, contract)) != accepted_snapshot:
                reasons.append("feature commit tree differs from accepted product snapshot")
            if historical:
                prefix = f"docs/work/{work_id}/"
                protected = [e for e in tree_entries(repo, feature_commit, records, work_id) if e["path"].startswith(prefix)]
                current_protected = [e for e in worktree_entries(repo, work_id, records) if e["path"].startswith(prefix)]
                if protected != current_protected:
                    reasons.append("protected historical work records changed")
            elif line(repo, "rev-parse", contract["feature_branch"]) != feature_commit:
                reasons.append("approved feature branch no longer points to feature_commit")
        except InvalidEvidence as exc:
            reasons.append(f"feature commit cannot be verified: {exc}")
    if isinstance(merge_commit, str) and re.fullmatch(r"[0-9a-f]{40,64}", merge_commit):
        try:
            parents = line(repo, "show", "-s", "--format=%P", merge_commit).split()
            if parents != [base_commit, feature_commit]:
                reasons.append("local merge must be a --no-ff merge of approved base and feature commits")
            if not historical and line(repo, "rev-parse", f"refs/heads/{contract['base_branch']}") != merge_commit:
                reasons.append("local base branch does not point to merge_commit")
            if line(repo, "rev-parse", f"{merge_commit}^{{tree}}") != line(repo, "rev-parse", f"{feature_commit}^{{tree}}"):
                reasons.append("local merge tree differs from accepted feature tree")
        except InvalidEvidence as exc:
            reasons.append(f"local merge cannot be verified: {exc}")
    if not historical and contract.get("remote_name") is not None:
        _check_remote_base(repo, contract, reasons)
    return {
        "gate": "completion", "ok": not reasons, "snapshot": accepted_snapshot,
        "feature_commit": feature_commit, "merge_commit": merge_commit, "reasons": reasons,
    }


def _unified_repo(repo, work_id):
    return workflow_fields(Path(repo) / 'docs/work' / work_id / 'workflow.md').get('schema') == 'megin-repo-workflow/v3'


def _workspace_extension():
    """Load a host-specific extension when the selected workspace supplies one."""
    import importlib.util
    path = Path(__file__).with_name("workspace_extension.py")
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("megin_workspace_extension", path)
    if spec is None or spec.loader is None:
        raise InvalidEvidence("cannot load workspace extension")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    extension = _workspace_extension()
    actions = ("snapshot", "check")
    if extension:
        actions = extension.quality_actions(actions)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=actions)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--gate", choices=("review", "acceptance", "delivery", "completion"))
    if extension:
        extension.extend_quality_parser(parser)
    args = parser.parse_args()
    try:
        if not WORK_ID_PATTERN.fullmatch(args.work_id):
            raise InvalidEvidence("invalid Work ID")
        if args.action == "check" and not args.gate:
            raise InvalidEvidence("check requires --gate")
        if args.action != "check" and args.gate:
            raise InvalidEvidence("--gate is valid only with check")
        if bool(args.repo) == bool(getattr(args, "group_root", None)):
            raise InvalidEvidence("provide exactly one of --repo or the installed workspace root")
        if getattr(args, "group_root", None):
            if extension is None:
                raise InvalidEvidence("the selected workspace profile does not support this action")
            result = extension.run_quality_action(args)
        else:
            repo = args.repo.resolve()
            if not repo.is_dir() or Path(line(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
                raise InvalidEvidence("--repo must identify the Git repository root")
            if args.action == 'validate-record':
                from unified_workflow import validate_record
                result = validate_record(repo, args.work_id, group=False)
            else:
                result = snapshot(repo, args.work_id) if args.action == "snapshot" else check(repo, args.work_id, args.gate)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if result.get("ok") is False else 0
    except (InvalidEvidence, OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
