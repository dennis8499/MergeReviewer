"""Manifest recovery records at the snapshot and first-write boundaries."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.fixtures import SCRIPTS
import review_coordinator as coordination
import review_manifest_recovery as recovery
import review_runtime


class ManifestRecoveryRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="manifest-record-")
        self.addCleanup(temporary.cleanup)
        self.identifier = "a" * 32
        self.context = Path(temporary.name) / self.identifier
        self.context.mkdir()
        self.marker = self.context / recovery.NAME

    def test_evidence_only_stage_and_restore_skip_author_snapshots(self):
        target = self.context / "manifest.json"
        staged = self.context / (".manifest-" + "a" * 32 + ".json")
        payload = {"repositories": []}
        artifact = review_runtime.store_view(self.context, payload)
        original = {"generation": 2, "repositories": [], "operations": {}}
        candidate = {**original, "generation": 3, "operations": {"inventory": {"artifact": artifact}}}
        coordination.atomic(target, original)
        coordination.atomic(staged, candidate)
        before = {"generation": 2, "sha256": coordination.file_digest(target)}
        after = {"generation": 3, "sha256": coordination.file_digest(staged)}
        batch = [{"id": "inventory", "op": "inventory"}]
        response = {"operations": [{"id": "inventory", "artifact": artifact, "result": payload}],
                    "complete": True, "next_index": 1}
        with mock.patch.object(recovery, "draft_state", side_effect=AssertionError("Read author state")):
            recovery.stage(self.context, staged, before, after, batch, response,
                           candidate, previous=None, phase_after="review")
            pending = recovery.read_pending(self.context)
            self.assertIs(pending["author_state_required"], False)
            snapshot_fields = {"previous_draft_sha256", "previous_checkpoint", "draft_sha256", "checkpoint"}
            self.assertFalse(snapshot_fields.intersection(pending))
            self.assertEqual((candidate, staged, target, response), recovery.restore(
                self.context, pending, {"manifest_sha256": before["sha256"]}))

    def test_prepared_record_rejects_mixed_and_unmarked_snapshot_modes(self):
        evidence = [{"id": "inventory", "op": "inventory"}]
        author = [{"id": "author", "op": "author"}]
        for name, batch, fields in (
                ("unmarked_missing_fields", evidence, {}),
                ("mixed_evidence_only", author, {"author_state_required": False}),
                ("conflicting_snapshot", evidence, {"author_state_required": False, "checkpoint": None}),
                ("non_boolean_discriminator", evidence, {"author_state_required": 0})):
            with self.subTest(case=name):
                coordination.atomic(self.marker, {
                    "schema": recovery.SCHEMA, "request_id": self.identifier, "state": "prepared",
                    "batch": batch, "batch_sha256": recovery.batch_digest(batch), **fields})
                with self.assertRaisesRegex(ValueError, "manifest recovery record"):
                    recovery.read_pending(self.context)

    def test_first_author_marker_retry_depends_on_readback(self):
        real_atomic = coordination.atomic
        batch = [{"id": "author", "op": "author"}]
        before = {"generation": 2, "sha256": "a" * 64}
        for outcome, exception in (("absent", recovery.Unstaged), ("unreadable", recovery.Unstaged),
                                   ("matching", recovery.Retryable), ("different", recovery.RecordUnavailable),
                                   ("invalid", recovery.RecordUnavailable)):
            with self.subTest(outcome=outcome):
                self.marker.unlink(missing_ok=True)
                denial = PermissionError("Synthetic sharing denial")
                denial.winerror = 32

                def atomic(path, value):
                    if outcome == "matching":
                        real_atomic(path, value)
                    if outcome == "different":
                        real_atomic(path, {**value, "before": {"generation": 1, "sha256": "b" * 64}})
                    if outcome == "invalid":
                        real_atomic(path, {**value, "state": "unknown"})
                    raise denial

                real_read_json = coordination.read_json

                def read_json(path):
                    if outcome == "unreadable":
                        raise denial
                    return real_read_json(path)

                with mock.patch.object(coordination, "atomic", side_effect=atomic), \
                     mock.patch.object(coordination, "read_json", side_effect=read_json), \
                     self.assertRaises(exception) as error:
                    recovery.begin_author_batch(self.context, batch, before, ("b" * 64, None), ["author"])
                self.assertIs(type(error.exception), exception)

    def test_locked_author_journal_requires_original_file_before_staging(self):
        denial = PermissionError("Synthetic journal sharing denial")
        denial.winerror = 32
        author = [{"id": "author", "op": "author"}]
        before = {"generation": 2, "sha256": "a" * 64}
        with mock.patch.object(coordination, "file_digest", side_effect=denial), \
             mock.patch.object(coordination, "atomic") as atomic, self.assertRaises(recovery.Unstaged):
            recovery.begin_author_batch(self.context, author, before, ("b" * 64, None), ["author"])
        atomic.assert_not_called()
        self.assertIsNone(recovery.read_pending(self.context))
