from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "package_release.py"


def load_release_module():
    spec = importlib.util.spec_from_file_location("merge_reviewer_package_release", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load release package module: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleasePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="merge-reviewer-release-test-"))
        self.skill = self.root / "skill"
        self.output = self.root / "dist"
        (self.skill / "agents").mkdir(parents=True)
        (self.skill / "references").mkdir()
        (self.skill / "scripts").mkdir()
        (self.skill / "SKILL.md").write_text("---\nname: merge-reviewer\n---\n", encoding="utf-8")
        (self.skill / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (self.skill / "agents" / "openai.yaml").write_text("interface:\n", encoding="utf-8")
        (self.skill / "references" / "review-rules.md").write_text("# Rules\n", encoding="utf-8")
        (self.skill / "scripts" / "git_review_context.py").write_text("print('ok')\n", encoding="utf-8")
        (self.skill / "scripts" / "__pycache__").mkdir()
        (self.skill / "scripts" / "__pycache__" / "stale.cpython-314.pyc").write_bytes(b"cache")
        (self.root / "tests" / "test_should_not_ship.py").parent.mkdir()
        (self.root / "tests" / "test_should_not_ship.py").write_text("not packaged\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_matching_tag_creates_installable_skill_zip(self) -> None:
        release = load_release_module()

        archive_path = release.build_release(
            tag="v0.1.0",
            version_file=self.skill / "VERSION",
            skill_dir=self.skill,
            output_dir=self.output,
        )

        self.assertEqual(archive_path, self.output / "merge-reviewer-0.1.0.zip")
        self.assertTrue(archive_path.exists())
        with ZipFile(archive_path) as archive:
            names = set(archive.namelist())
        self.assertIn("merge-reviewer/SKILL.md", names)
        self.assertIn("merge-reviewer/VERSION", names)
        self.assertIn("merge-reviewer/agents/openai.yaml", names)
        self.assertIn("merge-reviewer/references/review-rules.md", names)
        self.assertIn("merge-reviewer/scripts/git_review_context.py", names)
        self.assertNotIn("merge-reviewer/scripts/__pycache__/stale.cpython-314.pyc", names)
        self.assertNotIn("tests/test_should_not_ship.py", names)

    def test_mismatched_tag_stops_before_creating_archive(self) -> None:
        release = load_release_module()

        with self.assertRaisesRegex(release.ReleasePackageError, "does not match"):
            release.build_release(
                tag="v0.1.1",
                version_file=self.skill / "VERSION",
                skill_dir=self.skill,
                output_dir=self.output,
            )

        self.assertFalse(self.output.exists())

    def test_invalid_version_stops_before_creating_archive(self) -> None:
        release = load_release_module()
        (self.skill / "VERSION").write_text("0.1\n", encoding="utf-8")

        with self.assertRaisesRegex(release.ReleasePackageError, "valid SemVer"):
            release.build_release(
                tag="v0.1",
                version_file=self.skill / "VERSION",
                skill_dir=self.skill,
                output_dir=self.output,
            )

        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
