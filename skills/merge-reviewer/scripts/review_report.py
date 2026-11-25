#!/usr/bin/env python3
"""Validate and publish a concise Merge Report from a completed Codex review."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Sequence

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from workspace_report import DraftValidationError


class ReportValidationError(ValueError):
    pass


class ManifestPublicationBlocked(ValueError):
    def __init__(self, diagnostic, exit_code):
        super().__init__(diagnostic["error"])
        self.diagnostic = diagnostic
        self.exit_code = exit_code


def require_manifest_ready(context_dir):
    import review_coordinator as coordination
    import review_manifest_recovery as recovery

    identifier = context_dir.name
    try:
        pending = recovery.read_pending(context_dir)
    except recovery.Retryable as exc:
        raise ManifestPublicationBlocked(
            recovery.retry_with_requests_file(identifier, message=coordination.safe_error(exc)), 3) from exc
    except (ValueError, OSError) as exc:
        diagnostic = recovery.blocked(identifier, message=coordination.safe_error(exc))
        diagnostic.update(code="evidence.manifest-recovery-record", recoverable=False, terminal=True,
                          next_action="inspect-interrupted-step",
                          hint="Inspect the retained context and its manifest recovery record.")
        raise ManifestPublicationBlocked(diagnostic, 2) from exc
    if pending is not None:
        raise ManifestPublicationBlocked(recovery.blocked(identifier, pending=pending), 3)


def validate_result(draft, manifest, context_dir, *, timeout=180.0):
    from workspace_report import validate
    return validate(draft, manifest, context_dir, timeout=timeout)


def publish_report(context_dir: Path, result_file: Path, report_dir: Path | None = None, *,
                   timeout: float = 180.0, retain_on_validation_error: bool = False,
                   on_stage=None, on_published=None, cleanup_context=True, on_stage_receipt=None) -> Path:
    import review_coordinator as coordination
    import review_session

    context_dir = review_session.validate_session(context_dir)
    ownership = coordination.read_json(context_dir / review_session.OWNER_FILE)
    options = dict(timeout=timeout, retain_on_validation_error=retain_on_validation_error,
                   on_stage=on_stage, on_published=on_published, cleanup_context=cleanup_context,
                   on_stage_receipt=on_stage_receipt)
    schema = ownership.get("schema") if isinstance(ownership, dict) else None
    if schema == review_session.OWNER_SCHEMA:
        return _publish_report(context_dir, result_file, report_dir, **options)
    if schema != "merge-reviewer-session/v2":
        raise review_session.SessionError("Review session ownership differs.")
    owner_lock = context_dir.parent.parent / "jobs" / (context_dir.name + ".owner.lock")
    with coordination.FileLock(owner_lock, timeout=timeout):
        review_session.validate_session(context_dir)
        if coordination.read_json(context_dir / review_session.OWNER_FILE) != ownership:
            raise review_session.SessionError("Review session ownership differs.")
        return _publish_with_owner_lock(context_dir, result_file, report_dir, **options)


def _publish_with_owner_lock(context_dir, result_file, report_dir=None, *, timeout=180.0, **options):
    """Publish under the manifest lock while the caller holds the request owner lock."""
    import review_runtime
    import review_session

    context_dir = review_session.validate_session(context_dir)
    with review_runtime.writer(context_dir, timeout, allow_pending=True):
        require_manifest_ready(context_dir)
        return _publish_report(context_dir, result_file, report_dir, timeout=timeout, **options)


def _publish_report(context_dir, result_file, report_dir, *, timeout=180.0,
                    retain_on_validation_error=False, on_stage=None, on_published=None,
                    cleanup_context=True, on_stage_receipt=None):
    import review_session
    from review_workspace import load, safe_path
    from workspace_report import publish, validate_captured_evidence

    published = None
    retained = False
    try:
        result_file = result_file.expanduser().resolve()
        if not result_file.is_relative_to(context_dir):
            raise ReportValidationError("結果草稿必須位於本次受管理的 context 內。")
        relative = result_file.relative_to(context_dir).as_posix()
        draft_path = safe_path(context_dir, relative)
        manifest, manifest_hash = load(context_dir, with_digest=True)
        raw = draft_path.read_bytes()
        try:
            draft = json.loads(raw.decode("utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise DraftValidationError(str(exc)) from exc
        options = {"on_stage": on_stage} if on_stage else {}
        if on_stage_receipt:
            options["on_stage_receipt"] = on_stage_receipt
        published = publish(context_dir, draft, manifest, report_dir, timeout=timeout, **options)
        if on_published:
            on_published(published)
        return published
    except DraftValidationError as exc:
        exc.draft_snapshot = {"draft_sha256": hashlib.sha256(raw).hexdigest(),
                              "manifest_sha256": manifest_hash}
        if retain_on_validation_error:
            validate_captured_evidence(manifest, context_dir, timeout=timeout)
            retained = True
        raise
    finally:
        if not retained and cleanup_context:
            try:
                review_session.cleanup_session(context_dir)
            except (OSError, ValueError) as exc:
                if published:
                    raise review_session.SessionError(
                        f"報告已建立，但暫存清理失敗；報告：{published}；殘留：{context_dir}（{exc}）"
                    ) from exc
                raise


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    from git_review_context import positive_timeout
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-dir", required=True, help="Owned v3 evidence context.")
    parser.add_argument("--result", required=True, help="Codex-authored v3 draft inside the context.")
    parser.add_argument("--report-dir", help="Output override; defaults to the selected Repo/review-reports.")
    parser.add_argument("--git-timeout", type=positive_timeout, default=180.0)
    parser.add_argument("--retain-on-validation-error", action="store_true",
                        help="Keep owned evidence only for repairable draft errors (exit 3).")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        markdown = publish_report(Path(args.context_dir), Path(args.result),
            Path(args.report_dir) if args.report_dir else None,
            timeout=args.git_timeout, retain_on_validation_error=args.retain_on_validation_error)
    except ManifestPublicationBlocked as exc:
        print(json.dumps(exc.diagnostic, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    except DraftValidationError as exc:
        if args.retain_on_validation_error:
            print(json.dumps({"context_dir": str(Path(args.context_dir).expanduser().absolute()),
                              "context_retained": True, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 3
        print(f"review-report: 審查報告未建立或清理失敗：{exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"review-report: 審查報告未建立或清理失敗：{exc}", file=sys.stderr)
        return 2
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
