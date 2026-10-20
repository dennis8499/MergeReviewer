"""Isolated Git fixtures for evidence/format tests; these do not simulate Codex decisions."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

from tests.support import run_git as git, write_text

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/merge-reviewer/scripts"
sys.path.insert(0, str(SCRIPTS))
import git_review_context as api
import review_workspace as workspace
import workspace_report
import review_report
import review_session


class ReviewFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="branch-review-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.make_repo("api")
        git(self.repo, "switch", "-c", "feature")

    def make_repo(self, name, files=None):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "init", "-b", "main", str(target))
        git(target, "config", "user.name", "Review Test")
        git(target, "config", "user.email", "review@example.invalid")
        for relative, text in (files or {"app.py": "contract = 'old'\n"}).items():
            write_text(target / relative, text)
        git(target, "add", ".")
        git(target, "commit", "-m", "baseline")
        return target

    def commit(self, text="contract = 'new'\n", relative="app.py", repository=None):
        target = repository or self.repo
        write_text(target / relative, text)
        git(target, "add", ".")
        git(target, "commit", "-m", "change")

    def capture(self, *extra, workspace_root=None, project=None, base="main", head="feature"):
        arguments = ["--workspace", str(workspace_root or self.root),
                     "--project", str(project or self.repo), "--base", base, "--head", head, *extra]
        manifest = api.build_manifest(api.parse_args(arguments))
        context = Path(manifest["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        return context, manifest

    def draft(self, context, manifest):
        coverage, nodes = [], []
        for row in manifest["required_coverage"]:
            record = workspace.repository(manifest, row["repo"])
            allowed = workspace_report.allowed_sources(record, workspace_report.location(row))
            source = next(s for s in record["sources"] if s in allowed)
            evidence = {**row, "source": source, "ref": record["sources"][source]["ref"]}
            raw = workspace.fixed_bytes(context, record, source, row["path"], api)
            if raw.splitlines():
                evidence.update(line_start=1, line_end=1)
            if b"\0" in raw:
                coverage.append({**row, "status": "metadata-only", "reason": "二進位內容未完成語意檢查。", "evidence": []})
                continue
            entry = {**row, "status": "reviewed", "evidence": [evidence]}
            if manifest["extension_enabled"]:
                identifier = f"I-{len(nodes)+1:03}"
                contract_id = f"C-{len(nodes)+1:03}"
                line = raw.decode("utf-8").splitlines()[0]
                symbol = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", line)[0]
                terms = list(dict.fromkeys([symbol, line]))
                workspace.register_contract(context, manifest, {
                    "id": contract_id, "root": row, "kind": "behavior", "symbol": symbol,
                    "evidence": copy.deepcopy(evidence),
                    "search_terms": [{"query": term, "reason": "固定變更行中的契約名稱及上下文。"} for term in terms],
                }, api)
                nodes.append({"id": identifier, **row, "source": source, "status": "reviewed",
                              "contract_ids": [contract_id], "evidence": [evidence], "related_to": [],
                              "reason": "測試用的固定變更證據；不代表模型審查品質。"})
                entry["impact_ids"] = [identifier]
            coverage.append(entry)
        dispositions = []
        for record in manifest["repositories"]:
            state = "seed" if record["id"] == manifest["selected_repo"] else "not-related"
            reason = "使用者選定的唯一受審 Repo。" if state == "seed" else "測試夾具中的同名資料不是執行時契約。"
            ids = []
            if record["state"] == "error":
                state, reason = "unresolved", "無法擷取必要固定來源。"
            elif state != "seed":
                for contract in manifest["changed_contracts"]:
                    for term in contract["search_terms"]:
                        result = workspace.search(context, manifest, term["query"], [record["id"]], api,
                                                  contract_id=contract["id"])
                        ids += [s["id"] for s in result["searches"]]
            dispositions.append({"repo": record["id"], "status": state, "reason": reason, "search_ids": ids})
        seed = workspace.repository(manifest, manifest["selected_repo"])
        final_name = "merge-preview" if "merge-preview" in seed["sources"] else "head"
        final_ref = seed["sources"][final_name]["ref"]
        parent_reviews = []
        for view in manifest.get("required_merge_views", []):
            items = []
            for name in ("parent-" + view["parent"], final_name):
                source = seed["sources"][name]
                available = {entry["path"] for entry in source["files"] if entry["kind"] == "blob"}
                path = next((path for path in view["paths"] if path in available), None)
                if path:
                    items.append({"repo": seed["id"], "source": name, "ref": source["ref"], "path": path})
            parent_reviews.append({"id": view["id"], "status": "reviewed", "reason": "測試作者對照固定parent與最終版本。",
                                   "evidence": items, "final_ref": final_ref})
        return {"schema": workspace.RESULT, "summary": "已完成所選範圍的固定證據檢查。", "merge_parent_review": parent_reviews,
                "coverage": coverage, "impact_coverage": nodes, "repository_coverage": dispositions,
                "findings": [], "next_steps": [], "tests_executed": [],
                "tests_not_executed": ["未執行專案測試；此結果為測試夾具。"], "limitations": []}

    def attach_consumer(self, draft, manifest, consumer, *, reviewed=True, connected=True):
        record = next(r for r in manifest["repositories"] if r["path"] == str(consumer))
        evidence = {"repo": record["id"], "source": "head", "ref": record["sources"]["head"]["ref"],
                    "path": "app.py", "line_start": 1, "line_end": 1}
        node = {"id": "I-consumer", "repo": record["id"], "source": "head", "path": "app.py",
                "status": "reviewed" if reviewed else "not-reviewed",
                "contract_ids": list(draft["impact_coverage"][0]["contract_ids"]),
                "related_to": [draft["impact_coverage"][0]["id"]] if connected else [],
                "reason": "消費端讀取本次變更的固定契約。",
                "evidence": [evidence] if reviewed else []}
        draft["impact_coverage"].append(node)
        next(r for r in draft["repository_coverage"] if r["repo"] == record["id"])["status"] = "related"
        return node

    def finding(self, draft, priority="P1"):
        return {"id": "F-001", "priority": priority, "title": "契約改變後消費端無法處理資料",
                "impact": "使用者的交易資料無法完成處理。",
                "trigger": "消費端收到採用新格式的資料。",
                "expected": "既有消費端仍能取得必要資訊。",
                "actual": "消費端讀取不到原本的欄位。",
                "recommendation": "保留既有契約或同步調整消費端。",
                "owner": "開發", "scenario_source": "code-derived",
                "evidence": copy.deepcopy(draft["coverage"][0]["evidence"])}
