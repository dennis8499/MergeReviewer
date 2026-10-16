"""Repairable drafts may opt in to retaining evidence; fatal failures never do."""
from __future__ import annotations

import json
from unittest import mock

from tests.fixtures import ReviewFixture, SCRIPTS, workspace, workspace_report, review_report
from tests.support import run_python


class ReportRetryTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()

    def save(self, context, value):
        path = context / "draft.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_repairable_coverage_error_retains_snapshot_then_publishes_once(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        broken = {**draft, "coverage": []}
        original_manifest = (context / "manifest.json").read_bytes()
        with self.assertRaises(workspace_report.DraftValidationError):
            review_report.publish_report(context, self.save(context, broken), retain_on_validation_error=True)
        self.assertTrue(context.exists())
        self.assertEqual(original_manifest, (context / "manifest.json").read_bytes())
        self.assertFalse((self.repo / "review-reports").exists())
        with mock.patch.object(workspace_report, "validate", wraps=workspace_report.validate) as validate:
            report = review_report.publish_report(context, self.save(context, draft), retain_on_validation_error=True)
        self.assertEqual(1, validate.call_count)
        self.assertTrue(report.is_file())
        self.assertFalse(context.exists())

    def test_cli_invalid_json_is_retryable_only_when_requested(self):
        for retain in (False, True):
            with self.subTest(retain=retain):
                context, manifest = self.capture()
                path = context / "draft.json"
                path.write_text('{"schema":', encoding="utf-8")
                args = ["--context-dir", str(context), "--result", str(path)]
                if retain:
                    args.append("--retain-on-validation-error")
                result = run_python(SCRIPTS / "review_report.py", *args)
                self.assertEqual(3 if retain else 2, result.returncode, result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual(retain, context.exists())
                if retain:
                    diagnostic = json.loads(result.stderr)
                    self.assertTrue(diagnostic["context_retained"])
                    self.assertEqual(str(context), diagnostic["context_dir"])
                    self.assertTrue(diagnostic["error"])
                    self.save(context, self.draft(context, manifest))
                    result = run_python(SCRIPTS / "review_report.py", *args)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertFalse(context.exists())

    def test_invalid_lines_and_priority_are_draft_errors(self):
        for failure in ("line", "priority"):
            with self.subTest(failure=failure):
                context, manifest = self.capture()
                draft = self.draft(context, manifest)
                if failure == "line":
                    draft["coverage"][0]["evidence"][0].update(line_start=999, line_end=999)
                else:
                    draft["findings"] = [self.finding(draft, "P9")]
                with self.assertRaises(workspace_report.DraftValidationError):
                    review_report.publish_report(context, self.save(context, draft), retain_on_validation_error=True)
                self.assertTrue(context.exists())

    def test_invalid_manifest_and_missing_snapshot_remain_fatal(self):
        for failure in ("schema", "snapshot", "tree"):
            with self.subTest(failure=failure):
                context, manifest = self.capture()
                draft = self.draft(context, manifest)
                if failure == "schema":
                    manifest["schema"] = "invalid"
                else:
                    seed = workspace.repository(manifest, manifest["selected_repo"])
                    if failure == "snapshot":
                        workspace.evidence_repository(context, seed).joinpath("HEAD").unlink()
                    else:
                        seed["sources"]["head"]["tree_sha"] = "0" * 40
                workspace.save(context, manifest)
                with self.assertRaises(ValueError) as raised:
                    review_report.publish_report(context, self.save(context, draft), retain_on_validation_error=True)
                self.assertNotIsInstance(raised.exception, workspace_report.DraftValidationError)
                self.assertFalse(context.exists())

    def test_tampered_search_is_fatal_and_not_retained(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        manifest["searches"][0]["matches"].append("forged")
        workspace.save(context, manifest)
        with self.assertRaises(ValueError) as raised:
            review_report.publish_report(context, self.save(context, draft), retain_on_validation_error=True)
        self.assertNotIsInstance(raised.exception, workspace_report.DraftValidationError)
        self.assertFalse(context.exists())

    def test_publish_failure_and_interruption_clean_even_with_opt_in(self):
        for failure in ("write", "interrupt"):
            with self.subTest(failure=failure):
                context, manifest = self.capture()
                path = self.save(context, self.draft(context, manifest))
                target = "os.link" if failure == "write" else "validate"
                error = OSError("write failed") if failure == "write" else KeyboardInterrupt()
                with mock.patch("workspace_report." + target, side_effect=error), self.assertRaises(type(error)):
                    review_report.publish_report(context, path, retain_on_validation_error=True)
                self.assertFalse(context.exists())
