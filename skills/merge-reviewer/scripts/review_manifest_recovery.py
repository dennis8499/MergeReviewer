"""Recover one staged manifest commit without replaying its ordered operations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

import review_author_operation
import review_checkpoint
import review_coordinator as coordination
import review_runtime
from review_workspace import safe_path

SCHEMA = "MergeReviewManifestRecovery/v1"
PHASE = "manifest-write-repair"
NAME = ".manifest-recovery.json"
RETRY_WITH_REQUESTS_FILE = "retry-step-with-requests-file"
_STAGED_NAME = re.compile(r"\.manifest-[0-9a-f]{32}\.json")
_RETRY_DELAYS = (.05, .1, .2, .4)
_DRAFT_FIELDS = ("previous_draft_sha256", "draft_sha256")
_CHECKPOINT_FIELDS = ("previous_checkpoint", "checkpoint")
_AUTHOR_STATE_FIELDS = set(_DRAFT_FIELDS + _CHECKPOINT_FIELDS)


class Retryable(OSError):
    """The manifest transaction can still be retried."""


class Unstaged(Retryable):
    """Retry requires the original requests file because the saved batch is unconfirmed."""


class RecordUnavailable(OSError):
    """The recovery record could not be durably staged."""


class InvalidCheckpoint(ValueError):
    """The checkpoint cannot be safely used for authoring."""


def sharing_error(error):
    return getattr(error, "winerror", None) in (5, 32, 33)


def batch_digest(batch):
    encoded = json.dumps(batch, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def batch_has_author(batch):
    return isinstance(batch, list) and any(
        isinstance(row, dict) and row.get("op") == "author" for row in batch)


def _requires_author_state(pending):
    if "author_state_required" in pending:
        if (pending["state"] != "prepared" or pending["author_state_required"] is not False
                or _AUTHOR_STATE_FIELDS.intersection(pending) or batch_has_author(pending["batch"])):
            raise ValueError("Invalid evidence-only manifest recovery record")
        return False

    # Legacy prepared records always included all four fields, even for an empty snapshot.
    if (not _AUTHOR_STATE_FIELDS.issubset(pending)
            or any(pending[key] is not None and (not isinstance(pending[key], str)
                   or not coordination.DIGEST.fullmatch(pending[key])) for key in _DRAFT_FIELDS)
            or any(pending[key] is not None and not isinstance(pending[key], dict)
                   for key in _CHECKPOINT_FIELDS)):
        raise ValueError("Invalid author state in manifest recovery record")
    return True


def read_pending(context):
    try:
        pending = coordination.read_json(safe_path(context, NAME))
    except FileNotFoundError:
        return None
    except OSError as exc:
        if not sharing_error(exc):
            raise
        raise Retryable(coordination.safe_error(exc)) from exc
    if (not isinstance(pending, dict) or pending.get("schema") != SCHEMA
            or pending.get("request_id") != Path(context).name
            or pending.get("state") not in ("author-in-progress", "author-interrupted", "prepared")
            or not isinstance(pending.get("batch"), list)
            or batch_digest(pending["batch"]) != pending.get("batch_sha256")):
        raise ValueError("Invalid manifest recovery record")
    if pending["state"] == "prepared" or "author_state_required" in pending:
        _requires_author_state(pending)
    return pending


def blocked(identifier, *, message=None, pending=None):
    message = message or "A manifest commit is pending for this review request."
    result = {"request_id": identifier, "phase": PHASE, "code": "evidence.manifest-write-repair",
              "error": message, "hint": "Restore access to the session manifest and retry native step for this request.",
              "next_action": "retry-step", "complete": False, "operations": [], "next_index": 0,
              "terminal": False, "recoverable": True, "context_retained": True,
              "ready_for_finish": False, "evidence_verified": False, "git_commands": 0}
    if pending and pending["state"] == "author-in-progress":
        result["hint"] = "Retry native step for this request. The saved batch resumes if its author state can be verified."
    if pending and pending["state"] == "author-interrupted":
        result.update(code="evidence.manifest-preparation-interrupted", recoverable=False,
                      error="Authoring changed before the manifest transaction was prepared.",
                      hint="Inspect the retained draft and checkpoint before deciding how to proceed.",
                      next_action="inspect-interrupted-step")
    return result


def retry_with_requests_file(identifier, *, message=None):
    result = blocked(identifier, message=message or "The original batch is required to resume this step.")
    result.update(next_action=RETRY_WITH_REQUESTS_FILE,
                  hint="Restore access to the session and retry step for this request with the original --requests-file.")
    return result


def interrupted(context, pending):
    record = {**pending, "state": "author-interrupted"}
    try:
        coordination.atomic(safe_path(context, NAME), record)
    except OSError:
        pass  # The existing in-progress marker still prevents any unsafe replay.
    return blocked(record["request_id"], pending=record)


def _draft_sha(context):
    try:
        return coordination.file_digest(safe_path(context, "draft.json"))
    except FileNotFoundError:
        return None


def draft_state(context):
    draft_sha = _draft_sha(context)
    try:
        checkpoint = review_checkpoint.load(context)
    except ValueError as exc:
        raise InvalidCheckpoint("Repair or restore checkpoint.json before authoring.") from exc
    return draft_sha, checkpoint


def _author_journals(context, identifiers):
    journals = {}
    for identifier in identifiers:
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
            raise ValueError("Invalid author operation in manifest recovery record")
        try:
            journals[identifier] = coordination.file_digest(safe_path(context, f"author-{identifier}.json"))
        except FileNotFoundError:
            journals[identifier] = None
    return journals


def begin_author_batch(context, batch, before, previous, authors):
    try:
        journals = _author_journals(context, authors)
    except OSError as exc:
        if not sharing_error(exc):
            raise
        raise Unstaged(coordination.safe_error(exc)) from exc
    record = {"schema": SCHEMA, "request_id": Path(context).name, "state": "author-in-progress",
              "batch": batch, "batch_sha256": batch_digest(batch), "before": before,
              "previous_draft_sha256": previous[0], "previous_checkpoint": previous[1],
              "author_journals": journals}
    try:
        coordination.atomic(safe_path(context, NAME), record)
    except OSError as exc:
        # The replacement can have completed before atomic reports an error.
        try:
            pending = read_pending(context)
        except OSError:
            raise Unstaged(coordination.safe_error(exc)) from exc
        except ValueError as invalid:
            raise RecordUnavailable("Could not verify the saved author recovery record") from invalid
        if pending is None:
            raise Unstaged(coordination.safe_error(exc)) from exc
        if batch_digest(pending) != batch_digest(record):
            raise RecordUnavailable("The saved author recovery record differs from the original batch") from exc
        raise Retryable(coordination.safe_error(exc)) from exc
    return record


def _journal_matches(context, manifest, operation, journal, previous):
    draft_sha, checkpoint = previous
    revision = review_checkpoint.revision(checkpoint)
    expected_revision = operation.get("expected_checkpoint_revision")
    if (not isinstance(journal, dict) or journal.get("fingerprint") != coordination.digest(operation)
            or journal.get("before") != draft_sha or operation.get("expected_draft_sha256") != draft_sha
            or expected_revision is not None and (type(expected_revision) is not int or expected_revision != revision)
            or "checkpoint_before" not in journal or journal["checkpoint_before"] != checkpoint):
        return False
    after, result = journal.get("after"), journal.get("result")
    if not isinstance(after, str) or not re.fullmatch(r"[a-f0-9]{64}", after) or not isinstance(result, dict):
        return False
    raw = safe_path(context, "draft.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != draft_sha:
        return False
    draft = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(draft, dict):
        return False
    candidate = review_author_operation.draft_candidate(draft, operation.get("updates"))
    expected_after = (draft_sha if review_author_operation.checkpoint_only(operation)
                      else review_author_operation.draft_digest(candidate))
    if after != expected_after:
        return False
    if after != draft_sha and (journal.get("draft") != candidate
                               or review_author_operation.draft_digest(journal["draft"]) != after):
        return False
    return (result.get("saved") is True and result.get("draft_sha256") == after
            and result.get("draft_changed") is (after != draft_sha)
            and result.get("updated_fields") == list(operation["updates"])
            and review_checkpoint.prepared_matches(
                context, manifest, operation.get("checkpoint", {}), checkpoint,
                draft_sha256=after, at=journal.get("at"), saved=result.get("checkpoint")))


def author_batch_state(context, pending, evidence_receipt):
    """Allow replay only from the unchanged baseline or one matching journal ahead of it."""
    manifest, actual = _read_manifest(safe_path(context, "manifest.json"))
    if (actual != pending["before"] or not isinstance(evidence_receipt, dict)
            or actual["sha256"] != evidence_receipt.get("manifest_sha256")):
        raise ValueError("Manifest changed before the author batch could be committed")
    previous = (pending["previous_draft_sha256"], pending["previous_checkpoint"])
    journals = pending["author_journals"]
    try:
        if draft_state(context) != previous:
            return "interrupted"
        current = _author_journals(context, journals)
        if current == journals:
            return "unchanged"
        changed = [identifier for identifier, old in journals.items() if current[identifier] != old]
        if len(changed) != 1:
            return "interrupted"
        identifier = changed[0]
        if (journals[identifier] is not None or current[identifier] is None
                or identifier in manifest.get("operations", {})):
            return "interrupted"
        operations = [row for row in pending["batch"]
                      if isinstance(row, dict) and row.get("id") == identifier and row.get("op") == "author"]
        if len(operations) != 1:
            return "interrupted"
        raw = safe_path(context, f"author-{identifier}.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() == current[identifier]:
            journal = json.loads(raw.decode("utf-8-sig"))
            if _journal_matches(context, manifest, operations[0], journal, previous):
                return "journal-only"
    except OSError as exc:
        if sharing_error(exc):
            raise Retryable(coordination.safe_error(exc)) from exc
    except (ValueError, KeyError, TypeError):
        pass  # An unverified author journal may be inspected but not applied.
    return "interrupted"


def stage(context, staged, before, after, batch, response, manifest, *, previous, phase_after):
    """Save only the batch result metadata, referencing existing content-addressed operation results."""
    response_rows = []
    ledger = manifest["operations"]
    for row in response["operations"]:
        response_rows.append({
            "fields": {key: value for key, value in row.items() if key != "result"},
            "artifact": ledger[row["id"]]["artifact"],
        })
    record = {"schema": SCHEMA, "request_id": Path(context).name, "state": "prepared",
              "staged_manifest": staged.name, "before": before, "after": after,
              "source_identity": review_checkpoint.source_identity(manifest),
              "batch": batch, "batch_sha256": batch_digest(batch),
              "phase_after": phase_after, "response": {**response, "operations": response_rows}}
    if batch_has_author(batch):
        draft_sha, checkpoint = draft_state(context)
        record.update(previous_draft_sha256=previous[0], previous_checkpoint=previous[1],
                      draft_sha256=draft_sha, checkpoint=checkpoint)
    else:
        record["author_state_required"] = False
    try:
        coordination.atomic(safe_path(context, NAME), record)
    except OSError as exc:
        raise RecordUnavailable("Could not record the prepared manifest commit") from exc


def replace_staged(staged, target, *, check_cancelled):
    for delay in (*_RETRY_DELAYS, None):
        check_cancelled()
        try:
            os.replace(staged, target)
            return
        except OSError as exc:
            if not sharing_error(exc):
                raise
            if delay is None:
                raise Retryable(coordination.safe_error(exc)) from exc
            time.sleep(delay)


def _read_manifest(path):
    raw = path.read_bytes()
    manifest = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("Invalid staged or retained manifest")
    generation = manifest.get("generation", 0)
    if type(generation) is not int or generation < 0:
        raise ValueError("Invalid manifest recovery generation")
    return manifest, {"sha256": hashlib.sha256(raw).hexdigest(), "generation": generation}


def restore(context, pending, evidence_receipt):
    """Verify the existing or staged generation and restore a result without rerunning it."""
    before, after = pending["before"], pending["after"]
    if after["generation"] != before["generation"] + 1:
        raise ValueError("Invalid manifest recovery generation")
    if not isinstance(evidence_receipt, dict) or evidence_receipt.get("manifest_sha256") not in (
            before["sha256"], after["sha256"]):
        raise ValueError("Manifest recovery does not match the saved evidence receipt")
    name = pending["staged_manifest"]
    if not isinstance(name, str) or not _STAGED_NAME.fullmatch(name):
        raise ValueError("Invalid staged manifest name")
    target, staged = safe_path(context, "manifest.json"), safe_path(context, name)
    current, identity = _read_manifest(target)
    if identity == before:
        if evidence_receipt["manifest_sha256"] != before["sha256"]:
            raise ValueError("Manifest recovery generation rolled back")
        candidate, proposed = _read_manifest(staged)
        if proposed != after:
            raise ValueError("Staged manifest checksum or generation differs")
    elif identity == after:
        candidate, staged = current, None
    else:
        raise ValueError("Retained manifest checksum or generation differs")
    if (review_checkpoint.source_identity(current) != pending["source_identity"]
            or review_checkpoint.source_identity(candidate) != pending["source_identity"]):
        raise ValueError("Fixed source identities changed during manifest recovery")
    if _requires_author_state(pending) and draft_state(context) != (pending["draft_sha256"], pending["checkpoint"]):
        raise ValueError("Draft or checkpoint differs from the staged manifest transaction")
    response = pending["response"]
    rows = []
    for row in response["operations"]:
        fields, artifact = row["fields"], row["artifact"]
        if candidate["operations"][fields["id"]]["artifact"] != artifact:
            raise ValueError("Manifest recovery artifact differs from the operation ledger")
        rows.append({**fields, "result": review_runtime.load_view(context, artifact)})
    return candidate, staged, target, {**response, "operations": rows}


def clear(context):
    try:
        safe_path(context, NAME).unlink(missing_ok=True)
    except OSError as exc:
        raise Retryable(coordination.safe_error(exc)) from exc
