"""Sanitized regressions from the November 3–4 MergeReviewer sessions."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report, review_session
from tests.support import run_git as git
from tests import test_r3_authoring_diagnostics as own_diagnostics
import review_coordinator as coordination
import review_diagnostics
import review_flow as flow


class RuntimeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-discovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "api"
        (self.repo / ".git").mkdir(parents=True)
        self.managed = self.root / ".gitlab-workspace" / "runtime"
        self.managed.mkdir(parents=True)

    def guarded_discovery(self, blocked, *, roots=None):
        original = os.walk

        def walk(*args, **kwargs):
            for current, directories, files in original(*args, **kwargs):
                if Path(current) == blocked:
                    kwargs["onerror"](PermissionError(13, "fixture access denied", str(blocked)))
                yield current, directories, files

        with mock.patch.object(api.os, "walk", side_effect=walk):
            return api.discover_repositories(self.root, None, roots=roots)

    def test_unreadable_kit_runtime_is_pruned_but_sibling_repositories_remain(self):
        peer = self.managed.parent / "sources" / "peer"
        (peer / ".git").mkdir(parents=True)
        self.assertEqual(sorted([self.repo, peer], key=lambda p: str(p).casefold()),
                         self.guarded_discovery(self.managed))

    def test_configured_runtime_is_pruned_and_explicit_declared_root_is_honored(self):
        for variable in ("GITLAB_WORKSPACE_RUNTIME_DIR", "MERGE_REVIEW_RUNTIME_DIR"):
            with self.subTest(variable=variable), mock.patch.dict(os.environ, {variable: str(self.managed)}):
                (self.managed / ".git").mkdir(exist_ok=True)
                self.assertEqual([self.repo], self.guarded_discovery(self.managed))
                self.assertEqual([self.managed], api.discover_repositories(self.root, None, roots=[self.managed]))
                configured = self.root / "custom-runtime"
                configured.mkdir(exist_ok=True)
                with mock.patch.dict(os.environ, {variable: str(configured)}):
                    self.assertEqual([self.repo], self.guarded_discovery(configured))

    def test_real_repository_and_non_runtime_kit_errors_are_not_ignored(self):
        for blocked in (self.repo, self.managed.parent):
            with self.subTest(blocked=blocked), self.assertRaisesRegex(api.ReviewContextError, "fixture access denied"):
                self.guarded_discovery(blocked)

    def test_nested_group_runtime_is_pruned(self):
        nested = self.root / "nested" / ".gitlab-workspace" / "runtime"
        nested.mkdir(parents=True)
        self.assertEqual([self.repo], self.guarded_discovery(nested))


class DeletedContractTests(ReviewFixture):
    BASE = "contract = 'old'\ndef removed_api():\n    return 1\n\nkeep = True\n"
    HEAD = "contract = 'new'\nkeep = True\n"

    def scenario(self, base=None, head=None, *, divergent=False):
        self.consumer = self.make_repo("consumer")
        git(self.repo, "switch", "main")
        self.commit(base or self.BASE)
        git(self.repo, "switch", "-c", "reviewed", "main")
        self.commit(head or self.HEAD)
        if divergent:
            git(self.repo, "switch", "main")
            self.commit((base or self.BASE) + "base_only = True\n")
        return self.capture(head="reviewed")

    def contract(self, manifest, *, source="merge-base", symbol="removed_api", first=2, last=3, root=None):
        selected = workspace.repository(manifest, manifest["selected_repo"])
        root = root or next(row for row in manifest["required_coverage"] if row["source"] == "head")
        return {"id": "C-removed", "kind": "api", "root": root, "symbol": symbol,
                "evidence": {"repo": selected["id"], "source": source,
                             "ref": selected["sources"][source]["ref"], "path": root["path"],
                             "line_start": first, "line_end": last},
                "search_terms": [{"query": symbol, "reason": "Exact removed symbol."}]}

    def test_removed_api_in_surviving_file_can_anchor_left_without_replacing_head_coverage(self):
        context, manifest = self.scenario()
        item = self.contract(manifest)
        self.assertEqual("C-removed", workspace.register_contract(context, manifest, item, api)["id"])
        selected = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual({"head"}, workspace_report.allowed_sources(selected, workspace_report.location(item["root"])))

    def test_pure_deletion_and_replaced_field_are_supported(self):
        for base, head, symbol in (("removed_api = True\nkeep = True\n", "keep = True\n", "removed_api"),
                                   ("legacy_amount = 1\nkeep = True\n", "amount_cents = 1\nkeep = True\n", "legacy_amount")):
            with self.subTest(symbol=symbol):
                self.commit(base)
                git(self.repo, "branch", "-f", "main", "HEAD")
                self.commit(head)
                if not any(path.name == "consumer" for path in self.root.iterdir()):
                    self.make_repo("consumer")
                context, manifest = self.capture()
                item = self.contract(manifest, symbol=symbol, first=1, last=1)
                self.assertEqual(symbol, workspace.register_contract(context, manifest, item, api)["symbol"])

    def test_unchanged_symbol_is_rejected_even_when_wide_anchor_contains_a_changed_line(self):
        context, manifest = self.scenario("removed_api = True\nkeep = 1\n", "removed_api = True\nkeep = 2\n")
        for last in (1, 2):
            with self.subTest(last=last), self.assertRaisesRegex(ValueError, "symbol must occur in removed lines"):
                workspace.register_contract(context, manifest, self.contract(manifest, first=1, last=last), api)

    def test_divergent_base_and_wrong_sha_cannot_replace_actual_comparison_left(self):
        context, manifest = self.scenario(divergent=True)
        workspace.register_contract(context, manifest, self.contract(manifest), api)
        for source, wrong_sha in (("base", False), ("merge-base", True)):
            item = self.contract(manifest, source=source)
            item["id"] += "-invalid"
            if wrong_sha:
                item["evidence"]["ref"] = "0" * 40
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "changed root|source identity"):
                workspace.register_contract(context, manifest, item, api)

    def test_merge_preview_deletion_uses_head_as_left_not_merge_base(self):
        self.make_repo("consumer")
        git(self.repo, "switch", "main")
        self.commit(self.BASE)
        git(self.repo, "switch", "-c", "reviewed", "main")
        self.commit("feature = True\n", relative="feature.py")
        git(self.repo, "switch", "main")
        self.commit("contract = 'old'\nkeep = True\n")
        context, manifest = self.capture(head="reviewed")
        root = next(row for row in manifest["required_coverage"]
                    if row["source"] == "merge-preview" and row["path"] == "app.py")
        workspace.register_contract(context, manifest, self.contract(manifest, source="head", root=root), api)
        wrong = self.contract(manifest, root=root)
        wrong["id"] += "-invalid"
        with self.assertRaisesRegex(ValueError, "changed root"):
            workspace.register_contract(context, manifest, wrong, api)

    def test_deleted_contract_completes_connected_review_and_old_only_coverage_is_rejected(self):
        context, manifest = self.scenario()
        draft = self.draft(context, manifest)
        item = self.contract(manifest)
        item["search_terms"].append({"query": "def removed_api", "reason": "Exact removed declaration."})
        workspace.register_contract(context, manifest, item, api)
        root_node = draft["impact_coverage"][0]
        root_node["contract_ids"].append(item["id"])
        draft["impact_coverage"].append({"id": "I-removed", **item["evidence"], "status": "reviewed",
            "reason": "Removed API anchored on the actual comparison left.", "contract_ids": [item["id"]],
            "evidence": [item["evidence"]], "related_to": [root_node["id"]]})
        for row in draft["repository_coverage"]:
            for term in item["search_terms"]:
                result = workspace.search(context, manifest, term["query"], [row["repo"]], api, contract_id=item["id"])
                row["search_ids"].extend(value["id"] for value in result["searches"])
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        draft["report_layout"] = "engineering-review/v1"
        self.assertTrue(review_diagnostics.inspect(draft, manifest)["ready_for_finish"])
        draft["coverage"][0]["evidence"] = [item["evidence"]]
        with self.assertRaisesRegex(workspace_report.DraftValidationError, "wrong version/source"):
            workspace_report.validate(draft, manifest, context)


class ContractGraphDiagnosticsTests(unittest.TestCase):
    def test_child_contract_requires_its_own_location_without_git_or_source_reads(self):
        fixture = own_diagnostics.ImpactOwnEvidenceDiagnosticsTests()
        draft, manifest = fixture.fixture()
        child = copy.deepcopy(manifest["changed_contracts"][0])
        child.update(id="C-child", parent_ids=["C-001"])
        child["evidence"]["path"] = "other.py"
        manifest["changed_contracts"].append(child)
        draft["impact_coverage"][0]["contract_ids"].append("C-child")
        result = fixture.inspect(draft, manifest)
        self.assertFalse(result["ready_for_finish"])
        self.assertTrue(any("own connected impact anchor" in row["message"] for row in result["errors"]))
        draft["impact_coverage"].append({"id": "I-child", **child["evidence"], "status": "reviewed",
            "reason": "Own child location.", "evidence": [child["evidence"]], "contract_ids": ["C-child"],
            "related_to": ["I-001"]})
        self.assertTrue(fixture.inspect(draft, manifest)["ready_for_finish"])


class ContractGraphValidationTests(ReviewFixture):
    def test_child_anchor_location_is_shared_by_check_finish_and_compact_format(self):
        consumer = self.make_repo("consumer")
        self.commit()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        node = self.attach_consumer(draft, manifest, consumer)
        parent = manifest["changed_contracts"][0]
        child = workspace.register_contract(context, manifest, {
            "id": "C-child", "kind": "behavior", "symbol": "contract", "root": parent["root"],
            "parent_ids": [parent["id"]], "evidence": copy.deepcopy(node["evidence"][0]),
            "search_terms": [{"query": "contract", "reason": "Fixed consumer symbol."},
                             {"query": "contract = 'old'", "reason": "Fixed consumer assignment."}]}, api)
        draft["impact_coverage"][0]["contract_ids"].append(child["id"])
        for row in draft["repository_coverage"]:
            for term in child["search_terms"]:
                result = workspace.search(context, manifest, term["query"], [row["repo"]], api, contract_id=child["id"])
                row["search_ids"].extend(value["id"] for value in result["searches"])
        for schema in (workspace.RESULT, "merge-reviewer-authoring/v1"):
            with self.subTest(schema=schema):
                authored = {**draft, "schema": schema}
                with mock.patch.object(api, "run_git", side_effect=AssertionError("check ran Git")), \
                     mock.patch.object(api, "run_git_capture", side_effect=AssertionError("check ran Git")):
                    checked = flow.diagnostics(authored, manifest, context)
                self.assertFalse(checked["ready_for_finish"])
                self.assertTrue(any("own connected impact anchor" in row["message"] for row in checked["errors"]))
                with self.assertRaisesRegex(workspace_report.DraftValidationError, "own connected impact anchor"):
                    workspace_report.validate(authored, manifest, context)
        node["contract_ids"].append(child["id"])
        self.assertTrue(flow.diagnostics(draft, manifest, context)["ready_for_finish"])
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        draft["coverage"][0]["impact_ids"] = [node["id"]]
        checked = flow.diagnostics(draft, manifest, context)
        self.assertTrue(any("impact roots must cover its own" in row["message"] for row in checked["errors"]))
        with self.assertRaisesRegex(workspace_report.DraftValidationError, "impact roots must cover its own"):
            workspace_report.validate(draft, manifest, context)


class ValidationProgressTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.make_repo("consumer")
        self.commit()
        environment = mock.patch.dict(os.environ, {"GITLAB_WORKSPACE_RUNTIME_DIR": str(self.root / "network")})
        environment.start()
        self.addCleanup(environment.stop)

    def test_progress_is_throttled_and_reports_only_completed_validation(self):
        events, clock = [], [0.0]
        manifest = {"repositories": [{"state": "captured"}], "changed_contracts": [], "searches": []}
        with workspace_report.validation_progress(lambda **event: events.append(event)), \
             mock.patch.object(workspace_report.time, "monotonic", side_effect=lambda: clock[0]):
            progress = workspace_report.ValidationProgress(manifest)
            progress.emit("source-prefetch")
            for _ in range(100):
                progress.emit("source-prefetch")
            self.assertEqual(1, len(events))
            self.assertEqual(0, events[0]["validation"]["repositories"]["completed"])
            clock[0] = 1.1
            progress.emit("source-prefetch")
            self.assertEqual(2, len(events))
            progress.advance("repositories")
            self.assertEqual(1, events[-1]["validation"]["repositories"]["completed"])
        self.assertIsNone(workspace_report.VALIDATION_PROGRESS.get())

    def test_observer_preserves_results_and_git_command_counts(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        with mock.patch.object(api, "run_git", wraps=api.run_git) as git_calls, \
             mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as capture_calls:
            plain = workspace_report.validate(draft, manifest, context)
            counts = git_calls.call_count, capture_calls.call_count
        events = []
        with workspace_report.validation_progress(lambda **event: events.append(event)), \
             mock.patch.object(api, "run_git", wraps=api.run_git) as git_calls, \
             mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as capture_calls:
            observed = workspace_report.validate(draft, manifest, context)
            self.assertEqual(counts, (git_calls.call_count, capture_calls.call_count))
        self.assertEqual(plain, observed)
        self.assertTrue(all(row["completed"] == row["total"] for row in events[-1]["validation"].values()))

    def test_failed_search_replay_does_not_claim_all_searches_verified(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        events = []

        class ChangedSearch(workspace.FixedSearchCache):
            calls = 0

            def run(self, repo, source, query):
                self.calls += 1
                if self.calls == 2:
                    return 0, "forged ledger output\n", ""
                return super().run(repo, source, query)

        with workspace_report.validation_progress(lambda **event: events.append(event)), \
             mock.patch.object(workspace_report, "FixedSearchCache", ChangedSearch), \
             self.assertRaisesRegex(ValueError, "fixed search ledger differs"):
            workspace_report.validate(draft, manifest, context)
        self.assertEqual(1, events[-1]["validation"]["searches"]["completed"])
        self.assertEqual(2, events[-1]["validation"]["searches"]["total"])
        self.assertNotEqual("complete", events[-1]["activity"])

    def test_finish_exposes_prefetch_progress_and_status_does_not_touch_evidence_or_git(self):
        started = flow.start(flow.parser().parse_args(["start", "--workspace", str(self.root),
            "--project", str(self.repo), "--base", "main", "--head", "feature"]))
        context = Path(started["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        root, identifier = Path(started["runtime_root"]), started["request_id"]
        manifest = workspace.load(context)
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        coordination.atomic(context / "draft.json", draft)
        original, observations = api.run_git_capture, []

        def observe(repo, arguments, **kwargs):
            if arguments[0] == "grep" and not observations:
                before = (context / "manifest.json").read_bytes()
                with mock.patch.object(api, "run_git", side_effect=AssertionError("status ran Git")), \
                     mock.patch.object(api, "run_git_capture", side_effect=AssertionError("status ran Git")):
                    status = flow.status(root, identifier, summary=True)
                self.assertEqual(before, (context / "manifest.json").read_bytes())
                observations.append(status["progress"])
            return original(repo, arguments, **kwargs)

        with mock.patch.object(api, "run_git_capture", side_effect=observe):
            result, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertEqual("search-prefetch", observations[0]["activity"])
        self.assertEqual({"completed": 2, "total": 2}, observations[0]["validation"]["repositories"])
        self.assertEqual(0, observations[0]["validation"]["searches"]["completed"])
        self.assertTrue(observations[0]["validation_updated_at"])
        self.assertTrue(all(row["completed"] == row["total"] for row in result["progress"]["validation"].values()))
        with mock.patch.object(api, "run_git", side_effect=AssertionError("published retry ran Git")), \
             mock.patch.object(api, "run_git_capture", side_effect=AssertionError("published retry ran Git")):
            repeated, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertEqual(result["progress"]["validation"], repeated["progress"]["validation"])
        self.assertEqual(result["progress"]["validation_updated_at"], repeated["progress"]["validation_updated_at"])


if __name__ == "__main__":
    unittest.main()
