"""Publication replays unique fixed searches, while checking every ledger row."""
from __future__ import annotations

import copy
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report


class SearchReplayTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.make_repo("consumer")
        self.commit()

    def repeated_search(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        repeated = copy.deepcopy(manifest["searches"][0])
        repeated["id"] = f"S-{len(manifest['searches']) + 1:04}"
        manifest["searches"].append(repeated)
        row = next(row for row in draft["repository_coverage"] if row["repo"] == repeated["repo"])
        row["search_ids"].append(repeated["id"])
        workspace.save(context, manifest)
        return context, manifest, draft

    def test_each_unique_search_replays_once_and_keeps_the_result(self):
        context, manifest, draft = self.repeated_search()

        class Uncached(workspace.FixedSearchCache):
            def run(self, repo, name, query):
                return workspace.FixedSearchCache(self.context, self.api).run(repo, name, query)

        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            cached = workspace_report.validate(draft, manifest, context)
            cached_count = sum(call.args[1][0] == "grep" for call in calls.call_args_list)
        with mock.patch.object(workspace_report, "FixedSearchCache", Uncached), \
                mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            uncached = workspace_report.validate(draft, manifest, context)
            uncached_count = sum(call.args[1][0] == "grep" for call in calls.call_args_list)
        self.assertEqual(uncached, cached)
        self.assertEqual(len(manifest["searches"]), uncached_count)
        self.assertLessEqual(cached_count, uncached_count - 1)
        self.assertEqual(manifest["searches"], cached["searches"])

    def test_duplicate_ledger_output_is_still_checked(self):
        context, manifest, draft = self.repeated_search()
        manifest["searches"][-1]["matches"].append("forged duplicate")
        with self.assertRaisesRegex(ValueError, "fixed search ledger differs"):
            workspace_report.validate(draft, manifest, context)

    def test_next_validation_replays_again_instead_of_trusting_previous_cache(self):
        context, manifest, draft = self.repeated_search()
        workspace_report.validate(draft, manifest, context)
        original = api.run_git_capture

        def changed(repo, args, **kwargs):
            if args[0] == "grep":
                return 0, "changed after earlier validation\n", ""
            return original(repo, args, **kwargs)

        with mock.patch.object(api, "run_git_capture", side_effect=changed), \
                self.assertRaisesRegex(ValueError, "fixed search ledger differs"):
            workspace_report.validate(draft, manifest, context)
