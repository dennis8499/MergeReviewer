"""Receipt capture/publication with real commits and no installed Megin Skills."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from support import create_repository_fixture, run_git, load_module

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/merge-reviewer/scripts"
sys.path.insert(0, str(SCRIPTS))
import delivery_binding as binding
import git_review_context as context
import review_report as report
import review_session as session


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DeliveryBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo, _ = create_repository_fixture(self.root)
        self.work, self.version = "work-20261009-receipt", "plan-1"
        self.prefix = f"docs/work/{self.work}/"
        self.folder = self.repo / self.prefix
        (self.folder / self.version).mkdir(parents=True)
        (self.folder / "evidence").mkdir()
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        records = [self.prefix + "evidence/" + name for name in
                   ("quality.json", "delivery.json", "writer.md", "review.md", "acceptance.md", "check.log")]
        self.contract = {"schema": "megin-repo-quality-contract/v1", "work_id": self.work,
            "plan_version": self.version, "base_branch": "main", "base_commit": self.base,
            "feature_branch": "feature", "allowed_paths": ["base.txt", self.prefix],
            "checks": [{"id": "unit", "kind": "test", "command": "test unit", "cwd": "."}],
            "process_records": records, "quality_ref": records[0], "delivery_ref": records[1], "skills_sha256": "f" * 64}
        self.write(self.prefix + self.version + "/quality-contract.json", self.contract)
        self.write(self.prefix + self.version + "/plan.md", "accepted plan\n")
        self.write(self.prefix + "requirements.md", "returns accepted\n")
        self.write("base.txt", "accepted\n")
        run_git(self.repo, "add", "base.txt", self.prefix + self.version, self.prefix + "requirements.md")
        run_git(self.repo, "commit", "-m", "accepted feature")
        self.feature = run_git(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "switch", "main")
        run_git(self.repo, "merge", "--no-ff", "--no-edit", "feature")
        self.merge = run_git(self.repo, "rev-parse", "HEAD")
        tree_rows = []
        raw = run_git(self.repo, "ls-tree", "-r", self.feature)
        for row in raw.splitlines():
            metadata, path = row.split("\t", 1)
            tree_rows.append({"path": path, "content": metadata.split()[2]})
        tree_rows.sort(key=lambda r: r["path"])
        self.snapshot = digest(tree_rows)
        raw_claims = {
            "writer.md": "- context: writer\n- snapshot: " + self.snapshot + "\n",
            "review.md": "- context: reviewer\n- verdict: APPROVED\n- snapshot: " + self.snapshot + "\n",
            "acceptance.md": f"- work_id: {self.work}\n- version: acceptance-1\n- snapshot: {self.snapshot}\n- verdict: ACCEPTED\n",
            "check.log": "Working directory: .\nCommand: test unit\nExit code: 0\n1 test passed\n",
        }
        for name, text in raw_claims.items():
            self.write(self.prefix + "evidence/" + name, text)

        def reference(name, claims):
            path = self.prefix + "evidence/" + name
            lines = (self.repo / path).read_text(encoding="utf-8").splitlines()
            return {"path": path, "sha256": hashlib.sha256((self.repo / path).read_bytes()).hexdigest(),
                    "claims": {key: {"line": number, "text": lines[number - 1]} for key, number in claims.items()}}

        quality = {"schema": "megin-repo-quality-evidence/v1", "work_id": self.work,
            "plan_version": self.version, "snapshot": self.snapshot,
            "writer": {"context": "writer", "snapshot": self.snapshot, "source": reference("writer.md", {"context": 1, "snapshot": 2})},
            "review": {"context": "reviewer", "verdict": "APPROVED", "snapshot": self.snapshot,
                       "source": reference("review.md", {"context": 1, "verdict": 2, "snapshot": 3})},
            "acceptance": {"work_id": self.work, "version": "acceptance-1", "snapshot": self.snapshot, "verdict": "ACCEPTED",
                           "source": reference("acceptance.md", {"work_id": 1, "version": 2, "snapshot": 3, "verdict": 4})},
            "sources": [], "checks": [{"id": "unit", "status": "passed", "exit_code": 0,
                "executed": 1, "failed": 0, "skipped": 0, "snapshot": self.snapshot,
                "output": {**reference("check.log", {"cwd": 1, "command": 2, "exit_code": 3}), "line": 4, "text": "1 test passed"}}]}
        stdout = json.dumps({"gate": "delivery", "ok": True, "snapshot": self.snapshot}) + "\n"
        delivery = {"schema": "megin-repo-delivery-result/v1", "work_id": self.work,
            "plan_version": self.version, "accepted_snapshot": self.snapshot, "feature_commit": self.feature,
            "merge_commit": self.merge, "delivery_gate": {"stdout": stdout, "exit_code": 0,
                                                          "sha256": hashlib.sha256(stdout.encode()).hexdigest()}}
        self.write(records[0], quality)
        self.write(records[1], delivery)
        self.receipt = {"schema": "megin-repo-delivery-receipt/v1", "work_id": self.work,
            "plan_version": self.version, "repo_root": str(self.repo.resolve()), "accepted_snapshot": self.snapshot,
            "local_delivery_complete": True, "base_commit": self.base, "feature_commit": self.feature,
            "merge_commit": self.merge, "tree_sha": run_git(self.repo, "rev-parse", self.feature + "^{tree}"),
            "contract_ref": self.prefix + self.version + "/quality-contract.json", "quality_ref": records[0], "delivery_ref": records[1],
            "record_files": [{"path": p.relative_to(self.repo).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                             for p in sorted(self.folder.rglob("*")) if p.is_file()]}
        self.receipt["receipt_sha256"] = digest(self.receipt)
        self.receipt_file = self.root / "receipt.json"
        self.receipt_file.write_text(json.dumps(self.receipt), encoding="utf-8")

    def write(self, relative, value):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n" if isinstance(value, dict) else value, encoding="utf-8")

    def capture(self, head=None, extra=()):
        args = context.parse_args(["--workspace", str(self.repo), "--base", self.base,
            "--head", head or self.feature, "--mode", "direct", "--no-fetch",
            "--megin-receipt", str(self.receipt_file), *extra])
        manifest = context.build_manifest(args)
        path = Path(manifest["context_dir"])
        self.addCleanup(lambda: session.cleanup_session(path) if path.exists() else None)
        return manifest, path

    def draft(self, manifest):
        coverage = []
        for change in manifest["changed_files"]:
            path = change["path"]
            coverage.append({"path": path, "status": "reviewed", "evidence": [
                {"source": "head", "path": path, "ref": manifest["head_sha"], "line_start": 1, "line_end": 1}]})
        return {"schema_version": 1, "summary": "已檢查固定交付版本。", "coverage": coverage,
                "findings": [], "next_steps": [{"action": "依驗收流程續作。", "owner": "QA"}],
                "tests_executed": [], "tests_not_executed": ["未執行產品測試"], "limitations": []}

    def test_capture_and_publication_work_without_any_installed_megin(self):
        self.assertFalse((self.repo / ".agents/skills/megin").exists())
        manifest, path = self.capture()
        self.assertEqual(self.receipt["receipt_sha256"], manifest["megin_binding"]["receipt_sha256"])
        draft = path / "draft.json"
        draft.write_text(json.dumps(self.draft(manifest), ensure_ascii=False), encoding="utf-8")
        markdown, data = report.publish_report(path, draft, self.root / "reports", include_json=True)
        self.assertIn(self.work, markdown.read_text(encoding="utf-8"))
        self.assertIn(self.receipt["receipt_sha256"], markdown.read_text(encoding="utf-8"))
        self.assertEqual(manifest["megin_binding"], json.loads(data.read_text(encoding="utf-8"))["megin_binding"])
        self.assertFalse(path.exists())

    def test_merge_commit_is_also_a_valid_fixed_head(self):
        manifest, _ = self.capture(self.merge)
        self.assertEqual(self.merge, manifest["megin_binding"]["head_sha"])

    def test_other_heads_and_working_tree_capture_are_rejected_and_cleaned(self):
        failed = self.root / "failed-context"
        with self.assertRaises((ValueError, context.ReviewContextError)):
            self.capture(self.base, ["--context-dir", str(failed)])
        self.assertFalse(failed.exists())
        with self.assertRaises((ValueError, context.ReviewContextError)):
            self.capture(extra=["--include-working-tree"])

    def test_receipt_digest_foreign_repo_and_commit_identity_are_rejected(self):
        for mutate in (lambda r: r.update(receipt_sha256="0" * 64),
                       lambda r: r.update(repo_root=str(self.root)),
                       lambda r: r.update(feature_commit=self.base)):
            value = copy.deepcopy(self.receipt)
            mutate(value)
            if value["receipt_sha256"] != "0" * 64:
                value["receipt_sha256"] = digest({k: v for k, v in value.items() if k != "receipt_sha256"})
            with self.assertRaises(ValueError):
                binding.validate_receipt(value, self.repo)

    def test_later_live_product_changes_do_not_rewrite_history(self):
        self.write("base.txt", "later live bytes\n")
        binding.validate_receipt(self.receipt, self.repo)
        manifest, _ = self.capture()
        self.assertEqual(self.feature, manifest["head_sha"])

    def test_changed_raw_records_block_publication_and_clean_context(self):
        manifest, path = self.capture()
        self.write(self.prefix + "evidence/check.log", "tampered\n")
        draft = path / "draft.json"
        draft.write_text(json.dumps(self.draft(manifest)), encoding="utf-8")
        with self.assertRaises(report.ReportValidationError):
            report.publish_report(path, draft, self.root / "reports")
        self.assertFalse(path.exists())
        self.assertFalse((self.root / "reports").exists())

    def test_changed_binding_cannot_be_published(self):
        manifest, path = self.capture()
        manifest["megin_binding"]["plan_version"] = "plan-other"
        (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(report.ReportValidationError):
            report.validate_result(self.draft(manifest), manifest, path)

    def test_unrelated_context_flags_cannot_bypass_native_binding(self):
        manifest, path = self.capture()
        manifest["mr_context"] = {"schema": "untrusted"}
        manifest["megin_binding"]["receipt_sha256"] = "0" * 64
        with self.assertRaises(report.ReportValidationError):
            report.validate_result(self.draft(manifest), manifest, path)

    def test_null_binding_cannot_erase_delivery_identity(self):
        manifest, path = self.capture()
        manifest["megin_binding"] = None
        with self.assertRaises(report.ReportValidationError):
            report.validate_result(self.draft(manifest), manifest, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
