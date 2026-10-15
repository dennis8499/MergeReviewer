from __future__ import annotations
import json
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

from tests.support import load_module, run_git as git, run_python, write_text

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/merge-reviewer"


class DocumentationPackageTests(unittest.TestCase):
    def test_source_and_packaged_markdown_links_resolve(self):
        for directory in (ROOT, SKILL):
            for document in ([ROOT / "README.md"] if directory == ROOT else directory.rglob("*.md")):
                content = document.read_text(encoding="utf-8")
                for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
                    if target.startswith(("http:", "https:", "#")):
                        continue
                    relative = target.split("#", 1)[0]
                    self.assertTrue((document.parent / relative).is_file(), f"{document}: {target}")

    def test_installable_zip_contains_guide_and_no_retired_modules_and_runs_branches(self):
        release = load_module("documentation_release", ROOT / "scripts/package_release.py")
        with tempfile.TemporaryDirectory(prefix="guide-package-test-") as name:
            root = Path(name).resolve()
            archive = release.build_release(tag="v0.12.2", version_file=SKILL / "VERSION",
                                             skill_dir=SKILL, output_dir=root / "dist")
            with zipfile.ZipFile(archive) as zipped:
                names = set(zipped.namelist())
                for relative in ("references/usage.md", "references/report-style.md", "references/review-rules.md",
                                 "references/workspace-review.md", "scripts/review_workspace.py", "scripts/workspace_report.py"):
                    self.assertIn("merge-reviewer/" + relative, names)
                self.assertFalse(any("receipt_runtime/" in n or "delivery_binding" in n or "legacy_group_review" in n for n in names))
                self.assertEqual(5, sum(n.startswith("merge-reviewer/scripts/") and n.endswith(".py") for n in names))
                zipped.extractall(root / "installed")
            repo = root / "OrderService"
            git(root, "init", "-b", "main", str(repo))
            git(repo, "config", "user.name", "Guide Test")
            git(repo, "config", "user.email", "guide@example.invalid")
            write_text(repo / "app.py", "ready = False\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "baseline")
            git(repo, "switch", "-c", "feature/payment")
            write_text(repo / "app.py", "ready = True\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "change")
            scripts = root / "installed/merge-reviewer/scripts"
            prepared = run_python(scripts / "git_review_context.py", "--workspace", str(repo),
                                  "--project", "OrderService", "--base", "main", "--head", "feature/payment", "--pretty")
            self.assertEqual(0, prepared.returncode, prepared.stderr)
            manifest = json.loads(prepared.stdout)
            owned = Path(manifest["context_dir"])
            try:
                self.assertFalse(manifest["extension_enabled"])
                self.assertEqual([manifest["selected_repo"]], manifest["seed_repos"])
                inventory = run_python(scripts / "review_workspace.py", "inventory", "--context-dir", str(owned))
                self.assertEqual(0, inventory.returncode, inventory.stderr)
                self.assertIn("app.py", inventory.stdout)
                seed = next(r for r in manifest["repositories"] if r["id"] == manifest["selected_repo"])
                draft = {
                    "schema": "merge-reviewer-workspace-result/v3",
                    "summary": "已完成安裝套件的固定證據測試。",
                    "coverage": [{**row, "status": "reviewed", "evidence": [{
                        **row, "ref": seed["sources"][row["source"]]["ref"], "line_start": 1, "line_end": 1,
                    }]} for row in manifest["required_coverage"]],
                    "findings": [], "next_steps": [], "tests_executed": [],
                    "tests_not_executed": ["僅驗證 helper，未執行模型審查。"], "limitations": [],
                }
                write_text(owned / "draft.json", json.dumps(draft, ensure_ascii=False))
                published = run_python(scripts / "review_report.py", "--context-dir", str(owned),
                                       "--result", str(owned / "draft.json"))
                self.assertEqual(0, published.returncode, published.stderr)
                markdown = Path(published.stdout.strip())
                self.assertTrue(markdown.is_file())
                self.assertEqual(repo / "review-reports", markdown.parent)
                self.assertEqual([markdown], list(markdown.parent.iterdir()))
                base, head = seed["sources"]["base"]["ref"], seed["sources"]["head"]["ref"]
                self.assertRegex(markdown.name, rf"^merge-review-\d{{8}}T\d{{12}}Z-{base[:8]}-{head[:8]}\.md$")
                text = markdown.read_text(encoding="utf-8")
                self.assertIn(f"| 基礎分支 | `main` | `refs/heads/main` | `{base[:8]}` |", text)
                self.assertIn(f"| 比較分支 | `feature/payment` | `refs/heads/feature/payment` | `{head[:8]}` |", text)
                self.assertIn(f"`{base}`", text.split("## 技術附錄\n", 1)[1])
                self.assertIn(f"`{head}`", text.split("## 技術附錄\n", 1)[1])
            finally:
                if owned.exists():
                    cleaned = run_python(scripts / "review_session.py", "cleanup", "--context-dir", str(owned))
                    self.assertEqual(0, cleaned.returncode, cleaned.stderr)
            self.assertFalse(owned.exists())
