"""Durable navigation checkpoints; returned pages never imply semantic coverage."""
from __future__ import annotations

import hashlib
from pathlib import Path

import review_coordinator as coordination
import review_runtime

SCHEMA = "MergeReviewCheckpoint/v1"


def source_identity(manifest):
    return coordination.digest([
        {"id": row["id"], "sources": row.get("sources", {})}
        for row in manifest["repositories"]
    ])


def location(context):
    from review_workspace import safe_path
    return safe_path(context, "checkpoint.json")


def load(context):
    path = location(context)
    if not path.exists():
        return None
    value = coordination.read_json(path)
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or value.get("request_id") != Path(context).name
            or type(value.get("checkpoint_revision")) is not int
            or value["checkpoint_revision"] < 1):
        raise ValueError("Invalid persisted execution checkpoint")
    return value


def revision(value):
    return value.get("checkpoint_revision", 0) if value else 0


def _from_operation(context, manifest, checkpoint, current, *, draft_sha256, at, paging_artifact):
    result = dict(checkpoint)
    paging = result.pop("paging", None)
    if paging is not None:
        import review_paging
        review_paging.validate_state(paging, root=Path(context).parent.parent,
                                     identifier=Path(context).name,
                                     source_identity=source_identity(manifest))
        artifact = paging_artifact(context, paging)
        result["paging_ref"] = {
            "command": "review_session.py view", "runtime_root": str(Path(context).parent.parent),
            "request_id": Path(context).name, "artifact_scope": "context",
            "artifact": artifact, "pointer": "", "offset": 0, "page_size": 100,
            "read_only": True,
        }
        result["paging_counts"] = {name: len(paging.get(name, []))
                                   for name in ("pending", "acknowledged", "blocked")}
    elif current:
        # Legacy author updates cannot silently discard a newer paging frontier.
        for key in ("paging_ref", "paging_counts"):
            if key in current:
                result[key] = current[key]
    result.update(schema=SCHEMA, request_id=Path(context).name,
                  checkpoint_revision=revision(current) + 1, draft_sha256=draft_sha256,
                  source_identity=source_identity(manifest), at=at)
    return result


def prepare(context, manifest, checkpoint, current, *, draft_sha256, at):
    """Store immutable paging bytes during the author transaction, never in view."""
    return _from_operation(context, manifest, checkpoint, current, draft_sha256=draft_sha256, at=at,
                           paging_artifact=review_runtime.store_view)


def prepared_matches(context, manifest, checkpoint, current, *, draft_sha256, at, saved):
    """Verify a saved journal checkpoint without writing another paging artifact."""
    def existing_paging(context, value):
        artifact = hashlib.sha256(review_runtime.canonical(value)).hexdigest()
        review_runtime.load_view(context, artifact)
        return artifact

    if not isinstance(at, str) or not at or not isinstance(saved, dict):
        return False
    expected = _from_operation(context, manifest, checkpoint, current, draft_sha256=draft_sha256, at=at,
                               paging_artifact=existing_paging)
    return coordination.digest(saved) == coordination.digest(expected)


def restore(context, journal, current):
    """Finish an interrupted commit or reject a stale journal without rollback."""
    before = journal.get("checkpoint_before")
    after = journal.get("result", {}).get("checkpoint")
    if "checkpoint_before" not in journal:
        # Historical journals predate checkpoint CAS and cannot replace new state.
        if current is not None:
            raise ValueError("stale author checkpoint; read current checkpoint before retrying")
        return False
    if current == after:
        return False
    if current != before:
        raise ValueError("checkpoint revision conflict; read current checkpoint before authoring")
    return True
