"""Completion, transport and source-boundary regressions using local evidence."""
import copy
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report
from tests.support import run_git as git, run_python, write_text
import merge_parent_coverage as parents
import review_runtime


class QualityControlTests(ReviewFixture):
    def test_parent_gate_requires_relevant_views_and_accepts_final_repair(self):
        self.commit("authorize = False\ncontract = 'new'\n")
        git(self.repo, "switch", "main")
        self.commit("authorize = True\ncontract = 'old'\n")
        git(self.repo, "switch", "feature")
        git(self.repo, "merge", "main", "--no-ff", "--no-commit", check=False)
        self.commit("authorize = False\ncontract = 'new'\n")
        self.commit("authorize = True\ncontract = 'new'\n")
        context, manifest = self.capture()
        self.assertTrue(manifest["required_merge_views"])
        draft = self.draft(context, manifest)
        draft.pop("merge_parent_review", None)
        with self.assertRaisesRegex(ValueError, "omits necessary"):
            workspace_report.validate(draft, manifest, context)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        final_name = "merge-preview" if "merge-preview" in seed["sources"] else "head"
        final = seed["sources"][final_name]["ref"]
        draft["merge_parent_review"] = []
        for view in manifest["required_merge_views"]:
            draft["merge_parent_review"].append({"id": view["id"], "status": "reviewed",
                "reason": "已對照 parent；最終版本已恢復授權，不回報舊回歸。", "evidence": [
                    {"repo": seed["id"], "source": "parent-" + view["parent"], "ref": view["parent"], "path": "app.py"},
                    {"repo": seed["id"], "source": final_name, "ref": final, "path": "app.py"}]})
        checked = workspace_report.validate(draft, manifest, context)
        self.assertTrue(checked["review_complete"])
        self.assertEqual([], checked["findings"])

    def test_no_difference_and_net_zero_require_no_parent_views(self):
        self.commit()
        git(self.repo, "switch", "main")
        self.commit("contract = 'main'\n")
        git(self.repo, "switch", "feature")
        git(self.repo, "merge", "main", "--no-ff", "--no-commit", check=False)
        self.commit("contract = 'main'\n")
        context, manifest = self.capture()
        self.assertEqual([], manifest["required_merge_views"])
        checked = workspace_report.validate(self.draft(context, manifest), manifest, context)
        self.assertTrue(checked["review_complete"])

    def test_bom_requests_shared_content_and_durable_search_pages(self):
        self.commit("contract = 'new'\n" * 8)
        context, manifest = self.capture()
        seed = manifest["selected_repo"]
        requests = context / "requests.json"
        requests.write_text(json.dumps([{"repo": seed, "path": "app.py"}] * 2), encoding="utf-8-sig")
        result = run_python(Path(workspace.__file__), "batch-read", "--context-dir", str(context),
                            "--requests-file", str(requests), "--shared-content")
        self.assertEqual(0, result.returncode, result.stderr)
        shared = json.loads(result.stdout)
        self.assertEqual(1, len(shared["contents"]))
        self.assertEqual(2, len(shared["reads"]))
        result = run_python(Path(workspace.__file__), "search", "--context-dir", str(context),
                            "--query", "contract", "--page-size", "2")
        self.assertEqual(0, result.returncode, result.stderr)
        page = json.loads(result.stdout)
        self.assertEqual(2, len(page["result"]["searches"][0]["matches"]))
        full = review_runtime.load_view(context, page["artifact"])
        self.assertEqual(8, len(full["searches"][0]["matches"]))
        saved = workspace.load(context)
        self.assertEqual(8, len(saved["searches"][0]["matches"]))

    def test_file_symlink_hashes_link_instead_of_reading_external_target(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "external.txt"
            target.write_text("outside fixture")
            link = self.repo / "linked.txt"
            try:
                os.symlink(target, link)
            except OSError:
                self.skipTest("Symbolic-link privilege unavailable")
            with mock.patch.object(api, "file_digest", side_effect=AssertionError("target must not be read")):
                # Only the link is enumerated; unrelated regular files are not part of this check.
                with mock.patch.object(api, "run_git", return_value="linked.txt\0"):
                    api.working_tree_digest(self.repo, tracked_index=[])

    def test_fetch_policy_is_exact_and_atomic(self):
        git(self.repo, "remote", "add", "origin", str(self.root / "not-contacted.git"))
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            with mock.patch.object(api.git_network_lease, "run", return_value=__import__('subprocess').CompletedProcess([], 0, b"", b"")):
                api.fetch_inputs(self.repo, ["origin/main", "origin/feature"], True)
        fetch = [call.args[1] for call in calls.call_args_list if call.args[1][0] == "fetch"]
        self.assertEqual(1, len(fetch))
        self.assertTrue({"--atomic", "--no-recurse-submodules", "--no-auto-maintenance", "--no-write-fetch-head", "--jobs=1"} <= set(fetch[0]))
