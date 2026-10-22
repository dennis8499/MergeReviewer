"""start / ordered step / finish facade over the existing review helpers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

import review_coordinator as coordination
import review_runtime
import review_session

SCHEMA = "MergeReviewSession/v1"
LIMIT = 24 * 1024
TERMINAL = {"published", "failed", "cancelled"}


def now():
    return datetime.now(timezone.utc).isoformat()


def resolve_runtime(workspace=None, explicit=None, create=False):
    if explicit:
        root = Path(explicit)
    elif os.environ.get("MERGE_REVIEW_RUNTIME_DIR"):
        root = Path(os.environ["MERGE_REVIEW_RUNTIME_DIR"])
    elif workspace:
        workspace = Path(workspace).expanduser().resolve()
        if (workspace / ".git").exists():
            raise ValueError("A single-Repo workspace requires --runtime-root outside every Git Repo.")
        root = workspace / ".review-runtime"
    else:
        raise ValueError("Use --workspace or --runtime-root to locate this request.")
    return coordination.runtime(root, create=create)


def job_path(root, identifier):
    if not coordination.IDENTIFIER.fullmatch(identifier or ""):
        raise ValueError("request_id must be 32 hexadecimal characters.")
    return root / "jobs" / (identifier + ".json")


def update(root, identifier, **fields):
    path = job_path(root, identifier)
    with coordination.FileLock(path.with_suffix(".state.lock")):
        job = coordination.read_json(path)
        job.update(fields, updated_at=now())
        coordination.atomic(path, job)
        return job


def job_context(root, identifier, job):
    expected = root / "sessions" / identifier
    if (job.get("schema") != SCHEMA or job.get("request_id") != identifier
            or Path(job.get("runtime_root", "")) != root
            or job.get("context_dir") and Path(job["context_dir"]) != expected):
        raise ValueError("Session ownership differs from this request ID.")
    return expected


def status(root, identifier):
    job = coordination.read_json(job_path(root, identifier))
    if job.get("schema") != SCHEMA or job.get("request_id") != identifier:
        raise ValueError("Session identity differs.")
    result = {key: value for key, value in job.items() if key not in ("input_digest",)}
    context = job_context(root, identifier, job)
    if job["state"] == "published" or job.get("planned_report") and Path(job["planned_report"]).is_file():
        import report_receipt
        result["publication"] = report_receipt.verify(job)
        result["review_outcome"] = report_receipt.saved_review(job)
        result.update({key: result["review_outcome"][key]
                       for key in ("review_complete", "review_status", "gaps", "remaining")})
    if job["state"] in ("ready", "reviewing") and (context / "manifest.json").is_file():
        import review_workspace as workspace
        manifest = workspace.load(context)
        draft_file = context / "draft.json"
        try:
            draft = coordination.read_json(draft_file)
        except (OSError, ValueError):
            draft = {}
        result["resume"] = {"selected_repo": manifest["selected_repo"],
                            "repositories": repo_index(manifest),
                            "required_coverage": manifest["required_coverage"],
                            "contract_scope_policy": manifest.get("contract_scope_policy"),
                            "contracts": [{"id": c["id"], "symbol": c["symbol"], "search_scope": c.get("search_scope", "workspace")}
                                          for c in manifest["changed_contracts"]],
                            "operations": manifest.get("operations", {}),
                            "remaining": remaining(draft),
                            "diagnostics": diagnostics(draft, manifest),
                            "network_activity": manifest["network_activity"],
                            "sync_activity": manifest.get("sync_activity", [])}
    return result


def repo_index(manifest):
    return [{"repo": item["id"], "name": item["name"], "role": item["role"], "state": item["state"],
             "sources": {name: {"ref": source["ref"]} for name, source in item["sources"].items()}}
            for item in manifest["repositories"]]


def draft_template(manifest):
    return {"schema": "merge-reviewer-workspace-result/v3", "report_layout": "engineering-review/v1",
            "summary": "",
            "coverage": [{**row, "status": "not-reviewed", "reason": "尚未完成語意審查。",
                          "evidence": []} for row in manifest["required_coverage"]],
            "merge_parent_review": [{"id": item["id"], "status": "not-reviewed",
                                     "reason": "尚未完成必要 parent 對照。", "evidence": []}
                                    for item in manifest.get("required_merge_views", [])],
            "repository_coverage": [{"repo": item["id"],
                                     "status": "seed" if item["id"] == manifest["selected_repo"] else "unresolved",
                                     "reason": "使用者指定的審查起點。" if item["id"] == manifest["selected_repo"]
                                               else "尚未完成契約追查。",
                                     "search_ids": []} for item in manifest["repositories"]],
            "impact_coverage": [], "boundary_reviews": [], "findings": [], "pending_confirmations": [], "next_steps": [],
            "tests_executed": [], "tests_not_executed": ["本次尚未執行專案測試。"], "limitations": []}


def remaining(draft):
    if not isinstance(draft, dict):
        return {}
    def rows(field):
        value = draft.get(field, [])
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    return {"changes": [row for row in rows("coverage") if row.get("status") != "reviewed"],
            "parents": [row for row in rows("merge_parent_review") if row.get("status") != "reviewed"],
            "repositories": [row for row in rows("repository_coverage") if row.get("status") == "unresolved"],
            "boundaries": [row for row in rows("boundary_reviews") if row.get("outcome") == "unresolved"],
            "pending_confirmation_count": len(rows("pending_confirmations"))}


def diagnostics(draft, manifest):
    import review_diagnostics
    try:
        return review_diagnostics.inspect(draft, manifest)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        return {"errors": [{"code": "authoring.invalid-structure", "pointer": "", "path": "draft", "message": str(exc)}],
                "gaps": [], "ready_for_finish": False, "evidence_verified": False,
                "evidence_validation": "deferred-to-finish", "git_commands": 0}


def check(root, identifier):
    """Collect authoring gaps without running Git or marking evidence verified."""
    import review_workspace as workspace
    job = coordination.read_json(job_path(root, identifier))
    context = job_context(root, identifier, job)
    if job["state"] == "published":
        published = status(root, identifier)
        complete = published["review_outcome"]["review_complete"] is True
        return {**published, "errors": [], "ready_for_finish": False, "publication_complete": True,
                "evidence_verified": False, "evidence_validation": "publication-receipt-only",
                "git_commands": 0}, 0 if complete else 3
    manifest = workspace.load(context)
    try:
        draft = coordination.read_json(context / "draft.json")
    except (OSError, ValueError) as exc:
        return {"request_id": identifier, "errors": [{"code": "authoring.invalid-json", "pointer": "", "path": "draft", "message": str(exc)}],
                "ready_for_finish": False, "evidence_verified": False, "git_commands": 0}, 3
    value = diagnostics(draft, manifest)
    return {"request_id": identifier, "context_retained": True, **value}, 0 if value["ready_for_finish"] else 3


def preflight(path):
    """Exercise the actual read/write/replace permissions, before any transport."""
    path = coordination.plain(path)
    path.mkdir(parents=True, exist_ok=True)
    probe = path / (".review-probe-" + uuid.uuid4().hex)
    moved = probe.with_suffix(".checked")
    try:
        probe.write_bytes(b"review")
        if probe.read_bytes() != b"review":
            raise OSError("Review preflight read differs.")
        os.replace(probe, moved)
    finally:
        probe.unlink(missing_ok=True)
        moved.unlink(missing_ok=True)


def prune_jobs(root):
    cutoff = time.time() - 86400
    for path in (root / "jobs").glob("*.json"):
        if not coordination.IDENTIFIER.fullmatch(path.stem):
            continue
        # A live owner may atomically replace its job file. In particular on
        # Windows, maintenance must not race that replacement with a reader.
        lock = coordination.FileLock(path.with_suffix(".owner.lock"))
        if not lock.acquire(blocking=False):
            continue
        try:
            try:
                value = coordination.read_json(path)
            except (ValueError, OSError):
                continue  # An unrelated unreadable summary cannot fail capture.
            if (not isinstance(value, dict) or value.get("schema") != SCHEMA
                    or value.get("request_id") != path.stem):
                continue
            if value.get("state") in TERMINAL and value.get("finished_at_epoch", time.time()) < cutoff:
                # Evidence is cleaned only through its separate owner marker.
                if not value.get("context_dir") or not Path(value["context_dir"]).exists():
                    path.unlink()
                    (root / "jobs" / (path.stem + ".cancel")).unlink(missing_ok=True)
        finally:
            lock.release()


def start(args):
    import git_review_context as api
    import review_workspace as workspace
    root = resolve_runtime(args.workspace, args.runtime_root, create=True)
    identifier = args.request_id or uuid.uuid4().hex
    args.request_id = identifier
    path = job_path(root, identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    inputs = {"workspace": str(Path(args.workspace).resolve()), "project": args.project,
              "base": args.base, "head": args.head,
              "workspace_file": str(Path(args.workspace_file).resolve()) if args.workspace_file else None,
              "auth_scope": args.auth_scope, "refresh": args.refresh, "no_fetch": args.no_fetch}
    fingerprint = coordination.digest(inputs)
    lock = coordination.FileLock(path.with_suffix(".owner.lock"))
    if not lock.acquire(blocking=False):
        return status(root, identifier)
    context = None
    active = None
    manifest = None
    try:
        if path.exists():
            existing = coordination.read_json(path)
            if existing.get("input_digest") != fingerprint:
                raise ValueError("request_id already belongs to different inputs.")
            return status(root, identifier)
        job = {"schema": SCHEMA, "request_id": identifier, "runtime_root": str(root),
               "state": "running", "phase": "preflight", "started_at": now(),
               "updated_at": now(), "pid": os.getpid(), "inputs": inputs, "input_digest": fingerprint}
        coordination.atomic(path, job)
        def notify(phase, fields):
            update(root, identifier, phase=phase, **fields)
            print(json.dumps({"request_id": identifier, "phase": phase, **fields}, ensure_ascii=False),
                  file=sys.stderr, flush=True)
        active = coordination.Coordination(root, identifier, notify, args.refresh, args.auth_scope)
        notify("preflight", {"runtime_root": str(root)})
        seed, repositories = api.choose_repository(Path(inputs["workspace"]),
                                                    Path(inputs["workspace_file"]) if inputs["workspace_file"] else None,
                                                    args.project)
        if any(root.is_relative_to(repo.resolve()) for repo in repositories):
            raise ValueError("Review runtime must be outside every declared Git Repo.")
        inputs["project"] = str(seed.resolve())
        update(root, identifier, inputs=inputs)
        preflight(root)
        preflight(seed / "review-reports")
        if any(api.remote_target_for_ref(seed, value) for value in (args.base, args.head)):
            preflight(coordination.git_network_lease.runtime_root())
        context = review_session.create_session(runtime_root=root, request_id=identifier)
        update(root, identifier, context_dir=str(context), draft_path=str(context / "draft.json"))
        arguments = ["--workspace", inputs["workspace"], "--project", args.project,
                     "--base", args.base, "--head", args.head, "--git-timeout", str(args.git_timeout)]
        if args.workspace_file:
            arguments.extend(["--workspace-file", args.workspace_file])
        if args.no_fetch:
            arguments.append("--no-fetch")
        capture = api.parse_args(arguments)
        capture._prepared_context = str(context)
        began = time.perf_counter()
        with active.activate():
            manifest = api.build_manifest(capture)
            active.check()
            active.phase("review")
        coordination.atomic(context / "draft.json", draft_template(manifest))
        selected = workspace.repository(manifest, manifest["selected_repo"])
        patches = []
        for patch in sorted((context / f"repos/{selected['id']}/native").glob("*.patch")):
            patches.append({"path": patch.relative_to(context).as_posix(),
                            "bytes": patch.stat().st_size})
        instructions = workspace.instructions(context, manifest, selected["id"], None, api)
        guide = {"schema": "MergeReviewStart/v1", "request_id": identifier, "runtime_root": str(root),
                 "state": "ready", "context_dir": str(context), "draft_path": str(context / "draft.json"),
                 "selected_repo": manifest["selected_repo"], "repositories": repo_index(manifest),
                 "contract_scope_policy": manifest.get("contract_scope_policy"),
                 "required_coverage": manifest["required_coverage"],
                 "required_merge_views": manifest["required_merge_views"], "patches": patches,
                 "instructions": instructions["instructions"], "context_gaps": manifest["context_gaps"],
                 "network_activity": manifest["network_activity"], "sync_activity": manifest.get("sync_activity", []),
                 "pack_cache_hits": active.pack_hits,
                 "step": {"requests_file": "UTF-8 JSON array; every operation needs id and op",
                          "operations": ["read", "instructions", "inventory", "patch", "register-contract", "search", "view"],
                          "read": {"id": "read-1", "op": "read", "repo": selected["id"], "source": "head",
                                   "path": "<fixed relative path>", "around_line": 1, "context_lines": 20},
                          "contract": {"id": "<unique C-id>", "root": "<one required_coverage row>",
                                       "kind": "behavior", "search_scope": "workspace", "symbol": "<exact frozen symbol>",
                                       "evidence": {"repo": selected["id"], "source": "head", "ref": "<fixed SHA>",
                                                    "path": "<path>", "line_start": 1, "line_end": 1},
                                       "search_terms": [{"query": "<exact symbol>", "reason": "<contract meaning>"},
                                                        {"query": "<different frozen anchor term>", "reason": "<meaning>"}]},
                          "search": {"id": "search-1", "op": "search", "contract": "<registered C-id>",
                                     "query": "<registered term>", "repo": ["<Repo IDs>"]}}}
        coordination.atomic(context / "start.json", guide)
        update(root, identifier, state="ready", phase="review", selected_repo=selected["id"],
               network_activity=manifest["network_activity"], sync_activity=manifest.get("sync_activity", []),
               pack_cache_hits=active.pack_hits,
               metrics={"capture_seconds": time.perf_counter() - began, "phase_seconds": active.timings})
        prune_jobs(root)
        return guide
    except Exception as exc:
        if path.exists():
            update(root, identifier, state="cancelled" if active and active.cancelled() else "failed",
                   phase="terminal", error=coordination.safe_error(exc), finished_at_epoch=time.time(),
                   network_activity=getattr(exc, "network_activity",
                                            manifest["network_activity"] if manifest else api.empty_network_activity()),
                   sync_activity=[{key: value for key, value in item.items() if key != "receipt"}
                                  for item in active.syncs] if active else [])
        if context and context.exists():
            review_session.cleanup_session(context)
        raise
    finally:
        if active:
            active.release_syncs()
        lock.release()


def artifact_view(context, artifact, pointer="", offset=0, page_size=100, string_offset=0):
    value = review_runtime.load_view(context, artifact)
    if not isinstance(pointer, str) or pointer and not pointer.startswith("/"):
        raise ValueError("view pointer must be a JSON pointer.")
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    if type(offset) is not int or offset < 0 or type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("Invalid view page.")
    if isinstance(value, list):
        items = value[offset:offset + page_size]
        while len(review_runtime.canonical(items)) > LIMIT // 2 and len(items) > 1:
            items = items[:max(1, len(items) // 2)]
        end = offset + len(items)
        return {"artifact": artifact, "pointer": pointer, "items": items,
                "total": len(value), "next_offset": end if end < len(value) else None,
                "view_complete": end >= len(value)}
    if isinstance(value, str):
        if type(string_offset) is not int or string_offset < 0:
            raise ValueError("Invalid string_offset.")
        text = value[string_offset:string_offset + 2048]
        end = string_offset + len(text)
        return {"artifact": artifact, "pointer": pointer, "text": text,
                "next_string_offset": end if end < len(value) else None, "view_complete": end >= len(value)}
    return {"artifact": artifact, "pointer": pointer, "value": value,
            "fields": list(value) if isinstance(value, dict) else None}


def step_projection(value):
    """Keep every read line/match; reuse supplied inputs and shared content."""
    if not isinstance(value, dict) or not isinstance(value.get("operations"), list):
        return None
    projected, contents = [], {}
    for operation in value["operations"]:
        result = operation.get("result")
        if not isinstance(result, dict):
            projected.append(operation)
            continue
        if all(key in result for key in ("lines", "sha256", "path", "ref")):
            shared = review_runtime.shared_reads({"reads": [result]})
            contents.update(shared["contents"])
            result = shared["reads"][0]
        elif "anchor_sha256" in result and "root" in result:
            result = {key: result[key] for key in ("id", "parent_ids", "anchor_sha256") if key in result}
            result["input_fields_reused"] = True
        elif isinstance(result.get("searches"), list):
            rows = result["searches"]
            common = {key: rows[0][key] for key in ("query", "contract_id", "source") if rows and key in rows[0]
                      and all(row.get(key) == rows[0][key] for row in rows)}
            stripped = []
            for row in rows:
                redundant_output = (isinstance(row.get("output"), str)
                                    and row["output"].splitlines() == row.get("matches"))
                stripped.append({key: item for key, item in row.items()
                                 if key not in common and not (key == "output" and redundant_output)})
            result = {**result, **common, "searches": stripped}
        projected.append({**operation, "result": result})
    return {**value, "operations": projected, "contents": contents}


def compact(value, context):
    if len(review_runtime.canonical(value)) <= LIMIT:
        return value
    artifact = review_runtime.store_view(context, value)
    projected = step_projection(value)
    if projected is not None:
        lean = {"schema": "MergeReviewEvidenceStep/v1", "artifact": artifact,
                "evidence_complete": True, "projection_complete": True, **projected}
        if len(review_runtime.canonical(lean)) <= LIMIT:
            return lean
    # Bound every level, including thousands of matches, long lines and metadata.
    def sketch(item, pointer="", depth=0):
        if isinstance(item, str):
            if len(item.encode("utf-8")) > 800:
                return {"omitted": True, "pointer": pointer, "characters": len(item)}
            return item
        if isinstance(item, list):
            return {"total": len(item), "pointer": pointer,
                    "sample": [sketch(v, pointer + "/" + str(i), depth+1) for i, v in enumerate(item[:3])]}
        if isinstance(item, dict):
            if depth > 8:
                return {"fields": list(item)[:12], "pointer": pointer}
            return {k: sketch(v, pointer + "/" + k.replace("~", "~0").replace("/", "~1"), depth+1)
                    for k, v in item.items()}
        return item
    result = {"schema": "MergeReviewEvidenceSummary/v1", "artifact": artifact,
              "evidence_complete": True, "view_complete": False, "summary": sketch(value),
              "continue": {"op": "view", "artifact": artifact, "pointer": "", "offset": 0, "page_size": 100}}
    if isinstance(value, dict) and "evidence_verified" in value:
        result["evidence_verified"] = value["evidence_verified"]
        result["ready_for_finish"] = value.get("ready_for_finish", False)
    if len(review_runtime.canonical(result)) > LIMIT:
        result["summary"] = {"fields": list(value) if isinstance(value, dict) else None,
                             "count": len(value)}
    return result


def step_file(root, identifier, requested):
    """Treat an editable operations file failure as an incomplete owned step."""
    return step(root, identifier, None, requests_file=requested)


def step(root, identifier, operations, *, requests_file=None):
    import git_review_context as api
    import review_workspace as workspace
    path = job_path(root, identifier)
    with coordination.FileLock(path.with_suffix(".owner.lock")):
        job = coordination.read_json(path)
        if job["state"] not in ("ready", "reviewing"):
            raise ValueError("Session is not ready for semantic review.")
        active = coordination.Coordination(root, identifier)
        context = job_context(root, identifier, job)
        started = time.perf_counter()
        try:
            active.check()
            review_session.validate_session(context)
            with active.activate(), review_runtime.writer(context):
                manifest = workspace.load(context)
                input_error = None
                if operations is None:
                    try:
                        if requests_file is None:
                            raise ValueError("step requires --requests-file.")
                        operations = coordination.read_json(requests_file)
                    except (OSError, ValueError) as exc:
                        input_error = coordination.safe_error(exc)
                result = ({"operations": [], "complete": False, "next_index": 0,
                           "error": input_error, "code": "authoring.invalid-requests-file",
                           "pointer": "/requests_file", "terminal": False, "recoverable": True,
                           "context_retained": True} if input_error is not None else
                          workspace.ordered_step(context, manifest, operations, api))
                if not result["complete"] and not active.cancelled():
                    import workspace_report
                    workspace_report.validate_captured_evidence(manifest, context)
        except Exception as exc:
            update(root, identifier, state="cancelled" if active.cancelled() else "failed",
                   phase="terminal", error=coordination.safe_error(exc), finished_at_epoch=time.time())
            review_session.cleanup_session(context)
            raise
        if active.cancelled():
            review_session.cleanup_session(context)
            update(root, identifier, state="cancelled", phase="terminal", finished_at_epoch=time.time())
            return {**result, "complete": False, "cancelled": True}
        update(root, identifier, state="reviewing", phase="input-repair" if input_error is not None else "review",
               last_step={"complete": result["complete"], "next_index": result["next_index"]},
               metrics={**job.get("metrics", {}), "read_search_seconds":
                        job.get("metrics", {}).get("read_search_seconds", 0) + time.perf_counter() - started})
        return result


def draft_errors(draft):
    errors = []
    if not isinstance(draft, dict):
        return ["draft must be an object"]
    if draft.get("schema") != "merge-reviewer-workspace-result/v3":
        errors.append("schema must be merge-reviewer-workspace-result/v3")
    if draft.get("report_layout") != "engineering-review/v1":
        errors.append("report_layout must be engineering-review/v1")
    if not isinstance(draft.get("summary"), str) or not draft["summary"].strip():
        errors.append("summary is required")
    for name in ("coverage", "impact_coverage", "repository_coverage", "findings", "next_steps",
                 "merge_parent_review", "tests_executed", "tests_not_executed", "limitations"):
        if not isinstance(draft.get(name), list):
            errors.append(name + " must be an array")
    for index, finding in enumerate(draft.get("findings", []) if isinstance(draft.get("findings"), list) else []):
        for field in ("operation_scenario", "technical_trigger", "technical_explanation",
                      "impact_scope", "engineering_fix"):
            value = finding.get(field) if isinstance(finding, dict) else None
            if not isinstance(value, str) or not value.strip() or value.strip() == "未提供":
                errors.append(f"findings[{index}].{field} is required")
    return errors


def finish(root, identifier):
    import review_report
    import review_workspace as workspace
    import workspace_report
    import report_receipt
    path = job_path(root, identifier)
    with coordination.FileLock(path.with_suffix(".owner.lock")):
        job = coordination.read_json(path)
        context = job_context(root, identifier, job)
        planned = job.get("planned_report")
        def clean_published():
            try:
                if context.is_dir():
                    review_session.cleanup_session(context)
                update(root, identifier, cleanup_pending=False, cleanup_error=None)
            except (OSError, ValueError) as exc:
                update(root, identifier, cleanup_pending=True, cleanup_error=coordination.safe_error(exc))
            return status(root, identifier), 0
        if job["state"] == "published":
            report_receipt.verify(job)
            return clean_published()
        if planned and Path(planned).is_file():
            report_receipt.verify(job)
            update(root, identifier, state="published", phase="complete", report_path=planned,
                   finished_at_epoch=time.time())
            return clean_published()
        if job["state"] not in ("ready", "reviewing", "validating"):
            raise ValueError("Session cannot be published.")
        started = time.perf_counter()
        def staged(output, receipt):
            if (root / "jobs" / (identifier + ".cancel")).exists():
                raise coordination.Cancelled("Review cancelled before the report commit.")
            update(root, identifier, planned_report=str(output), report_sha256=receipt["report_sha256"],
                   report_receipt=receipt, review_outcome=receipt["review_outcome"])
        def published(output):
            update(root, identifier, state="published", phase="complete", report_path=str(output),
                   finished_at_epoch=time.time(), metrics={**job.get("metrics", {}),
                       "validation_publish_seconds": time.perf_counter() - started})
        try:
            if (root / "jobs" / (identifier + ".cancel")).exists():
                raise coordination.Cancelled("Review cancelled before publication.")
            draft_path = context / "draft.json"
            try:
                draft = coordination.read_json(draft_path)
            except (OSError, ValueError) as exc:
                draft, errors = {}, [f"draft input: {coordination.safe_error(exc)}"]
            else:
                errors = draft_errors(draft)
                # This inspection reads saved JSON only. Final evidence verification
                # remains mandatory after authoring errors have been repaired.
                inspected = diagnostics(draft, workspace.load(context))
                errors.extend(f"{row['path']}: {row['message']}" for row in inspected["errors"])
                errors = list(dict.fromkeys(errors))
            if errors:
                workspace_report.validate_captured_evidence(workspace.load(context), context)
                update(root, identifier, state="reviewing", phase="draft-repair", errors=errors)
                return {"request_id": identifier, "context_retained": True, "errors": errors,
                        "remaining": remaining(draft)}, 3
            update(root, identifier, state="validating", phase="validation",
                   draft_sha256=hashlib.sha256(draft_path.read_bytes()).hexdigest())
            with review_runtime.writer(context):
                review_report.publish_report(context, draft_path, retain_on_validation_error=True,
                                             on_stage_receipt=staged, on_published=published, cleanup_context=False)
            return clean_published()
        except workspace_report.DraftValidationError as exc:
            update(root, identifier, state="reviewing", phase="draft-repair", errors=[str(exc)])
            return {"request_id": identifier, "context_retained": True, "errors": [str(exc)]}, 3
        except Exception as exc:
            # A published receipt survives cleanup failures and may be retried.
            saved = coordination.read_json(path)
            if saved["state"] == "published":
                raise
            if saved.get("planned_report") and Path(saved["planned_report"]).is_file():
                report_receipt.verify(saved)
                # The link commit completed; keep its evidence and receipt for retry.
                raise
            update(root, identifier, state="cancelled" if isinstance(exc, coordination.Cancelled) else "failed",
                   phase="terminal", error=coordination.safe_error(exc),
                   finished_at_epoch=time.time())
            if context.exists():
                review_session.cleanup_session(context)
            raise


def cancel(root, identifier):
    path = job_path(root, identifier)
    job = coordination.read_json(path)
    job_context(root, identifier, job)
    if job["state"] in TERMINAL:
        return status(root, identifier)
    coordination.atomic(root / "jobs" / (identifier + ".cancel"), {"requested_at": now()})
    lock = coordination.FileLock(path.with_suffix(".owner.lock"))
    if lock.acquire(blocking=False):
        try:
            job = coordination.read_json(path)
            if job["state"] in TERMINAL:
                return status(root, identifier)
            context = job_context(root, identifier, job)
            if job.get("context_dir") and context.exists():
                review_session.cleanup_session(context)
            update(root, identifier, state="cancelled", phase="terminal", finished_at_epoch=time.time())
        finally:
            lock.release()
    return status(root, identifier)


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("command", choices=("start", "step", "check", "finish", "status", "cancel"))
    value.add_argument("--workspace")
    value.add_argument("--workspace-file")
    value.add_argument("--runtime-root")
    value.add_argument("--request-id")
    value.add_argument("--project")
    value.add_argument("--base")
    value.add_argument("--head")
    value.add_argument("--requests-file", type=Path)
    value.add_argument("--refresh", action="store_true")
    value.add_argument("--auth-scope", default=os.environ.get("GITLAB_WORKSPACE_AUTH_SCOPE", ""))
    value.add_argument("--no-fetch", action="store_true")
    value.add_argument("--git-timeout", type=float, default=180)
    return value


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    code = 0
    try:
        if args.command == "start":
            if not all((args.workspace, args.project, args.base, args.head)):
                raise ValueError("start requires workspace, project, base and head.")
            result = start(args)
            if result.get("state") in ("failed", "cancelled"):
                code = 2
            context = Path(result.get("context_dir", "")) if result.get("context_dir") else None
        else:
            root = resolve_runtime(args.workspace, args.runtime_root)
            if args.command == "status":
                result = status(root, args.request_id)
            elif args.command == "cancel":
                result = cancel(root, args.request_id)
            elif args.command == "step":
                result = step_file(root, args.request_id, args.requests_file)
                code = 0 if result["complete"] else (2 if result.get("cancelled") else 3)
            elif args.command == "check":
                result, code = check(root, args.request_id)
            else:
                result, code = finish(root, args.request_id)
            job = coordination.read_json(job_path(root, args.request_id))
            context = Path(job["context_dir"]) if job.get("context_dir") else None
        if context and context.exists():
            result = compact(result, context)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return code
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": coordination.safe_error(exc), "request_id": args.request_id,
                          "terminal": True}, ensure_ascii=False), file=sys.stderr)
        return 2
