from __future__ import annotations

import copy
import hashlib
import json
import re
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report, review_report
from tests.support import run_git as git


HEADINGS = ["審查結論", "問題總攬", "問題詳情（工程師參考）", "範圍與限制", "技術審查紀錄（工程師參考）"]


class ReviewUpdateTests(ReviewFixture):
    def test_marked_fresh_draft_requires_engineering_details_but_old_draft_remains_valid(self):
        self.commit()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["findings"] = [self.finding(draft)]
        # Existing v3 drafts remain compatible, including their display fallback.
        old = workspace_report.validate(draft, manifest, context)
        self.assertIn("未提供", workspace_report.render(old, manifest))
        draft["report_layout"] = "engineering-review/v1"
        for field in workspace_report.ENGINEERING_FIELDS:
            draft["findings"][0][field] = f"已根據固定來源說明 {field}。"
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        for field in workspace_report.ENGINEERING_FIELDS:
            for invalid in (None, "", "未提供", " 未提供 "):
                bad = copy.deepcopy(draft)
                if invalid is None:
                    bad["findings"][0].pop(field)
                else:
                    bad["findings"][0][field] = invalid
                with self.subTest(field=field, invalid=invalid), self.assertRaises(workspace_report.DraftValidationError):
                    workspace_report.validate(bad, manifest, context)
        draft["findings"][0].pop("operation_scenario")
        (context / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
        with self.assertRaises(workspace_report.DraftValidationError):
            review_report.publish_report(context, context / "draft.json", retain_on_validation_error=True)
        self.assertTrue(context.is_dir())
        self.assertFalse((self.repo / "review-reports").exists())

    def test_all_repo_versions_are_pinned_before_any_snapshot_is_sealed(self):
        consumer = self.make_repo("consumer")
        self.commit()
        old_peer = git(consumer, "rev-parse", "HEAD")
        old_head = git(self.repo, "rev-parse", "feature")
        original = workspace.seal_snapshot
        sealed = []

        def move_refs(context, record, api_module, **kwargs):
            if not sealed:
                # Both changes happen after all pins but before the first pack.
                self.commit("contract = 'later peer'\n", repository=consumer)
                git(self.repo, "update-ref", "refs/heads/feature", git(self.repo, "rev-parse", "main"))
            sealed.append(record["id"])
            return original(context, record, api_module, **kwargs)

        with mock.patch.object(workspace, "seal_snapshot", side_effect=move_refs):
            context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        peer = next(row for row in manifest["repositories"] if row["path"] == str(consumer))
        self.assertEqual(old_head, seed["sources"]["head"]["ref"])
        self.assertEqual(old_peer, peer["sources"]["head"]["ref"])
        self.assertEqual(b"contract = 'old'\n", workspace.fixed_bytes(context, peer, "head", "app.py", api))
        self.assertTrue(all(row["refs_pinned_at"] <= manifest["refs_pinned_at"] <= row["snapshot"]["captured_at"]
                            for row in manifest["repositories"]))

    def test_two_remote_branches_share_one_fetch_and_duplicate_ref_is_not_requested(self):
        remote = self.root / "origin.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        self.commit()
        git(self.repo, "push", "origin", "main", "feature")
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            _, manifest = self.capture(base="origin/main", head="origin/feature")
        fetches = [call for call in calls.call_args_list if call.args[1][0] == "fetch"]
        self.assertEqual(1, len(fetches))
        self.assertEqual(["fetch", "--no-tags", "--no-prune", "origin",
                          "refs/heads/main:refs/remotes/origin/main",
                          "refs/heads/feature:refs/remotes/origin/feature"], fetches[0].args[1])
        seed = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual(["main", "feature"], [row["branch"] for row in seed["native"]["fetches"]])
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            actions = api.fetch_inputs(self.repo, ["origin/main", "origin/main"], True)
        self.assertEqual(1, len(actions))
        self.assertEqual(1, len([call for call in calls.call_args_list if call.args[1][0] == "fetch"]))

    def test_five_sections_linked_overview_and_legacy_optional_fields(self):
        self.commit()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["findings"] = [self.finding(draft)]
        text = workspace_report.render(workspace_report.validate(draft, manifest, context), manifest)
        self.assertEqual(HEADINGS, re.findall(r"^## (.+)$", text, re.MULTILINE))
        self.assertTrue(text.startswith("# 合併審查: api\n"))
        self.assertIn("[F-001](#finding-F-001)", text)
        self.assertIn('<a id="finding-F-001"></a>', text)
        for label in ("操作情境", "證據說明", "影響範圍", "工程修正建議"):
            self.assertIn(f"**{label}**\n\n未提供", text)
        self.assertIn("待人工確認：0 項", text)
        for key in ("operation_scenario", "technical_trigger", "technical_explanation", "impact_scope", "engineering_fix"):
            invalid = copy.deepcopy(draft)
            invalid["findings"][0][key] = []
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, key):
                workspace_report.validate(invalid, manifest, context)

    def test_sensitive_frozen_evidence_is_validated_before_omission(self):
        sources = {
            "settings.json": '{"apiToken":"CONFIG-SECRET-001","host":"PRIVATE-HOST"}\n',
            "private.pem": "-----BEGIN PRIVATE KEY-----\nPRIVATE-MATERIAL-002\n-----END PRIVATE KEY-----\n",
            "client.py": "token = 'TOKEN-SECRET-003'\nurl = 'https://user:URL-SECRET-004@example.test/api'\nnormal = True\n",
        }
        for path, content in sources.items():
            self.commit(content, path)
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        finding = self.finding(draft)
        finding["evidence"] = [copy.deepcopy(row["evidence"][0]) for row in draft["coverage"]]
        for evidence in finding["evidence"]:
            evidence["line_end"] = len(sources[evidence["path"]].splitlines())
        finding["technical_explanation"] = "Bearer PROSE-SECRET-005"
        draft["findings"] = [finding]
        result = workspace_report.validate(draft, manifest, context)
        text = workspace_report.render(result, manifest)
        for secret in ("CONFIG-SECRET-001", "PRIVATE-HOST", "PRIVATE-MATERIAL-002", "TOKEN-SECRET-003", "URL-SECRET-004", "PROSE-SECRET-005"):
            self.assertNotIn(secret, text)
        self.assertIn("normal = True", text)
        self.assertIn("設定值已省略", text)
        self.assertIn("私鑰內容已省略", text)
        self.assertIn("敏感內容已遮蔽或省略", text)
        for evidence in result["findings"][0]["evidence"]:
            self.assertIn(evidence["ref"], text)
            self.assertIn(f"api/{evidence['path']}:1–{evidence['line_end']}", text)
            self.assertEqual(hashlib.sha256(sources[evidence["path"]].encode()).hexdigest(), evidence["content_sha256"])
        invalid = copy.deepcopy(draft)
        invalid["findings"][0]["evidence"][0]["line_end"] = 900
        with self.assertRaisesRegex(ValueError, "interval"):
            workspace_report.validate(invalid, manifest, context)
