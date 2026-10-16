"""A draft error must not make damaged captured evidence eligible for retry."""
from __future__ import annotations

import json

from tests.fixtures import ReviewFixture, SCRIPTS, api, workspace, workspace_report, review_report
from tests.support import run_python


class RetryIntegrityTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()

    def malformed_draft(self, context):
        path = context / "draft.json"
        path.write_text('{"schema":', encoding="utf-8")
        return path

    def assert_fatal_cleanup(self, context, draft_path):
        with self.assertRaises((ValueError, api.ReviewContextError, OSError)) as raised:
            review_report.publish_report(context, draft_path, retain_on_validation_error=True)
        self.assertNotIsInstance(raised.exception, workspace_report.DraftValidationError)
        self.assertFalse(context.exists())
        self.assertFalse((self.repo / "review-reports").exists())

    def test_malformed_json_and_missing_pack_are_fatal_in_the_cli(self):
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        packs = list(workspace.evidence_repository(context, seed).joinpath("objects/pack").glob("*.pack"))
        self.assertTrue(packs)
        for pack in packs:
            pack.chmod(0o666)
            pack.unlink()
        result = run_python(SCRIPTS / "review_report.py", "--context-dir", str(context),
                            "--result", str(self.malformed_draft(context)), "--retain-on-validation-error")
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        self.assertNotIn('"context_retained": true', result.stderr)
        self.assertFalse(context.exists())
        self.assertFalse((self.repo / "review-reports").exists())

    def test_draft_ref_mismatch_does_not_hide_a_tampered_manifest_ref(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        seed["sources"]["head"]["ref"] = seed["sources"]["base"]["ref"]
        workspace.save(context, manifest)
        path = context / "draft.json"
        path.write_text(json.dumps(draft), encoding="utf-8")
        self.assert_fatal_cleanup(context, path)

    def test_malformed_json_does_not_hide_tampered_fixed_searches(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        self.draft(context, manifest)
        self.assertTrue(manifest["searches"])
        manifest["searches"][0]["matches"].append("forged captured ledger output")
        workspace.save(context, manifest)
        self.assert_fatal_cleanup(context, self.malformed_draft(context))

    def test_error_state_cannot_hide_damaged_owned_snapshots(self):
        self.make_repo("consumer")
        for role in ("seed", "candidate"):
            with self.subTest(role=role):
                context, manifest = self.capture()
                repo = next(row for row in manifest["repositories"] if row["role"] == role)
                for pack in workspace.evidence_repository(context, repo).joinpath("objects/pack").glob("*.pack"):
                    pack.chmod(0o666)
                    pack.unlink()
                repo["state"] = "error"
                workspace.save(context, manifest)
                self.assert_fatal_cleanup(context, self.malformed_draft(context))
