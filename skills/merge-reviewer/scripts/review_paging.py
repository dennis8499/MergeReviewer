"""Caller-acknowledged, bounded continuation batches; no writes or Git activity.

``execute`` is a pure read. Only a caller that received and parsed its entire
response applies ``paging_delta`` and saves the resulting state with author.
Acknowledgement records transport delivery, never semantic review coverage.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re

SCHEMA = "MergeReviewPaging/v1"
BATCH = "MergeReviewPagingBatch/v1"
DELTA = "MergeReviewPagingDelta/v1"
LIMIT = 24 * 1024
PAGE_FIELDS = {"id", "command", "runtime_root", "request_id", "read_only", "artifact",
               "artifact_scope", "pointer", "offset", "page_size", "string_offset"}
STATE_FIELDS = {"schema", "runtime_root", "request_id", "source_identity", "pending",
                "acknowledged", "blocked", "last_delta_sha256"}
HEX64 = re.compile(r"[a-f0-9]{64}")
HEX32 = re.compile(r"[a-f0-9]{32}")


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _root(value):
    if not isinstance(value, (str, Path)) or not str(value).strip() or "\x00" in str(value):
        raise ValueError("paging runtime_root must be a nonempty path")
    return os.path.normcase(os.path.abspath(value))


def _id(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("paging request id must be nonempty text of at most 128 characters")
    return value


def normalize(request, runtime_root, request_id):
    """Validate continuation metadata, treating command as data, never executable."""
    if not isinstance(request, dict) or set(request) - PAGE_FIELDS:
        raise ValueError("continuation needs supported page arguments")
    if "command" in request and request["command"] not in ("review_session.py view", "view", "status"):
        raise ValueError("continuation command must identify a native artifact view")
    if "read_only" in request and request["read_only"] is not True:
        raise ValueError("continuation read_only must be true")
    root = _root(runtime_root)
    if "runtime_root" in request and _root(request["runtime_root"]) != root:
        raise ValueError("continuation runtime_root differs from the active request")
    if "request_id" in request and request["request_id"] != request_id:
        raise ValueError("continuation request_id differs from the active request")
    if not isinstance(request_id, str) or not HEX32.fullmatch(request_id):
        raise ValueError("paging request_id must be 32 lowercase hexadecimal characters")
    artifact = request.get("artifact")
    if not isinstance(artifact, str) or not HEX64.fullmatch(artifact):
        raise ValueError("continuation artifact must be 64 lowercase hexadecimal characters")
    scope = request.get("artifact_scope", "context")
    if scope not in ("context", "runtime"):
        raise ValueError("continuation artifact_scope must be context or runtime")
    pointer = request.get("pointer", "")
    if (not isinstance(pointer, str) or len(pointer) > 8192 or
            pointer and not pointer.startswith("/") or re.search(r"~(?![01])", pointer)):
        raise ValueError("continuation pointer must be a valid JSON pointer")
    values = {name: request.get(name, default) for name, default in
              (("offset", 0), ("page_size", 100), ("string_offset", 0))}
    if any(type(value) is not int for value in values.values()) or values["offset"] < 0 or values["string_offset"] < 0:
        raise ValueError("continuation offsets must be nonnegative integers")
    if not 1 <= values["page_size"] <= 1000:
        raise ValueError("continuation page_size must be between 1 and 1000")
    result = {"command": "review_session.py view", "runtime_root": root,
              "request_id": request_id, "read_only": True, "artifact": artifact,
              "artifact_scope": scope, "pointer": pointer, **values}
    if "id" in request:
        result["id"] = _id(request["id"])
    return result


def page_key(request, root=None, identifier=None):
    if not isinstance(request, dict):
        raise ValueError("continuation needs supported page arguments")
    value = normalize(request, root if root is not None else request.get("runtime_root"),
                      identifier if identifier is not None else request.get("request_id"))
    return digest({key: value[key] for key in ("runtime_root", "request_id", "artifact",
                  "artifact_scope", "pointer", "offset", "page_size", "string_offset")})


def new_state(runtime_root, request_id, continuations, source_identity=None):
    if not isinstance(continuations, list):
        raise ValueError("paging continuations must be an array")
    pending = []
    for index, request in enumerate(continuations):
        # Preserve malformed inputs for a recoverable per-item diagnostic.
        if not isinstance(request, dict):
            request = {"id": "invalid-" + str(index), "invalid_request": request}
        else:
            request = copy.deepcopy(request)
            request.setdefault("id", "page-" + digest(request)[:24] + "-" + str(index))
        pending.append(request)
    result = {"schema": SCHEMA, "runtime_root": _root(runtime_root), "request_id": request_id,
              "pending": pending, "acknowledged": [], "blocked": []}
    if source_identity is not None:
        result["source_identity"] = source_identity
    return validate_state(result, runtime_root, request_id)


def validate_state(state, root=None, identifier=None, source_identity=None):
    if not isinstance(state, dict) or state.get("schema") != SCHEMA or set(state) - STATE_FIELDS:
        raise ValueError("unsupported paging state")
    runtime = _root(state.get("runtime_root"))
    request_id = state.get("request_id")
    if not isinstance(request_id, str) or not HEX32.fullmatch(request_id):
        raise ValueError("paging request_id must be 32 lowercase hexadecimal characters")
    if root is not None and runtime != _root(root):
        raise ValueError("paging runtime_root differs from the active request")
    if identifier is not None and request_id != identifier:
        raise ValueError("paging request_id differs from the active request")
    if source_identity is not None and state.get("source_identity") is not None and state["source_identity"] != source_identity:
        raise ValueError("paging source_identity differs from the fixed snapshot")
    if "source_identity" in state and (not isinstance(state["source_identity"], str) or not state["source_identity"]):
        raise ValueError("paging source_identity must be nonempty text")
    if any(not isinstance(state.get(field), list) for field in ("pending", "acknowledged", "blocked")):
        raise ValueError("paging pending, acknowledged and blocked must be arrays")
    ids = [_id(row.get("id")) if isinstance(row, dict) else _id(None) for row in state["pending"]]
    if len(ids) != len(set(ids)):
        raise ValueError("paging pending IDs must be unique")
    if any(not isinstance(key, str) or not HEX64.fullmatch(key) for key in state["acknowledged"]):
        raise ValueError("paging acknowledged values must be canonical page keys")
    if len(state["acknowledged"]) != len(set(state["acknowledged"])):
        raise ValueError("paging acknowledged page keys must be unique")
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in state["blocked"]):
        raise ValueError("paging blocked entries need stable IDs")
    if "last_delta_sha256" in state and not HEX64.fullmatch(str(state["last_delta_sha256"])):
        raise ValueError("paging last_delta_sha256 must be a SHA-256")
    _bytes(state)
    return state


def protocol_continuations(page):
    """Read only view-envelope controls, never recurse through source items/text."""
    links = []
    if isinstance(page, dict):
        if isinstance(page.get("continue"), dict):
            links.append(page["continue"])
        if isinstance(page.get("drilldowns"), list):
            links.extend(link for link in page["drilldowns"] if isinstance(link, dict))
    return links


def _next_requests(page, root, identifier, current_key):
    links = []
    seen = set()
    for request in protocol_continuations(page):
        value = normalize(request, root, identifier)
        key = page_key(value)
        if key == current_key:
            raise ValueError("continuation did not advance; choose a more specific pointer")
        if key not in seen:
            value["id"] = "page-" + key
            links.append(value)
            seen.add(key)
    return links


def execute(root, identifier, state, read_page, limit=LIMIT):
    validate_state(state, root, identifier)
    if type(limit) is not int or not 1024 <= limit <= LIMIT:
        raise ValueError("paging response limit must be between 1024 and 24576 bytes")
    pending = state["pending"]
    ack = set(state["acknowledged"])
    seen, cache, results = {}, {}, []
    delta = {"schema": DELTA, "base_state_sha256": digest(state), "processed_ids": [],
             "acknowledged": [], "new_pending": [], "blocked": []}
    accepted_deltas = [delta]
    queued_keys = set()
    for request in pending:
        try:
            queued_keys.add(page_key(request, root, identifier))
        except (ValueError, TypeError):
            pass

    def response(rows, changes, error=None):
        remaining = len(pending) - len(changes["processed_ids"]) + len(changes["new_pending"])
        blocked_count = len(state["blocked"]) + len(changes["blocked"])
        value = {"schema": BATCH, "request_id": identifier, "results": rows, "paging_delta": changes,
                 "complete": len(changes["processed_ids"]) == len(pending),
                 "next_index": len(changes["processed_ids"]) if len(changes["processed_ids"]) < len(pending) else None,
                 "pending_count": remaining, "blocked_count": blocked_count,
                 "view_complete": remaining == 0 and blocked_count == 0,
                 "artifact_load_count": len(cache), "git_commands": 0}
        if error:
            value.update(error=error, code="evidence.paging-page-too-large", recoverable=True,
                         terminal=False)
        return value

    def fits(value):
        # Native CLI print may translate its final newline to CRLF on Windows.
        return len(_bytes(value)) + 2 <= limit

    def finish(error=None):
        nonlocal delta
        value = response(results, delta, error)
        # A tentative read that did not fit can still grow the shared cache;
        # e.g. 9 -> 10 loads adds a metadata digit after the last accepted row.
        # Re-check the actual final envelope and return only an acknowledged
        # prefix. The omitted requests stay pending in the unchanged input.
        while not fits(value):
            if not results:
                raise ValueError("paging control information exceeds the response limit")
            results.pop()
            accepted_deltas.pop()
            delta = accepted_deltas[-1]
            value = response(results, delta, error)
        return value

    for request in pending:
        identifier_in_batch = request["id"]
        changes = copy.deepcopy(delta)
        changes["processed_ids"].append(identifier_in_batch)
        row = {"id": identifier_in_batch}
        key = None
        try:
            value = normalize(request, root, identifier)
            key = page_key(value)
            if key in ack:
                row.update(status="already-returned", page_key=key)
            elif key in seen:
                row.update(status="duplicate", page_key=key, duplicate_of=seen[key])
            else:
                page, scope = read_page(root, identifier, value["artifact"], scope=value["artifact_scope"],
                                       pointer=value["pointer"], offset=value["offset"],
                                       page_size=value["page_size"], string_offset=value["string_offset"], cache=cache)
                if scope != value["artifact_scope"]:
                    raise ValueError("resolved artifact scope differs from continuation scope")
                page = copy.deepcopy(page)
                # The genuine controls share objects with nested preview links in
                # the native view result; normalize these before measuring bytes.
                for link in protocol_continuations(page):
                    link.update(command="review_session.py view", read_only=True)
                links = _next_requests(page, root, identifier, key)
                row.update(status="returned", page_key=key, result=page)
                changes["acknowledged"].append(key)
                new_keys = {page_key(link) for link in changes["new_pending"]}
                for link in links:
                    link_key = page_key(link)
                    if link_key not in ack and link_key not in queued_keys and link_key not in new_keys:
                        changes["new_pending"].append(link)
                        new_keys.add(link_key)
        except (ValueError, KeyError, IndexError, TypeError, OSError) as exc:
            message = str(exc)[:360]
            if "response limit" in message:
                error = "Page exceeds the response budget; reduce page_size or use a more specific pointer."
                return finish(error)
            error = {"id": identifier_in_batch, "code": "evidence.invalid-view-request",
                     "error": message, "recoverable": True}
            row.update(error)
            changes["blocked"].append(error)
        candidate = response(results + [row], changes)
        if not fits(candidate):
            if not results:
                return finish("Page and continuation controls exceed the response budget; reduce page_size or narrow the pointer.")
            break
        results.append(row)
        delta = changes
        accepted_deltas.append(delta)
        if row.get("status") == "returned":
            seen[key] = identifier_in_batch
    return finish()


def apply_delta(state, delta):
    """Acknowledge a successfully received batch, rejecting stale/reordered deltas."""
    validate_state(state)
    allowed = {"schema", "base_state_sha256", "processed_ids", "acknowledged", "new_pending", "blocked"}
    if not isinstance(delta, dict) or delta.get("schema") != DELTA or set(delta) != allowed:
        raise ValueError("unsupported paging delta")
    fingerprint = digest(delta)
    if state.get("last_delta_sha256") == fingerprint:
        return copy.deepcopy(state)
    if delta["base_state_sha256"] != digest(state):
        raise ValueError("paging delta does not match the current state")
    if any(not isinstance(delta[field], list) for field in ("processed_ids", "acknowledged", "new_pending", "blocked")):
        raise ValueError("paging delta fields must be arrays")
    processed = delta["processed_ids"]
    if processed != [row["id"] for row in state["pending"][:len(processed)]]:
        raise ValueError("paging delta must process an ordered prefix of pending IDs")
    original = {row["id"]: row for row in state["pending"][:len(processed)]}
    possible = set()
    for request in original.values():
        try:
            possible.add(page_key(request, state["runtime_root"], state["request_id"]))
        except (ValueError, TypeError):
            pass
    if any(not isinstance(key, str) or key not in possible for key in delta["acknowledged"]):
        raise ValueError("paging delta acknowledges an unprocessed page")
    blocked_ids = {row.get("id") for row in delta["blocked"] if isinstance(row, dict)}
    returned_keys = set(state["acknowledged"]) | set(delta["acknowledged"])
    for identifier, request in original.items():
        if identifier in blocked_ids:
            continue
        try:
            accounted = page_key(request, state["runtime_root"], state["request_id"]) in returned_keys
        except (ValueError, TypeError):
            accounted = False
        if not accounted:
            raise ValueError("processed paging request needs acknowledgement or a blocked result")
    result = copy.deepcopy(state)
    result["pending"] = result["pending"][len(processed):]
    result["acknowledged"] = list(dict.fromkeys(result["acknowledged"] + delta["acknowledged"]))
    for error in delta["blocked"]:
        if (not isinstance(error, dict) or error.get("id") not in original or
                not isinstance(error.get("error"), str) or not isinstance(error.get("code"), str)):
            raise ValueError("paging delta blocked entry does not name a processed request")
        result["blocked"].append({**copy.deepcopy(error), "request": copy.deepcopy(original[error["id"]])})
    queued_ids = {row["id"] for row in result["pending"]}
    queued_keys = set()
    for row in result["pending"]:
        try:
            queued_keys.add(page_key(row, state["runtime_root"], state["request_id"]))
        except (ValueError, TypeError):
            pass
    for request in delta["new_pending"]:
        value = normalize(request, state["runtime_root"], state["request_id"])
        key = page_key(value)
        value.setdefault("id", "page-" + key)
        if key in result["acknowledged"] or key in queued_keys:
            continue
        if value["id"] in queued_ids:
            raise ValueError("paging continuation reuses an existing pending ID")
        result["pending"].append(value)
        queued_ids.add(value["id"])
        queued_keys.add(key)
    result["last_delta_sha256"] = fingerprint
    return validate_state(result)
