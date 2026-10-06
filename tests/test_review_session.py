from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.support import load_module

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "merge-reviewer" / "scripts"
SESSION = load_module("merge_reviewer_session_tests", SCRIPTS / "review_session.py")


class ReviewSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="merge-reviewer-session-test-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cleanup_removes_the_owned_session_and_is_idempotent(self) -> None:
        context = SESSION.create_session(self.root / "context")
        report_draft = context / "review-result-draft.json"
        report_draft.write_text("{}\n", encoding="utf-8")

        self.assertTrue(SESSION.cleanup_session(context))
        self.assertFalse(context.exists())
        self.assertFalse(SESSION.cleanup_session(context))

    def test_unowned_and_existing_paths_are_preserved(self) -> None:
        context = self.root / "caller-owned"
        context.mkdir()
        original = context / "keep.txt"
        original.write_text("keep", encoding="utf-8")

        with self.assertRaises(SESSION.SessionError):
            SESSION.create_session(context)
        with self.assertRaises(SESSION.SessionError):
            SESSION.cleanup_session(context)
        self.assertEqual("keep", original.read_text(encoding="utf-8"))

    def test_paths_outside_the_system_temp_folder_are_rejected_without_writes(self) -> None:
        outside = Path(__file__).resolve().parents[1] / ".merge-reviewer-outside-test"
        self.assertFalse(outside.exists())
        with self.assertRaises(SESSION.SessionError):
            SESSION.create_session(outside)
        self.assertFalse(outside.exists())

    def test_linked_session_paths_are_rejected(self) -> None:
        context = SESSION.create_session(self.root / "context")
        link = self.root / "linked-context"
        try:
            link.symlink_to(context, target_is_directory=True)
        except OSError as exc:
            SESSION.cleanup_session(context)
            self.skipTest(f"symlink creation is unavailable in this environment: {exc}")

        with self.assertRaises(SESSION.SessionError):
            SESSION.cleanup_session(link)
        self.assertTrue((context / SESSION.OWNER_FILE).is_file())
        SESSION.cleanup_session(context)

    def test_manifest_cleanup_error_is_reported_with_the_residual_path(self) -> None:
        context = SESSION.create_session(self.root / "context")
        self.assertEqual(context.resolve(), SESSION.validate_session(context))

        marker = context / SESSION.OWNER_FILE
        marker.unlink()
        with self.assertRaisesRegex(SESSION.SessionError, "未刪除任何檔案"):
            SESSION.cleanup_session(context)
        self.assertTrue(context.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
