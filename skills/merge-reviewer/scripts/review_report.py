#!/usr/bin/env python3
"""Validate and publish a concise Merge Report from a completed Codex review."""
from __future__ import annotations

import argparse
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


def validate_result(draft, manifest, context_dir, *, timeout=180.0):
    from workspace_report import validate
    return validate(draft, manifest, context_dir, timeout=timeout)


def render_markdown(result, manifest):
    from workspace_report import render
    return render(result, manifest)


def publish_report(context_dir: Path, result_file: Path, report_dir: Path | None = None, *,
                   timeout: float = 180.0, retain_on_validation_error: bool = False,
                   on_stage=None, on_published=None, cleanup_context=True, on_stage_receipt=None) -> Path:
    import review_session
    from review_workspace import load, safe_path
    from workspace_report import publish, validate_captured_evidence

    context_dir = review_session.validate_session(context_dir)
    published = None
    retained = False
    try:
        result_file = result_file.expanduser().resolve()
        if not result_file.is_relative_to(context_dir):
            raise ReportValidationError("結果草稿必須位於本次受管理的 context 內。")
        relative = result_file.relative_to(context_dir).as_posix()
        draft_path = safe_path(context_dir, relative)
        manifest = load(context_dir)
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise DraftValidationError(str(exc)) from exc
        options = {"on_stage": on_stage} if on_stage else {}
        if on_stage_receipt:
            options["on_stage_receipt"] = on_stage_receipt
        published = publish(context_dir, draft, manifest, report_dir, timeout=timeout, **options)
        if on_published:
            on_published(published)
        return published
    except DraftValidationError:
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
