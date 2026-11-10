"""start / ordered step / finish facade over the existing review helpers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

import review_coordinator as coordination
import review_runtime
import review_session
import report_publication
from review_validation import fixed_line_count_key
from review_views import artifact_view, view_continuation

SCHEMA = "MergeReviewSession/v1"
LIMIT = 24 * 1024
TERMINAL = {"published", "failed", "cancelled"}


class MeasuredFileLock:
    """FileLock that exposes acquisition delay for per-attempt metrics."""

    def __init__(self, path):
        self.lock = coordination.FileLock(path)
        self.wait_seconds = 0.0

    def __enter__(self):
        started = time.perf_counter()
        self.lock.acquire()
        self.wait_seconds = time.perf_counter() - started
        return self

    def __exit__(self, *_):
        self.lock.release()


def now():
    return datetime.now(timezone.utc).isoformat()


def _line_count_key(manifest, request):
    """Bind remembered line counts to the exact fixed Repo/source/blob."""
    return fixed_line_count_key(manifest, request)


def runtime_path(workspace=None, explicit=None):
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
    return coordination.plain(Path(root).expanduser().absolute()).resolve()


def resolve_runtime(workspace=None, explicit=None, create=False):
    return coordination.runtime(runtime_path(workspace, explicit), create=create)


class LaunchPreflightError(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__("; ".join(row["message"] for row in result["errors"]))


def launch_preflight(args):
    """Inspect launch inputs without creating a runtime, job, lock or Git ref."""
    import git_review_context as api
    errors, canonical = [], {}

    def error(field, exc):
        errors.append({"code": "launch.invalid-" + field.replace("_", "-"),
                       "pointer": "/" + field, "message": coordination.safe_error(exc)[:2000]})

    for field in ("workspace", "project", "base", "head"):
        if not isinstance(getattr(args, field, None), str) or not getattr(args, field).strip():
            error(field, "A nonempty value is required.")
    if args.request_id is not None and not coordination.IDENTIFIER.fullmatch(args.request_id):
        error("request_id", "request_id must be 32 hexadecimal characters.")
    if not isinstance(args.git_timeout, (int, float)) or not 0 < args.git_timeout < float("inf"):
        error("git_timeout", "git-timeout must be positive and finite.")
    if args.draft_format not in ("v3", "compact"):
        error("draft_format", "draft format must be v3 or compact")
    declared, workspace_file, repositories, selected, root = None, None, [], None, None
    if args.workspace:
        try:
            declared = Path(args.workspace).expanduser().resolve(strict=True)
            if not declared.is_dir():
                raise ValueError("workspace must be an existing directory")
            canonical["workspace"] = str(declared)
        except (ValueError, OSError) as exc:
            error("workspace", exc)
            declared = None
    if args.workspace_file:
        try:
            workspace_file = Path(args.workspace_file).expanduser().resolve(strict=True)
            if not workspace_file.is_file():
                raise ValueError("workspace-file must be an existing file")
            canonical["workspace_file"] = str(workspace_file)
        except (ValueError, OSError) as exc:
            error("workspace_file", exc)
    if declared and not any(row["pointer"] == "/workspace_file" for row in errors):
        try:
            roots = api.workspace_roots(declared, workspace_file)
            if any(not path.is_dir() for path in roots):
                raise ValueError("Every declared workspace folder must exist.")
            repositories = api.discover_repositories(declared, workspace_file, roots=roots)
            if args.project:
                selected, repositories = api.choose_repository(declared, workspace_file, args.project,
                                                                repositories=repositories)
                canonical["project"] = str(selected)
        except (ValueError, OSError, api.ReviewContextError) as exc:
            error("project", exc)
    try:
        root = runtime_path(args.workspace, args.runtime_root)
        if any((parent / ".git").exists() for parent in (root, *root.parents)) or any(
                root.is_relative_to(repo.resolve()) for repo in repositories):
            raise ValueError("Review runtime must be outside every declared Git Repo.")
        if root.exists():
            if not root.is_dir():
                raise ValueError("Review runtime must be a directory.")
            marker = root / ".merge-reviewer-runtime.json"
            if marker.exists():
                coordination.runtime(root)
            elif any(item.name != ".ownership.lock" for item in root.iterdir()):
                raise ValueError(f"Review runtime is not owned: {root}")
        canonical["runtime_root"] = str(root)
    except (ValueError, OSError) as exc:
        error("runtime_root", exc)
    if selected:
        for field in ("base", "head"):
            ref = getattr(args, field)
            if not ref:
                continue
            try:
                target = api.remote_target_for_ref(selected, ref)
                if target:
                    if args.no_fetch:
                        raise ValueError("Explicit remote branches cannot use --no-fetch.")
                    if not api.git_ok(selected, ["check-ref-format", "refs/heads/" + target.branch]):
                        raise ValueError("A valid branch name is required.")
                elif not api.resolve_commit_ref(selected, ref)[0]:
                    raise ValueError("The local branch does not exist; tags, commits and HEAD are unsupported.")
                canonical[field] = ref
            except (ValueError, OSError, api.ReviewContextError) as exc:
                error(field, exc)
    return {"schema": "MergeReviewPreflight/v1", "valid": not errors, "errors": errors,
            "canonical_inputs": canonical, "selected_repo": str(selected) if selected else None,
            "network_calls": 0, "writes": 0}


def job_path(root, identifier):
    if not coordination.IDENTIFIER.fullmatch(identifier or ""):
        raise ValueError("request_id must be 32 hexadecimal characters.")
    return root / "jobs" / (identifier + ".json")


def response_context(root, identifier, *, create=False):
    job_path(root, identifier)
    path = coordination.plain(root / "responses" / identifier)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return coordination.plain(path)


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


def status(root, identifier, *, summary=False):
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
                            "boundary_changes": manifest.get("boundary_changes", []),
                            "contract_scope_policy": manifest.get("contract_scope_policy"),
                            "contracts": [{"id": c["id"], "symbol": c["symbol"], "search_scope": c.get("search_scope", "workspace")}
                                          for c in manifest["changed_contracts"]],
                            "operations": manifest.get("operations", {}),
                            "remaining": remaining(draft),
                            "diagnostics": diagnostics(draft, manifest, context, job.get("line_counts")),
                            "network_activity": manifest["network_activity"],
                            "sync_activity": manifest.get("sync_activity", [])}
    return status_summary(result) if summary else result


def status_summary(value):
    """Keep actionable state in one small response; full receipts remain on status."""
    def brief(item, depth=0):
        if isinstance(item, str):
            return item[:700]
        if isinstance(item, list):
            return [brief(child, depth + 1) for child in item[:3]]
        if isinstance(item, dict):
            if depth >= 4:
                return {"details_omitted": True}
            return {str(key)[:160]: brief(child, depth + 1) for key, child in list(item.items())[:12]}
        return item

    resume = value.get("resume", {})
    inspected = resume.get("diagnostics", {})
    errors = list(inspected.get("errors", []))
    for item in value.get("errors", []):
        if item not in errors:
            errors.append(item)
    if value.get("error"):
        errors.append(value["error"])
    gaps = value.get("gaps", inspected.get("gaps", []))
    pending = value.get("remaining", resume.get("remaining", {}))
    result = {"schema": "MergeReviewStatusSummary/v1", "request_id": value["request_id"],
              "runtime_root": value.get("runtime_root"), "state": value["state"], "phase": value.get("phase"),
              "tool_identity": value.get("tool_identity"), "progress": brief(value.get("progress")),
              "review_complete": value.get("review_complete"), "review_status": value.get("review_status"),
              "report_path": value.get("report_path"), "cleanup_pending": value.get("cleanup_pending", False),
              "errors": brief(errors), "error_count": len(errors), "gaps": brief(gaps), "gap_count": len(gaps),
              "remaining": brief(pending),
              "remaining_counts": {key: len(rows) if isinstance(rows, list) else rows for key, rows in pending.items()},
              "ready_for_finish": inspected.get("ready_for_finish", False), "evidence_verified": False,
              "expected_outcome": inspected.get("expected_outcome", value.get("expected_outcome")),
              "blocking_gaps": brief(inspected.get("blocking_gaps", value.get("blocking_gaps", []))),
              "context_retained": value.get("context_retained", False),
              "recoverable": value.get("recoverable", False),
              "failure_kind": value.get("failure_kind"), "retained_until_epoch": value.get("retained_until_epoch"),
              "details": {"command": "status", "runtime_root": value.get("runtime_root"),
                          "request_id": value["request_id"]}}
    if value["state"] == "published":
        action = "finish" if value.get("cleanup_pending") else "open-report"
    elif value["state"] in TERMINAL:
        action = "inspect-render-error" if value.get("failure_kind") == "render" else "stop"
    elif value.get("phase") == "publication-repair":
        action = "repair-output-and-finish"
    elif errors:
        action = "continue-review" if value["state"] == "ready" else "repair-draft-and-check"
    elif inspected.get("ready_for_finish"):
        action = "finish"
    else:
        action = "continue-review"
    result["next_action"] = action
    result["details_omitted"] = True
    if len(review_runtime.canonical(result)) >= LIMIT - 512:
        # Counts and the full-status route survive even unusually large drafts.
        result["remaining"] = {}
        result["errors"] = brief(errors[:1])
        result["gaps"] = brief(gaps[:1])
    if len(review_runtime.canonical(result)) >= LIMIT - 512:
        result["errors"] = [{"message": str(errors[0])[:700]}] if errors else []
        result["gaps"] = [str(gaps[0])[:700]] if gaps else []
    return result


def tool_identity():
    directory = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.py"), key=lambda item: item.name):
        digest.update(path.name.encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return {"skill_version": (directory.parent / "VERSION").read_text(encoding="utf-8").strip(),
            "helper_sha256": digest.hexdigest(), "python_version": sys.version.split()[0]}


def capabilities():
    """Machine-readable protocol discovery for workbenches and agent hosts."""
    return {"schema": "MergeReviewCapabilities/v1", "protocol_version": 1, **tool_identity(),
            "draft_formats": ["compact", "v3"],
            "features": {"artifact_scopes": ["context", "runtime"],
                         "read_only_artifact_view": True,
                         "request_preflight": True,
                         "incremental_resume": True,
                          "publication_retry": True, "batch_artifact_view": True, "summary_status": True,
                          "validation_progress": True,
                         "launch_preflight": True, "read_only_inspection": True,
                         "completion_preview": True, "scope_notes": True,
                         "phase_metrics": True}}


def repo_index(manifest):
    return [{"repo": item["id"], "name": item["name"], "role": item["role"], "state": item["state"],
             "sources": {name: {"ref": source["ref"]} for name, source in item["sources"].items()}}
            for item in manifest["repositories"]]


def draft_template(manifest):
    result = {"schema": "merge-reviewer-workspace-result/v3", "report_layout": "engineering-review/v1",
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
            "impact_coverage": [], "boundary_reviews": [
                {"change_id": item["id"], "outcome": "unresolved", "reason": "尚未完成邊界判定。",
                 "evidence": [], "search_ids": [], "boundary_contract_ids": []}
                for item in manifest.get("boundary_changes", []) if manifest.get("extension_enabled")],
            "findings": [], "pending_confirmations": [], "next_steps": [],
            "tests_executed": [], "tests_not_executed": ["本次尚未執行專案測試。"],
            "limitations": [], "scope_notes": []}
    selected = next((repo for repo in manifest.get("repositories", [])
                     if repo.get("id") == manifest.get("selected_repo")), {})
    final = selected.get("sources", {}).get("merge-preview", selected.get("sources", {}).get("head", {}))
    for row in result["merge_parent_review"]:
        row["final_ref"] = final.get("ref")
    return result


_draft_template_v3 = draft_template


def draft_template(manifest, draft_format="v3"):
    if draft_format == "v3":
        return _draft_template_v3(manifest)
    if draft_format != "compact":
        raise ValueError("draft format must be v3 or compact")
    import contract_boundary
    result = _draft_template_v3(manifest)
    result["schema"] = "merge-reviewer-authoring/v1"
    result.pop("report_layout", None)
    changes = {tuple(item["root"].get(key) for key in ("repo", "source", "path")): item["id"]
               for item in contract_boundary.changes(manifest)}
    for row in result["coverage"]:
        key = tuple(row.get(name) for name in ("repo", "source", "path"))
        identifier = changes.get(key)
        if identifier:
            row.pop("repo", None)
            row.pop("source", None)
            row.pop("path", None)
            row["change_id"] = identifier
    return result


def initial_read_requests(manifest):
    """Build the initial diff, instruction, changed-source and parent reads."""
    import review_workspace as workspace

    repo = next(row for row in manifest["repositories"] if row["id"] == manifest["selected_repo"])
    requests, seen = [], set()
    index = workspace.FixedSourceIndex()

    def add_request(op, **values):
        requests.append({"id": f"init-{len(requests) + 1:06}", "op": op, **values})

    # Start returns these fixed artifacts so the first helper round can read
    # the diff and applicable instructions alongside source files.
    add_request("instructions", repo=repo["id"])
    for patch in repo.get("patches", []):
        add_request("patch", repo=repo["id"], path=patch)

    def blobs(source_name, path):
        source = repo.get("sources", {}).get(source_name)
        if not isinstance(source, dict):
            return []
        return [item for item in index.entries(source, path) if item.get("kind") == "blob"]

    def add(source_name, path):
        matches = blobs(source_name, path)
        key = (repo["id"], source_name, path)
        if len(matches) != 1 or key in seen:
            return
        seen.add(key)
        add_request("read", repo=repo["id"], source=source_name, path=path)

    for root in manifest.get("required_coverage", []):
        if root.get("repo") != repo["id"]:
            continue
        path = root.get("path")
        for source_name in ("base", "head", "merge-preview"):
            add(source_name, path)
        if not any(blobs(name, path) for name in ("base", "head", "merge-preview")):
            add("merge-base", path)
    for view in manifest.get("required_merge_views", []):
        if not view.get("parent_content_available"):
            continue
        for path in view.get("paths", []):
            add("parent-" + view["parent"], path)
    return requests


def operation_preflight(operations, manifest=None, line_counts=None):
    """Check request structure before any speculative source or search reads."""
    if not isinstance(operations, list) or not operations:
        return {"next_index": 0, "code": "authoring.invalid-requests",
                "pointer": "/operations", "error": "operations must be a nonempty array"}
    allowed = {
        "read": {"repo", "source", "path", "line_start", "line_end", "around_line", "context_lines"},
        "instructions": {"repo", "path"}, "inventory": {"repo", "source"},
        "patch": {"repo", "path", "line_start", "line_end"},
        "register-contract": {"contract"},
        "search": {"query", "repo", "source", "contract", "change_id"},
        "view": {"artifact", "pointer", "offset", "page_size", "string_offset"},
    }
    seen_ids = set()
    repositories = {item.get("id"): item for item in (manifest or {}).get("repositories", [])}
    for index, operation in enumerate(operations):
        base = f"/operations/{index}"
        if not isinstance(operation, dict):
            return {"next_index": index, "code": "authoring.invalid-operation",
                    "pointer": base, "error": "operation must be an object"}
        identifier = operation.get("id")
        if not isinstance(identifier, str) or not identifier or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
            return {"next_index": index, "code": "authoring.invalid-operation-id",
                    "pointer": base + "/id", "operation_id": identifier,
                    "error": "operation needs a unique id (letters, digits, underscore or hyphen)"}
        if identifier in seen_ids:
            return {"next_index": index, "code": "authoring.duplicate-operation-id",
                    "pointer": base + "/id", "operation_id": identifier,
                    "error": "operation IDs must be unique within one request"}
        seen_ids.add(identifier)
        kind = operation.get("op")
        if kind not in allowed:
            return {"next_index": index, "code": "authoring.unsupported-operation",
                    "pointer": base + "/op", "operation_id": identifier,
                    "error": "unsupported operation; expected one of " + ", ".join(allowed)}
        extra = set(operation) - (allowed[kind] | {"id", "op"})
        if extra:
            name = sorted(extra)[0]
            message = "fields is output-only; it cannot select a JSON pointer" if name == "fields" else "unknown operation argument: " + name
            return {"next_index": index, "code": "authoring.invalid-operation-argument",
                    "pointer": base + "/" + name.replace("~", "~0").replace("/", "~1"),
                    "operation_id": identifier, "error": message}
        args = {key: value for key, value in operation.items() if key not in ("id", "op")}
        string_fields = {"repo", "source", "path", "query", "contract", "change_id", "artifact", "pointer"}
        for name in string_fields & args.keys():
            if kind == "search" and name == "repo" or kind == "register-contract" and name == "contract":
                continue
            value = args[name]
            if value is not None and not isinstance(value, str):
                return {"next_index": index, "code": "authoring.invalid-operation-argument",
                        "pointer": f"{base}/{name}", "operation_id": identifier,
                        "error": name + " must be text"}
        if kind == "search" and args.get("repo") is not None and not (
                isinstance(args["repo"], list) and all(isinstance(repo, str) for repo in args["repo"])):
            return {"next_index": index, "code": "authoring.invalid-operation-argument",
                    "pointer": base + "/repo", "operation_id": identifier,
                    "error": "search repo must be an array of Repo IDs"}
        if manifest is not None:
            if kind == "search":
                requested_repos = (args["repo"] if args.get("repo") is not None
                                   else list(repositories))
            elif kind == "inventory":
                requested_repos = [args["repo"]] if args.get("repo") else list(repositories)
            elif kind in ("read", "patch", "instructions"):
                requested_repos = [args.get("repo")]
            else:
                requested_repos = []
            for repo_id in requested_repos:
                if repo_id not in repositories:
                    return {"next_index": index, "code": "authoring.unknown-repository",
                            "pointer": base + "/repo", "operation_id": identifier,
                            "error": "Repo ID is not present in the captured review"}
            if kind in ("read", "patch", "instructions") and not args.get("repo"):
                return {"next_index": index, "code": "authoring.invalid-operation-argument",
                        "pointer": base + "/repo", "operation_id": identifier,
                        "error": kind + " needs a Repo ID"}
            if kind in ("read", "patch") and args.get("repo") in repositories:
                repo = repositories[args["repo"]]
                source_name = args.get("source") or repo.get("review_source")
                if source_name not in repo.get("sources", {}):
                    return {"next_index": index, "code": "authoring.unknown-source",
                            "pointer": base + "/source", "operation_id": identifier,
                            "error": "source is not present in the captured review"}
                if kind == "read" and args.get("path"):
                    source = repo["sources"][source_name]
                    if not any(item.get("path") == args["path"] and item.get("kind") == "blob"
                               for item in source.get("files", [])):
                        return {"next_index": index, "code": "authoring.unknown-path",
                                "pointer": base + "/path", "operation_id": identifier,
                                "error": "path is not a fixed blob in this source"}
                    line_count = (line_counts or {}).get(_line_count_key(manifest, args))
                    if (type(args.get("line_start")) is int and args["line_start"] >= 1
                            and type(args.get("line_end")) is int
                            and args["line_end"] >= args["line_start"] and type(line_count) is int
                            and args["line_end"] > line_count):
                        return {"next_index": index, "code": "evidence.line-range-out-of-bounds",
                                "pointer": base + "/line_end", "operation_id": identifier,
                                "line_count": line_count,
                                "error": f"line_end exceeds fixed file line count ({line_count})",
                                "hint": f"line_end must be at most {line_count}; use around_line for a bounded excerpt."}
            if kind in ("search", "inventory") and args.get("source") is not None:
                targets = [repositories[repo_id] for repo_id in requested_repos]
                if targets and any(args["source"] not in item.get("sources", {}) for item in targets):
                    return {"next_index": index, "code": "authoring.unknown-source",
                            "pointer": base + "/source", "operation_id": identifier,
                            "error": "source is not present in every selected Repo"}
        if kind == "search" and (not isinstance(args.get("query"), str) or not args.get("query")):
            return {"next_index": index, "code": "authoring.invalid-operation-argument",
                    "pointer": base + "/query", "operation_id": identifier,
                    "error": "search query must be nonempty text"}
        if kind == "register-contract" and not isinstance(args.get("contract"), dict):
            return {"next_index": index, "code": "authoring.invalid-operation-argument",
                    "pointer": base + "/contract", "operation_id": identifier,
                    "error": "register-contract needs one contract object"}
        if kind in ("read", "patch"):
            if ("line_start" in args) != ("line_end" in args):
                bad = "line_start" if "line_start" not in args else "line_end"
                return {"next_index": index, "code": "authoring.invalid-operation-argument",
                        "pointer": base + "/" + bad, "operation_id": identifier,
                        "error": "line_start and line_end must be supplied together as integers"}
            first, last = args.get("line_start"), args.get("line_end")
            if first is not None and (type(first) is not int or type(last) is not int or first < 1 or last < first):
                return {"next_index": index, "code": "authoring.invalid-operation-argument",
                        "pointer": base + "/line_start", "operation_id": identifier,
                        "error": "line range must satisfy 1 <= line_start <= line_end"}
            if kind == "read" and args.get("around_line") is not None and (type(args["around_line"]) is not int or args["around_line"] < 1):
                return {"next_index": index, "code": "authoring.invalid-operation-argument",
                        "pointer": base + "/around_line", "operation_id": identifier,
                        "error": "around_line must be a positive integer"}
            if kind == "read" and args.get("context_lines") is not None and (type(args["context_lines"]) is not int or args["context_lines"] < 0):
                return {"next_index": index, "code": "authoring.invalid-operation-argument",
                        "pointer": base + "/context_lines", "operation_id": identifier,
                        "error": "context_lines must be a nonnegative integer"}
            if kind == "read" and args.get("around_line") is not None and first is not None:
                return {"next_index": index, "code": "authoring.invalid-operation-argument",
                        "pointer": base + "/around_line", "operation_id": identifier,
                        "error": "around_line cannot accompany exact bounds"}
        if kind in ("read", "patch") and not args.get("path"):
            return {"next_index": index, "code": "authoring.invalid-operation-argument",
                    "pointer": base + "/path", "operation_id": identifier, "error": kind + " needs a path"}
        if kind in ("read", "patch") and not args.get("repo"):
            return {"next_index": index, "code": "authoring.invalid-operation-argument",
                    "pointer": base + "/repo", "operation_id": identifier, "error": kind + " needs a Repo ID"}
        if kind == "instructions" and not args.get("repo"):
            return {"next_index": index, "code": "authoring.invalid-operation-argument",
                    "pointer": base + "/repo", "operation_id": identifier,
                    "error": "instructions needs a Repo ID"}
        if kind == "view":
            if not args.get("artifact"):
                return {"next_index": index, "code": "authoring.invalid-operation-argument",
                        "pointer": base + "/artifact", "operation_id": identifier,
                        "error": "view needs an artifact SHA-256"}
            if args.get("pointer", "") and not args["pointer"].startswith("/"):
                return {"next_index": index, "code": "authoring.invalid-operation-argument",
                        "pointer": base + "/pointer", "operation_id": identifier,
                        "error": "view pointer must be empty or a JSON pointer"}
            for name, minimum in (("offset", 0), ("string_offset", 0), ("page_size", 1)):
                if name in args and (type(args[name]) is not int or args[name] < minimum
                                     or name == "page_size" and args[name] > 1000):
                    return {"next_index": index, "code": "authoring.invalid-operation-argument",
                            "pointer": base + "/" + name, "operation_id": identifier,
                            "error": name + " is outside the supported integer range"}
    return None


def _pack_receipt(context, manifest):
    import review_workspace as workspace
    packs = []
    for repo in manifest.get("repositories", []):
        if repo.get("state") != "captured" or not isinstance(repo.get("snapshot"), dict):
            continue
        pack_dir = workspace.evidence_repository(context, repo) / "objects" / "pack"
        if not pack_dir.exists():
            continue
        for item in sorted(pack_dir.iterdir()):
            if item.is_symlink() or not item.is_file():
                raise ValueError("Captured pack directory contains an unexpected entry")
            packs.append({"path": item.relative_to(context).as_posix(), "size": item.stat().st_size,
                          "sha256": coordination.file_digest(item)})
    return packs


def capture_integrity_receipt(context, manifest, previous=None):
    packs = previous.get("packs") if isinstance(previous, dict) else None
    if packs is None:
        packs = _pack_receipt(context, manifest)
    return {"schema": "MergeReviewEvidenceReceipt/v1",
            "manifest_sha256": hashlib.sha256((Path(context) / "manifest.json").read_bytes()).hexdigest(),
            "packs": packs}


def refresh_integrity_receipt(context, manifest, previous=None):
    return capture_integrity_receipt(context, manifest, previous)


def verify_retry_integrity(context, manifest, job):
    """Hash mutable evidence only on a recovery path; full publish checks remain mandatory."""
    receipt = job.get("evidence_receipt")
    if not isinstance(receipt, dict) or receipt.get("schema") != "MergeReviewEvidenceReceipt/v1":
        import workspace_report
        workspace_report.validate_captured_evidence(manifest, context)
        return capture_integrity_receipt(context, manifest)
    manifest_hash = hashlib.sha256((Path(context) / "manifest.json").read_bytes()).hexdigest()
    current_packs = _pack_receipt(context, manifest)
    if current_packs != receipt.get("packs"):
        import workspace_report
        workspace_report.validate_captured_evidence(manifest, context)
        raise ValueError("Captured pack bytes differ from the saved integrity receipt")
    if manifest_hash != receipt.get("manifest_sha256"):
        import workspace_report
        workspace_report.validate_captured_evidence(manifest, context)
        return {**receipt, "manifest_sha256": manifest_hash}
    return receipt


def verify_step_manifest_receipt(context, manifest, job):
    receipt = job.get("evidence_receipt")
    if not isinstance(receipt, dict) or receipt.get("schema") != "MergeReviewEvidenceReceipt/v1":
        import workspace_report
        workspace_report.validate_captured_evidence(manifest, context)
        return capture_integrity_receipt(context, manifest)
    current = hashlib.sha256((Path(context) / "manifest.json").read_bytes()).hexdigest()
    if current != receipt.get("manifest_sha256"):
        import workspace_report
        workspace_report.validate_captured_evidence(manifest, context)
        return {**receipt, "manifest_sha256": current}
    return receipt


def verify_step_packs(context, manifest, job):
    receipt = job.get("evidence_receipt")
    if not isinstance(receipt, dict) or receipt.get("schema") != "MergeReviewEvidenceReceipt/v1":
        return verify_retry_integrity(context, manifest, job)
    if _pack_receipt(context, manifest) != receipt.get("packs"):
        import workspace_report
        workspace_report.validate_captured_evidence(manifest, context)
        raise ValueError("Captured pack bytes differ from the saved integrity receipt")
    return receipt


def git_metrics_after(metrics, phase, activity):
    result = dict(metrics or {})
    rows = list(result.get("git_command_phases", []))
    rows.append({"phase": phase, "command_count": activity.get("command_count", 0),
                 "fetch_command_count": activity.get("fetch_command_count", 0),
                 "process_seconds": activity.get("process_seconds", 0.0)})
    result["git_command_phases"] = rows[-200:]
    result["git_command_count"] = result.get("git_command_count", 0) + activity.get("command_count", 0)
    result["fetch_command_count"] = result.get("fetch_command_count", 0) + activity.get("fetch_command_count", 0)
    result["git_process_seconds"] = result.get("git_process_seconds", 0.0) + activity.get("process_seconds", 0.0)
    result["fetch_connection_evidence"] = "not-observed"
    result.setdefault("model_analysis_seconds", None)
    result.setdefault("model_token_count", None)
    return result


def remaining(draft):
    if not isinstance(draft, dict):
        return {}
    def rows(field):
        value = draft.get(field, [])
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    return {"changes": [row for row in rows("coverage") if row.get("status") != "reviewed"],
            "parents": [row for row in rows("merge_parent_review") if row.get("status") != "reviewed"],
            "repositories": [row for row in rows("repository_coverage") if row.get("status") == "unresolved"],
            "impacts": [row for row in rows("impact_coverage") if row.get("status") != "reviewed"],
            "boundaries": [row for row in rows("boundary_reviews") if row.get("outcome") == "unresolved"],
            "pending_confirmation_count": len(rows("pending_confirmations"))}


def diagnostics(draft, manifest, context=None, line_counts=None):
    import review_diagnostics
    import review_authoring
    try:
        if context is not None:
            draft = review_authoring.expand(draft, manifest, context)
        return review_diagnostics.inspect(draft, manifest, line_counts=line_counts)
    except review_authoring.AuthoringError as exc:
        return {"errors": exc.errors, "gaps": [], "ready_for_finish": False,
                "evidence_verified": False, "evidence_validation": "deferred-to-finish", "git_commands": 0}
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as exc:
        return {"errors": [{"code": "authoring.invalid-structure", "pointer": "", "path": "draft", "message": str(exc)}],
                "gaps": [], "ready_for_finish": False, "evidence_verified": False,
                "evidence_validation": "deferred-to-finish", "git_commands": 0}


def check(root, identifier):
    import git_review_context as api
    token = api.GIT_PROCESS_ACTIVITY.set(api.empty_git_process_activity())
    started = time.perf_counter()
    result = None
    metrics = None
    try:
        result = _check_once(root, identifier)
    finally:
        activity = api.git_process_activity()
        api.GIT_PROCESS_ACTIVITY.reset(token)
        try:
            job = coordination.read_json(job_path(root, identifier))
            metrics = git_metrics_after(job.get("metrics", {}), "check", activity)
            metrics["check_diagnostic_seconds"] = (job.get("metrics", {}).get("check_diagnostic_seconds", 0.0)
                                                    + time.perf_counter() - started)
            fields = {"metrics": metrics}
            if result is not None and not result[0].get("publication_complete"):
                checked = result[0]
                import workspace_report
                def safe(value):
                    if isinstance(value, str):
                        return workspace_report.redact(value)
                    if isinstance(value, dict):
                        return {key: safe(child) for key, child in value.items()}
                    if isinstance(value, list):
                        return [safe(child) for child in value]
                    return value
                fields.update(last_check_at=now(), remaining_counts=checked.get("remaining_counts", {}),
                              expected_outcome=checked.get("expected_outcome"),
                              blocking_gaps=safe(checked.get("blocking_gaps", [])))
            update(root, identifier, **fields)
        except (OSError, ValueError, KeyError):
            pass
    if result is not None and metrics is not None:
        result[0]["metrics"] = metrics
    return result


def _check_once(root, identifier):
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
    value = diagnostics(draft, manifest, context, job.get("line_counts"))
    counts = {key: len(rows) if isinstance(rows, list) else rows for key, rows in remaining(draft).items()}
    return {"request_id": identifier, "context_retained": True, "remaining_counts": counts,
            **value}, 0 if value["ready_for_finish"] else 3


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
                if value.get("failure_kind") == "render" and value.get("context_retained"):
                    try:
                        context = job_context(root, path.stem, value)
                        review_session.cleanup_session(context)
                    except (ValueError, OSError) as exc:
                        update(root, path.stem, cleanup_error=coordination.safe_error(exc))
                        continue
                if not value.get("context_dir") or not Path(value["context_dir"]).exists():
                    response = response_context(root, path.stem)
                    if response.exists():
                        try:
                            coordination.remove_owned(response, root / "responses")
                        except (ValueError, OSError):
                            continue
                    path.unlink()
                    (root / "jobs" / (path.stem + ".cancel")).unlink(missing_ok=True)
        finally:
            lock.release()


def start(args):
    import git_review_context as api
    import review_workspace as workspace
    request_started = time.perf_counter()
    # Existing IDs are checked against their saved inputs below. They must not
    # depend on live workspace availability after their snapshot was captured.
    candidate = runtime_path(args.workspace, args.runtime_root)
    existing_path = candidate / "jobs" / ((args.request_id or "") + ".json")
    launch_activity = api.empty_git_process_activity()
    if not (args.request_id and coordination.IDENTIFIER.fullmatch(args.request_id)
            and coordination.plain(existing_path).is_file()):
        launch_token = api.GIT_PROCESS_ACTIVITY.set(launch_activity)
        timeout_token = api.GIT_TIMEOUT_SECONDS.set(args.git_timeout)
        try:
            inspected = launch_preflight(args)
        finally:
            api.GIT_TIMEOUT_SECONDS.reset(timeout_token)
            api.GIT_PROCESS_ACTIVITY.reset(launch_token)
        if not inspected["valid"]:
            raise LaunchPreflightError(inspected)
    root = resolve_runtime(args.workspace, args.runtime_root, create=True)
    identifier = args.request_id or uuid.uuid4().hex
    args.request_id = identifier
    path = job_path(root, identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    inputs = {"workspace": str(Path(args.workspace).resolve()), "project": args.project,
              "base": args.base, "head": args.head,
              "workspace_file": str(Path(args.workspace_file).resolve()) if args.workspace_file else None,
              "auth_scope": args.auth_scope, "refresh": args.refresh, "no_fetch": args.no_fetch,
              "draft_format": args.draft_format}
    fingerprint = coordination.digest(inputs)
    lock = coordination.FileLock(path.with_suffix(".owner.lock"))
    if not lock.acquire(blocking=False):
        return status(root, identifier)
    git_activity_token = api.GIT_PROCESS_ACTIVITY.set(launch_activity)
    context = None
    active = None
    manifest = None
    job_created = False
    try:
        if path.exists():
            existing = coordination.read_json(path)
            if existing.get("input_digest") != fingerprint:
                raise ValueError("request_id already belongs to different inputs.")
            return status(root, identifier)
        job = {"schema": SCHEMA, "request_id": identifier, "runtime_root": str(root),
               "state": "running", "phase": "preflight", "started_at": now(),
                "updated_at": now(), "pid": os.getpid(), "inputs": inputs, "input_digest": fingerprint,
                "draft_format": args.draft_format, "tool_identity": tool_identity()}
        coordination.atomic(path, job)
        job_created = True
        def notify(phase, fields):
            update(root, identifier, phase=phase, **fields)
            print(json.dumps({"request_id": identifier, "phase": phase, **fields}, ensure_ascii=False),
                  file=sys.stderr, flush=True)
        active = coordination.Coordination(root, identifier, notify, args.refresh, args.auth_scope)
        notify("preflight", {"runtime_root": str(root)})
        discovery_started = time.perf_counter()
        declared = Path(inputs["workspace"])
        workspace_file = Path(inputs["workspace_file"]) if inputs["workspace_file"] else None
        workspace_digest = hashlib.sha256(workspace_file.read_bytes()).hexdigest() if workspace_file else None
        roots = api.workspace_roots(declared, workspace_file)
        repositories = api.discover_repositories(declared, workspace_file, roots=roots)
        seed, repositories = api.choose_repository(declared, workspace_file, args.project, repositories=repositories)
        if workspace_file and hashlib.sha256(workspace_file.read_bytes()).hexdigest() != workspace_digest:
            raise ValueError("Workspace file changed during repository discovery.")
        discovery_seconds = time.perf_counter() - discovery_started
        if any(root.is_relative_to(repo.resolve()) for repo in repositories):
            raise ValueError("Review runtime must be outside every declared Git Repo.")
        inputs["project"] = str(seed.resolve())
        update(root, identifier, inputs=inputs)
        preflight_started = time.perf_counter()
        preflight(root)
        report_publication.preflight(seed / "review-reports")
        if any(api.remote_target_for_ref(seed, value) for value in (args.base, args.head)):
            preflight(coordination.git_network_lease.runtime_root())
        context = review_session.create_session(runtime_root=root, request_id=identifier)
        preflight_seconds = time.perf_counter() - preflight_started
        update(root, identifier, context_dir=str(context), draft_path=str(context / "draft.json"))
        arguments = ["--workspace", inputs["workspace"], "--project", inputs["project"],
                     "--base", args.base, "--head", args.head, "--git-timeout", str(args.git_timeout)]
        if args.workspace_file:
            arguments.extend(["--workspace-file", args.workspace_file])
        if args.no_fetch:
            arguments.append("--no-fetch")
        capture = api.parse_args(arguments)
        capture._prepared_context = str(context)
        capture._workspace_discovery = (declared, workspace_file, roots, repositories, seed, workspace_digest)
        began = time.perf_counter()
        with active.activate():
            manifest = api.build_manifest(capture)
            active.check()
            active.phase("review")
        coordination.atomic(context / "draft.json", draft_template(manifest, args.draft_format))
        selected = workspace.repository(manifest, manifest["selected_repo"])
        patches = []
        for patch in sorted((context / f"repos/{selected['id']}/native").glob("*.patch")):
            patches.append({"path": patch.relative_to(context).as_posix(),
                            "bytes": patch.stat().st_size})
        instructions = workspace.instructions(context, manifest, selected["id"], None, api)
        initial_reads = initial_read_requests(manifest)
        initial_reads_path = context / "initial-reads.json"
        coordination.atomic(initial_reads_path, initial_reads)
        guide = {"schema": "MergeReviewStart/v1", "request_id": identifier, "runtime_root": str(root),
                  "state": "ready", "context_dir": str(context), "draft_path": str(context / "draft.json"),
                  "tool_identity": job["tool_identity"],
                 "selected_repo": manifest["selected_repo"], "repositories": repo_index(manifest),
                 "contract_scope_policy": manifest.get("contract_scope_policy"),
                 "required_coverage": manifest["required_coverage"],
                 "boundary_changes": manifest.get("boundary_changes", []),
                  "required_merge_views": manifest["required_merge_views"], "patches": patches,
                  "draft_format": args.draft_format, "initial_reads_file": str(initial_reads_path),
                  "initial_read_count": len(initial_reads),
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
                                      "query": "<registered term>", "repo": ["<Repo IDs>"]},
                           "local_search": {"id": "local-search-1", "op": "search",
                                            "change_id": "<boundary change ID>", "query": "<local use>"}},
                 "authoring_schema": "merge-reviewer-authoring/v1"}
        coordination.atomic(context / "start.json", guide)
        active.finish_phase()
        evidence_receipt = capture_integrity_receipt(context, manifest)
        git_metrics = api.git_process_activity()
        metrics = {"request_total_seconds": time.perf_counter() - request_started,
                   "discovery_seconds": discovery_seconds, "preflight_seconds": preflight_seconds,
                   "capture_seconds": time.perf_counter() - began,
                   "phase_seconds": dict(active.timings),
                   "sync_wait_seconds": active.timings.get("waiting-network", 0),
                   "packing_seconds": active.timings.get("packing_seconds", 0),
                   "snapshot_pack_wait_seconds": active.timings.get("packing_wait_seconds", 0),
                   "pack_cache_hits": active.pack_hits,
                   "git_command_count": git_metrics["command_count"],
                   "fetch_command_count": git_metrics["fetch_command_count"],
                   "git_process_seconds": git_metrics["process_seconds"],
                   "git_command_phases": [{"phase": "start", "command_count": git_metrics["command_count"],
                                           "fetch_command_count": git_metrics["fetch_command_count"],
                                           "process_seconds": git_metrics["process_seconds"]}],
                   "fetch_connection_evidence": "not-observed",
                   "model_analysis_seconds": None, "model_token_count": None}
        update(root, identifier, state="ready", phase="review", selected_repo=selected["id"],
               network_activity=manifest["network_activity"], sync_activity=manifest.get("sync_activity", []),
               pack_cache_hits=active.pack_hits,
               metrics=metrics, evidence_receipt=evidence_receipt)
        prune_jobs(root)
        return guide
    except Exception as exc:
        if job_created and path.exists():
            activity = api.git_process_activity()
            previous = coordination.read_json(path)
            metrics = git_metrics_after(previous.get("metrics", {}), "start", activity)
            metrics.update(request_total_seconds=time.perf_counter() - request_started,
                           phase_seconds=dict(active.timings) if active else {},
                           sync_wait_seconds=active.timings.get("waiting-network", 0) if active else 0,
                           packing_seconds=active.timings.get("packing_seconds", 0) if active else 0,
                           snapshot_pack_wait_seconds=active.timings.get("packing_wait_seconds", 0) if active else 0,
                           pack_cache_hits=active.pack_hits if active else 0)
            update(root, identifier, state="cancelled" if active and active.cancelled() else "failed",
                   phase="terminal", error=coordination.safe_error(exc), finished_at_epoch=time.time(),
                   network_activity=getattr(exc, "network_activity",
                                            manifest["network_activity"] if manifest else api.empty_network_activity()),
                   sync_activity=[{key: value for key, value in item.items() if key != "receipt"}
                                  for item in active.syncs] if active else [], metrics=metrics)
        if context and context.exists():
            review_session.cleanup_session(context)
        raise
    finally:
        if active:
            active.release_syncs()
        api.GIT_PROCESS_ACTIVITY.reset(git_activity_token)
        lock.release()


def artifact_location(root, identifier, artifact, scope=None):
    """Resolve only already-existing artifacts; a read never creates a view directory."""
    job = coordination.read_json(job_path(root, identifier))
    context = job_context(root, identifier, job)
    if scope not in (None, "context", "runtime"):
        raise ValueError("artifact_scope must be context or runtime")
    candidates = ([scope] if scope else ["context", "runtime"])
    for candidate in candidates:
        directory = context if candidate == "context" else response_context(root, identifier)
        path = directory / "views" / (artifact + ".json")
        if path.is_file():
            return directory, candidate
    raise FileNotFoundError("artifact was not found in the selected context or runtime scope")


def read_artifact(root, identifier, artifact, *, scope=None, pointer="", offset=0,
                  page_size=100, string_offset=0, cache=None):
    directory, selected_scope = artifact_location(root, identifier, artifact, scope)
    result = artifact_view(directory, artifact, pointer, offset, page_size, string_offset, selected_scope, cache=cache)
    return result, selected_scope


def batch_artifact_views(root, identifier, requests, *, offset=0):
    """Read independent pages once per artifact, stopping before the response limit."""
    if (not isinstance(requests, list) or not requests or type(offset) is not int
            or not 0 <= offset <= len(requests)):
        raise ValueError("view requests must be a nonempty array with a valid offset")
    allowed = {"id", "artifact", "artifact_scope", "pointer", "offset", "page_size", "string_offset"}
    cache, results = {}, []
    index = offset
    while index < len(requests):
        request = requests[index]
        row = {"id": str(request.get("id", f"view-{index + 1}"))[:128]} if isinstance(request, dict) else {"id": f"view-{index + 1}"}
        try:
            if not isinstance(request, dict) or set(request) - allowed or not request.get("artifact"):
                raise ValueError("view request needs artifact and supported page arguments")
            page, _ = read_artifact(root, identifier, request["artifact"], scope=request.get("artifact_scope"),
                                    pointer=request.get("pointer", ""), offset=request.get("offset", 0),
                                    page_size=request.get("page_size", 100),
                                    string_offset=request.get("string_offset", 0), cache=cache)
            row["result"] = page
        except (ValueError, KeyError, IndexError, TypeError, OSError) as exc:
            row.update(error=coordination.safe_error(exc), pointer=f"/requests/{index}",
                       code="evidence.invalid-view-request", recoverable=True)
        if len(review_runtime.canonical(results + [row])) > LIMIT - 2048:
            if not results:
                raise ValueError("a batch view page exceeds the response limit; reduce its page_size")
            break
        results.append(row)
        index += 1
    return {"schema": "MergeReviewArtifactBatch/v1", "request_id": identifier, "results": results,
            "complete": index == len(requests), "next_index": index if index < len(requests) else None,
            "view_complete": index == len(requests) and all(
                row.get("result", {}).get("view_complete") is True for row in results),
            "artifact_load_count": len(cache), "git_commands": 0}


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
    scope = "runtime" if Path(context).parent.name == "responses" else "context"
    if isinstance(value, dict) and isinstance(value.get("operations"), list) and all(
            isinstance(item, dict) and "id" in item and "result" in item for item in value["operations"]):
        returned = {**value, "evidence_complete": True, "review_status": "agent-determined",
                    "operations": [{**item, "saved": True, "result_returned": True} for item in value["operations"]]}
    else:
        returned = value
    if len(review_runtime.canonical(returned)) < LIMIT:
        return returned
    artifact = review_runtime.store_view(context, value)
    projected = step_projection(value)
    if projected is not None:
        lean = {"schema": "MergeReviewEvidenceStep/v1", "artifact": artifact,
                "evidence_complete": True, "projection_complete": True, "review_status": "agent-determined", **projected}
        lean["operations"] = [{**item, "saved": True, "result_returned": True} for item in lean["operations"]]
        if len(review_runtime.canonical(lean)) < LIMIT:
            return lean
        # Reserve a continuation for each operation, then fill the smallest
        # results first. One large operation must not hide unrelated small ones.
        operations = value["operations"]
        rows = [{"id": operation.get("id"), "saved": True, "result_returned": False,
                 "continue": view_continuation(context, artifact, f"/operations/{index}/result", 0,
                                                scope=scope)}
                for index, operation in enumerate(operations)]
        partial = {**{key: item for key, item in value.items() if key != "operations"},
                   "schema": "MergeReviewEvidenceStep/v2", "artifact": artifact,
                   "evidence_complete": True, "review_status": "agent-determined",
                   "projection_complete": False, "operations": rows}
        if len(review_runtime.canonical(partial)) >= LIMIT and len(rows) > 64:
            rows[:] = rows[:64]
            partial.update(operations_total=len(operations), operations_next_offset=len(rows),
                           continue_operations=view_continuation(context, artifact, "/operations", len(rows),
                                                                 page_size=64, scope=scope))
        if len(review_runtime.canonical(partial)) < LIMIT:
            sizes = sorted(range(len(rows)), key=lambda index: len(review_runtime.canonical(operations[index])))
            for index in sizes:
                previous = rows[index]
                rows[index] = {**operations[index], "saved": True, "result_returned": True}
                if len(review_runtime.canonical(partial)) >= LIMIT:
                    rows[index] = previous
            partial["projection_complete"] = len(rows) == len(operations) and all(row["result_returned"] for row in rows)
            return partial
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
              "continue": view_continuation(context, artifact, "", 0, scope=scope)}
    if isinstance(value, dict) and "evidence_verified" in value:
        result["evidence_verified"] = value["evidence_verified"]
        result["ready_for_finish"] = value.get("ready_for_finish", False)
    if len(review_runtime.canonical(result)) >= LIMIT:
        result["summary"] = {"fields": [str(key)[:80] for key in list(value)[:32]] if isinstance(value, dict) else None,
                             "count": len(value)}
    return result


def step_file(root, identifier, requested):
    """Treat an editable operations file failure as an incomplete owned step."""
    return step(root, identifier, None, requests_file=requested)


def step(root, identifier, operations, *, requests_file=None):
    import git_review_context as api
    import review_workspace as workspace
    path = job_path(root, identifier)
    lock_started = time.perf_counter()
    owner_lock = coordination.FileLock(path.with_suffix(".owner.lock")).acquire()
    lock_wait = time.perf_counter() - lock_started
    git_token = api.GIT_PROCESS_ACTIVITY.set(api.empty_git_process_activity())
    try:
        job = coordination.read_json(path)
        if job["state"] not in ("ready", "reviewing"):
            raise ValueError("Session is not ready for semantic review.")
        active = coordination.Coordination(root, identifier)
        context = job_context(root, identifier, job)
        started = time.perf_counter()
        error_revalidation = 0.0
        operation_seconds, operation_counts, operation_starts = {}, {}, {}
        prefetch_starts = {}

        def progress(stage, completed, total, operation):
            if stage in ("prefetch-searches", "prefetch-reads"):
                prefetch_starts[stage] = time.perf_counter()
                update(root, job["request_id"], phase="review", progress={"phase": stage,
                       "completed": completed, "total": total, "operation_id": None,
                       "operation": None, "at": now()})
            elif stage in ("prefetch-searches-complete", "prefetch-reads-complete"):
                original = stage.removesuffix("-complete")
                begun = prefetch_starts.pop(original, None)
                if begun is not None:
                    key = original.replace("prefetch-", "")
                    operation_seconds[key] = operation_seconds.get(key, 0.0) + time.perf_counter() - begun
                update(root, job["request_id"], progress={"phase": stage, "completed": completed,
                       "total": total, "operation_id": None, "operation": None, "at": now()})
            elif stage == "operation" and isinstance(operation, dict):
                identifier = operation.get("id")
                kind = operation.get("op", "unknown")
                operation_starts[identifier] = (kind, time.perf_counter())
                operation_counts[kind] = operation_counts.get(kind, 0) + 1
                row = {"phase": "operation", "completed": completed, "total": total,
                       "operation_id": identifier, "operation": kind, "at": now()}
                update(root, job["request_id"], phase="review", progress=row)
                print(json.dumps({"request_id": job["request_id"], **row}, ensure_ascii=False),
                      file=sys.stderr, flush=True)
            elif stage == "operation-complete" and isinstance(operation, dict):
                identifier = operation.get("id")
                active_operation = operation_starts.pop(identifier, None)
                if active_operation:
                    kind, began = active_operation
                    operation_seconds[kind] = operation_seconds.get(kind, 0.0) + time.perf_counter() - began
                update(root, job["request_id"], progress={"phase": "operation-complete",
                       "completed": completed, "total": total, "operation_id": None,
                       "operation": None, "at": now()})

        try:
            active.check()
            review_session.validate_session(context)
            with active.activate(), review_runtime.writer(context):
                manifest = workspace.load(context)
                receipt = verify_step_manifest_receipt(context, manifest, job)
                job = {**job, "evidence_receipt": receipt}
                input_error = None
                if operations is None:
                    try:
                        if requests_file is None:
                            raise ValueError("step requires --requests-file.")
                        operations = coordination.read_json(requests_file)
                    except (OSError, ValueError) as exc:
                        input_error = coordination.safe_error(exc)
                preflight_error = (operation_preflight(operations, manifest, job.get("line_counts"))
                                   if input_error is None else None)
                run_operations = (operations[:preflight_error["next_index"]]
                                  if preflight_error is not None and isinstance(operations, list)
                                  else [] if preflight_error is not None else operations)
                result = ({"operations": [], "complete": False, "next_index": 0,
                           "error": input_error, "code": "authoring.invalid-requests-file",
                           "pointer": "/requests_file", "terminal": False, "recoverable": True,
                           "context_retained": True} if input_error is not None else
                          (workspace.ordered_step(context, manifest, run_operations, api, on_progress=progress)
                           if run_operations else {"operations": [], "complete": True,
                                                   "next_index": 0}))
                for kind, began in operation_starts.values():
                    operation_seconds[kind] = operation_seconds.get(kind, 0.0) + time.perf_counter() - began
                operation_starts.clear()
                line_counts = dict(job.get("line_counts", {}))
                for completed in result.get("operations", []):
                    read = completed.get("result", {})
                    if not isinstance(read, dict) or not isinstance(read.get("lines"), list) or read.get("binary"):
                        continue
                    total = read.get("total_lines")
                    if total is None and "line_start" not in read and "requested_line" not in read:
                        total = len(read["lines"])
                    cache_key = _line_count_key(manifest, read)
                    if cache_key and type(total) is int and total >= 0:
                        line_counts[cache_key] = total
                if line_counts != job.get("line_counts", {}):
                    job = update(root, identifier, line_counts=line_counts)
                if preflight_error is not None:
                    result.update({key: value for key, value in preflight_error.items()
                                   if key not in ("next_index", "line_count")})
                    result.update(next_index=preflight_error["next_index"], complete=False,
                                  terminal=False, recoverable=True, context_retained=True)
                line_overflow = re.search(r"line_end exceeds fixed file line count \((\d+)\)",
                                          result.get("error", ""))
                if (line_overflow and isinstance(operations, list)
                        and type(result.get("next_index")) is int
                        and 0 <= result["next_index"] < len(operations)):
                    line_counts = dict(job.get("line_counts", {}))
                    cache_key = _line_count_key(manifest, operations[result["next_index"]])
                    if cache_key:
                        line_counts[cache_key] = int(line_overflow.group(1))
                        job = update(root, identifier, line_counts=line_counts)
                if not result["complete"] and not active.cancelled():
                    known_authoring_error = (preflight_error is not None
                                             or result.get("code", "").startswith("authoring.")
                                             or result.get("code") == "evidence.line-range-out-of-bounds")
                    if not known_authoring_error or input_error is not None:
                        validation_started = time.perf_counter()
                        receipt = verify_step_packs(context, manifest, job)
                        error_revalidation = time.perf_counter() - validation_started
                    else:
                        result["error_revalidation_skipped"] = True
                receipt = refresh_integrity_receipt(context, manifest, receipt)
        except Exception as exc:
            metrics = git_metrics_after(job.get("metrics", {}), "step", api.git_process_activity())
            metrics["step_seconds"] = job.get("metrics", {}).get("step_seconds", 0.0) + time.perf_counter() - started
            metrics["read_search_seconds"] = metrics["step_seconds"]
            metrics["error_revalidation_seconds"] = job.get("metrics", {}).get("error_revalidation_seconds", 0.0) + error_revalidation
            metrics["error_revalidation_count"] = job.get("metrics", {}).get("error_revalidation_count", 0) + int(error_revalidation > 0)
            metrics["lock_wait_seconds"] = job.get("metrics", {}).get("lock_wait_seconds", 0.0) + lock_wait
            update(root, identifier, state="cancelled" if active.cancelled() else "failed",
                   phase="terminal", error=coordination.safe_error(exc), finished_at_epoch=time.time(),
                   metrics=metrics)
            review_session.cleanup_session(context)
            raise
        if active.cancelled():
            review_session.cleanup_session(context)
            metrics = git_metrics_after(job.get("metrics", {}), "step", api.git_process_activity())
            update(root, identifier, state="cancelled", phase="terminal", finished_at_epoch=time.time(),
                   metrics=metrics)
            return {**result, "complete": False, "cancelled": True}
        elapsed = time.perf_counter() - started
        metrics = git_metrics_after(job.get("metrics", {}), "step", api.git_process_activity())
        metrics["step_seconds"] = job.get("metrics", {}).get("step_seconds", 0.0) + elapsed
        metrics["read_search_seconds"] = metrics["step_seconds"]
        old_operation_seconds = dict(job.get("metrics", {}).get("operation_seconds", {}))
        old_operation_counts = dict(job.get("metrics", {}).get("operation_counts", {}))
        for key, value in operation_seconds.items():
            old_operation_seconds[key] = old_operation_seconds.get(key, 0.0) + value
        for key, value in operation_counts.items():
            old_operation_counts[key] = old_operation_counts.get(key, 0) + value
        metrics["operation_seconds"] = old_operation_seconds
        metrics["operation_counts"] = old_operation_counts
        metrics["read_seconds"] = (job.get("metrics", {}).get("read_seconds", 0.0)
                                    + operation_seconds.get("read", 0.0)
                                    + operation_seconds.get("reads", 0.0))
        metrics["search_seconds"] = (job.get("metrics", {}).get("search_seconds", 0.0)
                                     + operation_seconds.get("search", 0.0)
                                     + operation_seconds.get("searches", 0.0))
        metrics["read_count"] = job.get("metrics", {}).get("read_count", 0) + operation_counts.get("read", 0)
        metrics["search_count"] = job.get("metrics", {}).get("search_count", 0) + operation_counts.get("search", 0)
        metrics["error_revalidation_seconds"] = job.get("metrics", {}).get("error_revalidation_seconds", 0.0) + error_revalidation
        metrics["error_revalidation_count"] = job.get("metrics", {}).get("error_revalidation_count", 0) + int(error_revalidation > 0)
        metrics["error_revalidation_skipped_count"] = (job.get("metrics", {}).get(
            "error_revalidation_skipped_count", 0) + int(bool(result.get("error_revalidation_skipped"))))
        metrics["lock_wait_seconds"] = job.get("metrics", {}).get("lock_wait_seconds", 0.0) + lock_wait
        update(root, identifier, state="reviewing", phase="input-repair" if input_error is not None or preflight_error else "review",
               last_step={"complete": result["complete"], "next_index": result["next_index"]},
               progress={"phase": "operation-complete" if result["complete"] else "operation-failed",
                         "completed": result["next_index"], "total": len(operations) if isinstance(operations, list) else 0,
                         "operation_id": None, "operation": None, "at": now()},
               evidence_receipt=receipt, metrics=metrics)
        result["metrics"] = {"step_seconds": elapsed, "error_revalidation_seconds": error_revalidation,
                             "error_revalidation_skipped": bool(result.get("error_revalidation_skipped")),
                             "git_command_count": api.git_process_activity()["command_count"]}
        return result
    finally:
        api.GIT_PROCESS_ACTIVITY.reset(git_token)
        owner_lock.release()


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
    import git_review_context as api
    activity_token = api.GIT_PROCESS_ACTIVITY.set(api.empty_git_process_activity())
    started = time.perf_counter()
    outcome = "failed"
    try:
        result = _finish_once(root, identifier)
        outcome = "published" if result[1] == 0 else result[0].get("phase", "draft-repair")
        return result
    finally:
        elapsed = time.perf_counter() - started
        activity = api.git_process_activity()
        api.GIT_PROCESS_ACTIVITY.reset(activity_token)
        try:
            job = coordination.read_json(job_path(root, identifier))
            metrics = git_metrics_after(job.get("metrics", {}), "finish", activity)
            metrics["finish_total_seconds"] = elapsed
            metrics["finish_attempt_count"] = job.get("metrics", {}).get("finish_attempt_count", 0) + 1
            attempts = list(job.get("metrics", {}).get("finish_attempts", []))
            attempts.append({"at": now(), "outcome": outcome, "seconds": elapsed,
                             "git_command_count": activity.get("command_count", 0),
                             "fetch_command_count": activity.get("fetch_command_count", 0),
                             "lock_wait_seconds": job.get("metrics", {}).get("finish_lock_wait_last_seconds", 0.0),
                             "error_revalidation_seconds": job.get("metrics", {}).get(
                                 "finish_error_revalidation_seconds", 0.0),
                             "draft_preparation_seconds": job.get("metrics", {}).get(
                                 "draft_preparation_seconds", 0.0),
                             "validation_publish_seconds": job.get("metrics", {}).get(
                                 "validation_publish_seconds")})
            metrics["finish_attempts"] = attempts[-100:]
            update(root, identifier, metrics=metrics)
        except (OSError, ValueError, KeyError):
            pass


def _finish_once(root, identifier):
    import review_report
    import review_workspace as workspace
    import workspace_report
    import report_receipt
    path = job_path(root, identifier)
    with MeasuredFileLock(path.with_suffix(".owner.lock")) as owner_lock:
        job = coordination.read_json(path)
        metrics = dict(job.get("metrics", {}))
        metrics["finish_lock_wait_seconds"] = metrics.get("finish_lock_wait_seconds", 0.0) + owner_lock.wait_seconds
        metrics["finish_lock_wait_last_seconds"] = owner_lock.wait_seconds
        metrics["finish_error_revalidation_seconds"] = 0.0
        metrics["draft_preparation_seconds"] = 0.0
        metrics["validation_publish_seconds"] = None
        job = update(root, identifier, metrics=metrics)
        context = job_context(root, identifier, job)
        planned = job.get("planned_report")

        previous_progress = job.get("progress") or {}
        validation_counts = previous_progress.get("validation")
        validation_updated_at = previous_progress.get("validation_updated_at")

        def progress(stage, *, completed=0, total=0, **details):
            nonlocal validation_counts, validation_updated_at
            timestamp = now()
            if "validation" in details:
                validation_counts, validation_updated_at = details["validation"], timestamp
            row = {"stage": stage, "completed": completed, "total": total,
                   "at": timestamp, **details}
            if validation_counts is not None:
                row.update(validation=validation_counts, validation_updated_at=validation_updated_at)
            update(root, identifier, progress=row)
            print(json.dumps({"request_id": identifier, "phase": stage, **row}, ensure_ascii=False),
                  file=sys.stderr, flush=True)

        def clean_published():
            progress("cleanup", completed=0, total=1)
            try:
                if context.is_dir():
                    review_session.cleanup_session(context)
                update(root, identifier, cleanup_pending=False, cleanup_error=None)
            except (OSError, ValueError) as exc:
                update(root, identifier, cleanup_pending=True, cleanup_error=coordination.safe_error(exc))
            progress("cleanup", completed=1, total=1)
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
        timing = {"draft_preparation_seconds": 0.0, "finish_error_revalidation_seconds": 0.0}
        def staged(output, receipt):
            if (root / "jobs" / (identifier + ".cancel")).exists():
                raise coordination.Cancelled("Review cancelled before the report commit.")
            update(root, identifier, planned_report=str(output), report_sha256=receipt["report_sha256"],
                   report_receipt=receipt, review_outcome=receipt["review_outcome"])
        def published(output):
            update(root, identifier, state="published", phase="complete", report_path=str(output),
                   finished_at_epoch=time.time(), metrics={**job.get("metrics", {}),
                        **timing, "validation_publish_seconds": time.perf_counter() - validation_started,
                        "finish_total_seconds": time.perf_counter() - started})
        try:
            if (root / "jobs" / (identifier + ".cancel")).exists():
                raise coordination.Cancelled("Review cancelled before publication.")
            progress("draft-preparation", completed=0, total=1)
            manifest = workspace.load(context)
            with workspace_report.validation_progress(progress):
                receipt = verify_step_manifest_receipt(context, manifest, job)
            if receipt != job.get("evidence_receipt"):
                job = update(root, identifier, evidence_receipt=receipt)
            draft_started = time.perf_counter()
            draft_path = context / "draft.json"
            try:
                draft = coordination.read_json(draft_path)
            except (OSError, ValueError) as exc:
                draft, errors = {}, [f"draft input: {coordination.safe_error(exc)}"]
            else:
                import review_authoring
                try:
                    expanded = review_authoring.expand(draft, manifest, context)
                except (KeyError, TypeError, ValueError, OSError) as exc:
                    expanded, errors = draft, [f"draft input: {coordination.safe_error(exc)}"]
                else:
                    errors = draft_errors(expanded)
                # This inspection reads saved JSON only. Final evidence verification
                # remains mandatory after authoring errors have been repaired.
                inspected = diagnostics(expanded, manifest, context, job.get("line_counts"))
                errors.extend(f"{row['path']}: {row['message']}" for row in inspected["errors"])
                errors = list(dict.fromkeys(errors))
            progress("draft-preparation", completed=1, total=1)
            if errors:
                progress("evidence-recheck", completed=0, total=1)
                integrity_started = time.perf_counter()
                with workspace_report.validation_progress(progress):
                    verify_retry_integrity(context, manifest, job)
                timing["finish_error_revalidation_seconds"] = time.perf_counter() - integrity_started
                progress("evidence-recheck", completed=1, total=1)
                timing["draft_preparation_seconds"] = time.perf_counter() - draft_started
                update(root, identifier, metrics={**job.get("metrics", {}), **timing,
                                                 "finish_total_seconds": time.perf_counter() - started})
                update(root, identifier, state="reviewing", phase="draft-repair", errors=errors)
                return {"request_id": identifier, "context_retained": True, "errors": errors,
                        "remaining": remaining(draft)}, 3
            update(root, identifier, state="validating", phase="validation", errors=[], error=None, recoverable=False,
                   draft_sha256=hashlib.sha256(draft_path.read_bytes()).hexdigest())
            timing["draft_preparation_seconds"] = time.perf_counter() - draft_started
            validation_started = time.perf_counter()
            progress("validation", completed=0, total=1)

            def publication_stage(_output, _digest):
                progress("report-publication", completed=1, total=2)

            with review_runtime.writer(context), workspace_report.validation_progress(progress):
                review_report.publish_report(context, draft_path, retain_on_validation_error=True,
                                             on_stage=publication_stage, on_stage_receipt=staged,
                                             on_published=published, cleanup_context=False)
            progress("validation", completed=1, total=1)
            progress("report-publication", completed=2, total=2)
            return clean_published()
        except workspace_report.DraftValidationError as exc:
            timing["finish_error_revalidation_seconds"] = max(
                timing["finish_error_revalidation_seconds"],
                time.perf_counter() - validation_started if "validation_started" in locals() else 0.0)
            saved = coordination.read_json(path)
            update(root, identifier, metrics={**saved.get("metrics", {}), **timing})
            update(root, identifier, state="reviewing", phase="draft-repair", errors=[str(exc)])
            return {"request_id": identifier, "context_retained": True, "errors": [str(exc)]}, 3
        except workspace_report.ReportRenderError as exc:
            # The formatter failed before staging. Preserve evidence for diagnosis,
            # but never rerun a terminal request with the same or another helper.
            failed_at = time.time()
            diagnostic = {"code": "publication.render-failed", "pointer": "/report",
                          "message": coordination.safe_error(exc),
                          "hint": "Inspect retained evidence; cancel removes it. Publication cannot retry."}
            update(root, identifier, state="failed", phase="terminal", failure_kind="render",
                   errors=[diagnostic], error=None, recoverable=False, context_retained=True,
                   next_action="inspect-render-error", finished_at_epoch=failed_at,
                   retained_until_epoch=failed_at + 86400)
            return {"request_id": identifier, "terminal": True, "recoverable": False,
                    "context_retained": True, "errors": [diagnostic],
                    "next_action": "inspect-render-error", "retained_until_epoch": failed_at + 86400}, 2
        except Exception as exc:
            # A published receipt survives cleanup failures and may be retried.
            saved = coordination.read_json(path)
            if saved["state"] == "published":
                raise
            if saved.get("planned_report") and Path(saved["planned_report"]).is_file():
                report_receipt.verify(saved)
                if isinstance(exc, report_publication.PublicationError):
                    update(root, identifier, state="published", phase="complete",
                           report_path=saved["planned_report"], finished_at_epoch=time.time())
                    return clean_published()
                # The link commit completed; keep its evidence and receipt for retry.
                raise
            if isinstance(exc, report_publication.PublicationError) and exc.recoverable:
                diagnostic = {"code": "publication.output-unavailable", "pointer": "/report_path",
                              "message": coordination.safe_error(exc), "operation": exc.operation,
                              "hint": "Repair the report directory and retry finish with this request ID."}
                metrics = {**saved.get("metrics", {}), **timing}
                metrics["publication_failure_count"] = metrics.get("publication_failure_count", 0) + 1
                update(root, identifier, state="reviewing", phase="publication-repair",
                       errors=[diagnostic], recoverable=True, metrics=metrics)
                return {"request_id": identifier, "phase": "publication-repair", "errors": [diagnostic],
                        "terminal": False, "recoverable": True, "context_retained": True,
                        "retry": "finish"}, 3
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
    if job["state"] in TERMINAL and job.get("failure_kind") == "render" and job.get("context_retained"):
        with coordination.FileLock(path.with_suffix(".owner.lock")):
            job = coordination.read_json(path)
            if job.get("failure_kind") == "render" and job.get("context_retained"):
                review_session.cleanup_session(job_context(root, identifier, job))
                update(root, identifier, context_retained=False, cleanup_error=None)
        return status(root, identifier)
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
    value.add_argument("command", choices=("capabilities", "preflight", "start", "step", "check", "finish", "status", "cancel", "view"))
    value.add_argument("--workspace")
    value.add_argument("--workspace-file")
    value.add_argument("--runtime-root")
    value.add_argument("--request-id")
    value.add_argument("--project")
    value.add_argument("--base")
    value.add_argument("--head")
    value.add_argument("--draft-format", choices=("compact", "v3"), default="v3")
    value.add_argument("--requests-file", type=Path)
    value.add_argument("--summary", action="store_true", help="Return actionable status without large receipts")
    value.add_argument("--read-only", action="store_true", help="Read artifact pages without metrics or other writes")
    value.add_argument("--refresh", action="store_true")
    value.add_argument("--auth-scope", default=os.environ.get("GITLAB_WORKSPACE_AUTH_SCOPE", ""))
    value.add_argument("--no-fetch", action="store_true")
    value.add_argument("--git-timeout", type=float, default=180)
    value.add_argument("--artifact", help="Read a persisted oversized terminal response with status")
    value.add_argument("--artifact-scope", choices=("context", "runtime"),
                       help="Choose the active context or terminal runtime artifact store")
    value.add_argument("--pointer", default="")
    value.add_argument("--offset", type=int, default=0)
    value.add_argument("--page-size", type=int, default=100)
    value.add_argument("--string-offset", type=int, default=0)
    return value


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    read_only_command = (args.command in ("capabilities", "preflight") or
                         args.command == "status" and args.summary or args.read_only)
    code = 0
    runtime_response = False
    context = None
    paging_started = None
    try:
        if args.summary and (args.command != "status" or args.artifact):
            raise ValueError("--summary is only supported by status without --artifact")
        if args.read_only and args.command != "view":
            raise ValueError("--read-only is only supported by view")
        if args.command == "capabilities":
            result = capabilities()
        elif args.command == "preflight":
            result = launch_preflight(args)
            code = 0 if result["valid"] else 3
        elif args.command == "start":
            if not all((args.workspace, args.project, args.base, args.head)):
                raise ValueError("start requires workspace, project, base and head.")
            result = start(args)
            root = Path(result["runtime_root"])
            if result.get("state") in ("failed", "cancelled"):
                code = 2
            context = Path(result.get("context_dir", "")) if result.get("context_dir") else None
        else:
            root = resolve_runtime(args.workspace, args.runtime_root)
            if args.command == "status":
                if args.artifact:
                    paging_started = time.perf_counter()
                    result, scope = read_artifact(root, args.request_id, args.artifact,
                                                  scope=args.artifact_scope, pointer=args.pointer,
                                                  offset=args.offset, page_size=args.page_size,
                                                  string_offset=args.string_offset)
                    runtime_response = scope == "runtime"
                else:
                    result = status(root, args.request_id, summary=args.summary)
            elif args.command == "view":
                if not args.request_id or not (args.artifact or args.requests_file):
                    raise ValueError("view requires --request-id and --artifact or --requests-file")
                if args.artifact and args.requests_file:
                    raise ValueError("view accepts either --artifact or --requests-file")
                paging_started = time.perf_counter()
                if args.requests_file:
                    try:
                        result = batch_artifact_views(root, args.request_id,
                                                      coordination.read_json(args.requests_file), offset=args.offset)
                    except (OSError, ValueError) as exc:
                        result = {"request_id": args.request_id, "code": "evidence.invalid-view-requests",
                                  "error": coordination.safe_error(exc), "pointer": "/requests_file",
                                  "terminal": False, "recoverable": True, "view_complete": False,
                                  "complete": False, "results": []}
                    code = 0 if result["complete"] and all("error" not in row for row in result["results"]) else 3
                else:
                    result, scope = read_artifact(root, args.request_id, args.artifact,
                                                  scope=args.artifact_scope, pointer=args.pointer,
                                                  offset=args.offset, page_size=args.page_size,
                                                  string_offset=args.string_offset)
                    runtime_response = scope == "runtime"
            elif args.command == "cancel":
                result = cancel(root, args.request_id)
            elif args.command == "step":
                result = step_file(root, args.request_id, args.requests_file)
                code = 0 if result["complete"] else (2 if result.get("cancelled") else 3)
            elif args.command == "check":
                result, code = check(root, args.request_id)
            else:
                result, code = finish(root, args.request_id)
            if args.command not in ("capabilities",):
                job = coordination.read_json(job_path(root, args.request_id))
                context = (artifact_location(root, args.request_id, args.artifact, args.artifact_scope)[0]
                           if args.artifact and args.command in ("view", "status") else
                           Path(job["context_dir"]) if job.get("context_dir") else None)
                if paging_started is not None and not read_only_command:
                    metrics = dict(job.get("metrics", {}))
                    metrics["paging_seconds"] = metrics.get("paging_seconds", 0.0) + time.perf_counter() - paging_started
                    pages = len(result.get("results", [])) if args.command == "view" and args.requests_file else 1
                    metrics["paging_read_count"] = metrics.get("paging_read_count", 0) + pages
                    update(root, args.request_id, metrics=metrics)
        read_only_view = args.command == "view" or args.command == "status" and bool(args.artifact)
        if context and context.exists() and not read_only_view and not read_only_command:
            result = compact(result, context)
        elif args.request_id and not read_only_view and not read_only_command and len(review_runtime.canonical(result)) >= LIMIT:
            response = response_context(root, args.request_id, create=True)
            result = compact(result, response)
            runtime_response = True
        if runtime_response:
            result["response_scope"] = "runtime"
            if len(review_runtime.canonical(result)) >= LIMIT:
                result["summary"] = {"fields": list(result.get("summary", {}))[:16]}
        if args.read_only:
            def preserve_read_only(value):
                if isinstance(value, dict):
                    if "command" in value and "artifact" in value:
                        value.update(command="review_session.py view", read_only=True)
                    for child in value.values():
                        preserve_read_only(child)
                elif isinstance(value, list):
                    for child in value:
                        preserve_read_only(child)
            preserve_read_only(result)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return code
    except LaunchPreflightError as exc:
        print(json.dumps({**exc.result, "request_id": args.request_id, "terminal": False,
                          "job_created": False}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (ValueError, KeyError, IndexError, TypeError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": coordination.safe_error(exc), "request_id": args.request_id,
                          "terminal": True}, ensure_ascii=False), file=sys.stderr)
        return 2
