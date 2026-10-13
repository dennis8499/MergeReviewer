"""Standalone historical proof using real commits and bundled native policy."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/merge-reviewer/scripts"))
import delivery_binding_v2 as binding


class FeatureReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="standalone-native-receipts-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.fixtures = {}
        for mode in ("repo", "group"):
            folder = Path(cls.temporary.name) / mode
            folder.mkdir()
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/native_receipt_fixture.py"), str(folder), mode],
                                    capture_output=True, text=True, encoding="utf-8", timeout=600)
            if result.returncode:
                raise AssertionError(result.stderr)
            receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
            cls.fixtures[mode] = receipt

    def manifest(self, receipt):
        root = Path(receipt["workspace_root"])
        seeds = receipt["repositories"]
        return {"schema": "merge-reviewer-workspace-context/v2", "workspace": str(root), "target": "branch",
                "scope": "group", "seed_repos": [f"repo-{i:03}" for i in range(len(seeds))],
                "repositories": [{"id": f"repo-{i:03}", "path": str(root if row["repo_path"] == "." else root / row["repo_path"]),
                    "native": {"base_sha": row["base_commit"], "head_sha": row["feature_commit"]},
                    "sources": {"head": {"ref": row["feature_commit"]}}} for i, row in enumerate(seeds)]}

    def test_native_repo_and_group_export_verify_without_installed_megin(self):
        for mode, receipt in self.fixtures.items():
            with self.subTest(mode=mode):
                self.assertEqual(receipt, binding.validate_receipt(receipt, Path(receipt["workspace_root"])))
                manifest = self.manifest(receipt)
                manifest["megin_binding"] = binding.bind_delivery(receipt, manifest)
                self.assertEqual(manifest["megin_binding"], binding.verify_manifest_binding(manifest))

    def test_digest_and_rehashed_delivery_forgery_are_rejected(self):
        for mutate in (lambda r: r.update(accepted_snapshot="f" * 64),
                       lambda r: r["repositories"][0].update(feature_commit=r["repositories"][0]["base_commit"])):
            receipt = copy.deepcopy(self.fixtures["group"])
            mutate(receipt)
            receipt["receipt_sha256"] = binding.canonical_digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
            with self.assertRaises(ValueError):
                binding.validate_receipt(receipt, Path(receipt["workspace_root"]))

    def test_later_working_edits_do_not_change_historical_proof(self):
        receipt = self.fixtures["repo"]
        root = Path(receipt["workspace_root"])
        app = root / "app.py"
        previous = app.read_bytes()
        try:
            app.write_text("unrelated later working edit\n", encoding="utf-8")
            self.assertEqual(receipt, binding.validate_receipt(receipt, root))
        finally:
            app.write_bytes(previous)

    def test_missing_and_tampered_protected_records_are_rejected(self):
        receipt = self.fixtures["group"]
        root = Path(receipt["workspace_root"])
        record = next(r for r in receipt["record_files"] if r["path"].endswith("plan.md"))
        path = root / record["path"]
        previous = path.read_bytes()
        try:
            path.write_text("changed approved plan\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "record changed"):
                binding.validate_receipt(receipt, root)
        finally:
            path.write_bytes(previous)
        fake = copy.deepcopy(receipt)
        fake["record_files"] = [r for r in fake["record_files"] if r["path"] != record["path"]]
        fake["receipt_sha256"] = binding.canonical_digest({k: v for k, v in fake.items() if k != "receipt_sha256"})
        with self.assertRaisesRegex(ValueError, "differs"):
            binding.validate_receipt(fake, root)

    def test_receipt_seed_versions_scope_and_binding_are_revalidated(self):
        receipt = self.fixtures["group"]
        manifest = self.manifest(receipt)
        manifest["megin_binding"] = binding.bind_delivery(receipt, manifest)
        for mutation in ("head", "base", "scope", "binding"):
            altered = copy.deepcopy(manifest)
            if mutation == "head":
                altered["repositories"][0]["sources"]["head"]["ref"] = "a" * 40
            elif mutation == "base":
                altered["repositories"][0]["native"]["base_sha"] = "a" * 40
            elif mutation == "scope":
                altered["seed_repos"].pop()
            else:
                altered["megin_binding"]["receipt_sha256"] = "f" * 64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                binding.verify_manifest_binding(altered)

    def test_group_receipt_captures_all_fixed_seeds_and_publishes_bound_report(self):
        import git_review_context as api
        import review_workspace as workspace
        import review_report
        import review_session
        receipt = self.fixtures["group"]
        root = Path(receipt["workspace_root"])
        receipt_file = root.parent / "receipt.json"
        manifest = api.build_manifest(api.parse_args(["--workspace", str(root), "--megin-receipt", str(receipt_file),
                                                      "--mode", "direct", "--no-fetch"]))
        context = Path(manifest["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        self.assertEqual("merge-reviewer-workspace-context/v2", manifest["schema"])
        self.assertEqual(2, len(manifest["seed_repos"]))
        nodes, coverage = [], []
        for index, row in enumerate(manifest["required_coverage"]):
            repo = workspace.repository(manifest, row["repo"])
            source = row["source"]
            lines = workspace.fixed_bytes(context, repo, source, row["path"], api).decode("utf-8").splitlines()
            evidence = {**row, "ref": repo["sources"][source]["ref"], "line_start": 2, "line_end": 2}
            contract_id, node_id = f"C-{index:03}", f"I-{index:03}"
            workspace.register_contract(context, manifest, {
                "id": contract_id, "root": row, "kind": "behavior", "symbol": "accepted", "evidence": evidence,
                "search_terms": [{"query": term, "reason": "Returned value and its source expression."}
                                 for term in ("accepted", lines[1])]}, api)
            nodes.append({**row, "id": node_id, "status": "reviewed", "reason": "Reviewed delivered return behavior.",
                          "evidence": [evidence], "related_to": [], "contract_ids": [contract_id]})
            coverage.append({**row, "status": "reviewed", "evidence": [evidence], "impact_ids": [node_id]})
        draft = {"schema": workspace.RESULT, "summary": "已核對交付版本及相關流程。", "coverage": coverage,
                 "impact_coverage": nodes, "repository_coverage": [{"repo": r["id"], "status": "seed",
                    "reason": "Fixed delivered seed.", "search_ids": []} for r in manifest["repositories"]],
                 "findings": [], "limitations": [], "next_steps": [{"owner": "開發", "action": "依審查結果處理。"}],
                 "tests_executed": [], "tests_not_executed": ["僅進行靜態審查。"]}
        for repo in manifest["repositories"]:
            disposition = next(r for r in draft["repository_coverage"] if r["repo"] == repo["id"])
            for contract in manifest["changed_contracts"]:
                if contract["evidence"]["repo"] == repo["id"]:
                    continue
                for term in contract["search_terms"]:
                    records = workspace.search(context, manifest, term["query"], [repo["id"]], api,
                                               repo["review_source"], contract["id"])["searches"]
                    disposition["search_ids"].extend(r["id"] for r in records)
        draft_file = context / "draft.json"
        draft_file.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        markdown, result_file = review_report.publish_report(context, draft_file, include_json=True)
        self.assertTrue(markdown.is_file())
        self.assertFalse(context.exists())
        result = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(receipt["receipt_sha256"], result["megin_binding"]["receipt_sha256"])
        self.assertTrue(result["review_complete"])

    def test_group_receipt_repo_scope_requires_one_explicit_delivery_repo(self):
        import git_review_context as api
        import review_session
        receipt = self.fixtures["group"]
        root = Path(receipt["workspace_root"])
        base_args = ["--workspace", str(root), "--megin-receipt", str(root.parent / "receipt.json"),
                     "--scope", "repo", "--mode", "direct", "--no-fetch"]
        with self.assertRaisesRegex(api.ReviewContextError, "requires --project"):
            api.build_manifest(api.parse_args(base_args))
        selected = root / receipt["repositories"][0]["repo_path"]
        manifest = api.build_manifest(api.parse_args(base_args + ["--project", str(selected)]))
        self.addCleanup(review_session.cleanup_session, Path(manifest["context_dir"]))
        self.assertEqual("repo", manifest["scope"])
        self.assertEqual([str(selected)], [r["path"] for r in manifest["repositories"]])
        self.assertEqual(1, len(manifest["megin_binding"]["reviewed_repositories"]))
        self.assertEqual(2, len(manifest["megin_binding"]["repositories"]))


if __name__ == "__main__":
    unittest.main()
