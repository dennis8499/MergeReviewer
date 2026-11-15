"""Timing snapshots report elapsed activity without inventing validated counts."""
from __future__ import annotations

import unittest
from unittest import mock

from tests.fixtures import SCRIPTS
import workspace_report


class ValidationActivityMetricsTests(unittest.TestCase):
    def manifest(self):
        return {"repositories": [{"state": "captured"}, {"state": "captured"}, {"state": "error"}],
                "changed_contracts": [{"id": "C-1"}],
                "searches": [{"status": "complete"}, {"status": "not-complete"}]}

    def test_prefetch_transitions_close_elapsed_activities_without_validating_items(self):
        events, clock = [], [100.0]
        with workspace_report.validation_progress(lambda **event: events.append(event)), \
             mock.patch.object(workspace_report.time, "monotonic", side_effect=lambda: clock[0]):
            progress = workspace_report.ValidationProgress(self.manifest())
            progress.emit("source-prefetch")
            clock[0] = 102.5
            progress.emit("search-prefetch")
            clock[0] = 256.5
            progress.emit("repositories")
        self.assertEqual({"source-prefetch": 0.0}, events[0]["activity_seconds"])
        self.assertEqual({"source-prefetch": 2.5, "search-prefetch": 0.0}, events[1]["activity_seconds"])
        self.assertEqual({"source-prefetch": 2.5, "search-prefetch": 154.0, "repositories": 0.0},
                         events[2]["activity_seconds"])
        self.assertEqual([0, 0, 0], [event["completed"] for event in events])
        self.assertEqual({"repositories": {"completed": 0, "total": 2},
                          "contracts": {"completed": 0, "total": 1},
                          "searches": {"completed": 0, "total": 1}}, events[-1]["validation"])
        self.assertEqual("validation", events[-1]["stage"])

    def test_same_activity_keeps_one_second_throttle_and_snapshots_include_current_elapsed(self):
        events, clock = [], [0.0]
        with workspace_report.validation_progress(lambda **event: events.append(event)), \
             mock.patch.object(workspace_report.time, "monotonic", side_effect=lambda: clock[0]):
            progress = workspace_report.ValidationProgress(self.manifest())
            progress.emit("search-prefetch")
            clock[0] = 0.4
            progress.emit("search-prefetch")
            clock[0] = 0.999
            progress.emit("search-prefetch")
            self.assertEqual(1, len(events))
            clock[0] = 1.0
            progress.emit("search-prefetch")
            self.assertEqual(2, len(events))
            self.assertEqual({"search-prefetch": 1.0}, events[-1]["activity_seconds"])
            clock[0] = 1.1
            progress.emit("search-prefetch", force=True)
            self.assertEqual(3, len(events))
            self.assertEqual({"search-prefetch": 1.1}, events[-1]["activity_seconds"])
        self.assertEqual({"search-prefetch": 0.0}, events[0]["activity_seconds"])

    def test_revisited_activity_accumulates_once_and_final_counts_match_actual_advances(self):
        events, clock = [], [10.0]
        with workspace_report.validation_progress(lambda **event: events.append(event)), \
             mock.patch.object(workspace_report.time, "monotonic", side_effect=lambda: clock[0]):
            progress = workspace_report.ValidationProgress(self.manifest(), stage="evidence-recheck")
            progress.emit("source-prefetch")
            clock[0] = 12.0
            progress.advance("repositories")
            clock[0] = 12.2
            progress.advance("repositories")
            clock[0] = 13.0
            progress.emit("source-prefetch")
            clock[0] = 16.0
            progress.advance("contracts")
            clock[0] = 17.0
            progress.advance("searches")
            clock[0] = 20.0
            progress.emit("complete", force=True)
        self.assertEqual({"source-prefetch": 5.0, "repositories": 1.0,
                          "contracts": 1.0, "searches": 3.0, "complete": 0.0},
                         events[-1]["activity_seconds"])
        self.assertEqual(4, events[-1]["completed"])
        self.assertEqual(4, events[-1]["total"])
        self.assertEqual("evidence-recheck", events[-1]["stage"])
        self.assertEqual({"completed": 2, "total": 2}, events[2]["validation"]["repositories"])
        self.assertEqual({"completed": 0, "total": 1}, events[2]["validation"]["searches"])

    def test_no_observer_preserves_no_clock_work_and_context_is_reset(self):
        with mock.patch.object(workspace_report.time, "monotonic", side_effect=AssertionError("unobserved timing")):
            progress = workspace_report.ValidationProgress(self.manifest())
            progress.emit("source-prefetch")
            progress.advance("repositories")
        self.assertEqual(1, progress.counts["repositories"]["completed"])
        self.assertIsNone(workspace_report.VALIDATION_PROGRESS.get())


if __name__ == "__main__":
    unittest.main()
