from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import re
import tempfile
import unittest
from unittest import mock
import zipfile

from tests.support import load_module, run_git as git, run_python, write_text

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/merge-reviewer/scripts"
sys.path.insert(0, str(SCRIPTS))
import review_workspace as workspace
import workspace_report
import review_session


class WorkspaceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workspace-review-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = load_module("workspace_test_context", SCRIPTS / "git_review_context.py")
        self.repo = self.make_repo("api")

    def make_repo(self, name, *, unborn=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "init", "-b", "main", str(path))
        git(path, "config", "user.name", "Review Tests")
        git(path, "config", "user.email", "review@example.invalid")
        if not unborn:
            write_text(path / "app.py", "contract = 'old'\n")
            git(path, "add", ".")
            git(path, "commit", "-m", "base")
        return path

    def capture(self, target="staged", scope="auto", project=True, *extra):
        args = ["--workspace", str(self.root), "--scope", scope, "--target", target]
        if project:
            args += ["--project", str(self.repo)]
        args += list(extra)
        manifest = self.api.build_manifest(self.api.parse_args(args))
        context = Path(manifest["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        return context, manifest

    def change(self):
        write_text(self.repo / "app.py", "contract = 'new'\n")
        git(self.repo, "add", "app.py")

    def draft(self, context, manifest):
        nodes, coverage = [], []
        for row in manifest["required_coverage"]:
            repo = workspace.repository(manifest, row["repo"])
            allowed = workspace_report.allowed_sources(repo, workspace_report.location(row))
            name = next(s for s in repo["sources"] if s in allowed)
            evidence = {**row, "source": name, "ref": repo["sources"][name]["ref"]}
            raw = workspace.fixed_bytes(context, repo, name, row["path"], self.api)
            if raw.splitlines():
                evidence.update(line_start=1, line_end=1)
            identifier = f"I-{len(nodes) + 1:03}"
            contract_ids = []
            if manifest["scope"] == "group":
                symbol = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", raw.decode("utf-8"))[0]
                contract_id = f"C-{len(nodes) + 1:03}"
                exact_line = raw.decode("utf-8").splitlines()[0]
                terms = list(dict.fromkeys([symbol, exact_line]))
                if len(terms) < 2:
                    terms.append("alias_" + symbol)
                workspace.register_contract(context, manifest, {
                    "id": contract_id, "root": row, "kind": "behavior", "symbol": symbol,
                    "evidence": evidence, "search_terms": [{"query": term, "reason": "Contract name or contextual alias."} for term in terms]
                }, self.api)
                contract_ids = [contract_id]
            nodes.append({"id": identifier, **row, "source": name, "status": "reviewed", "evidence": [evidence],
                          "related_to": [], "contract_ids": contract_ids, "reason": "Reviewed changed contract and its local behavior."})
            coverage.append({**row, "status": "reviewed", "evidence": [evidence], "impact_ids": [identifier]})
        dispositions = []
        for repo in manifest["repositories"]:
            record = {"repo": repo["id"], "status": "seed" if repo["id"] in manifest["seed_repos"] else "not-related",
                      "reason": "Selected change." if repo["id"] in manifest["seed_repos"] else "Searched contract; no matching consumers.",
                      "search_ids": []}
            if record["status"] == "not-related" and manifest["required_coverage"]:
                for contract in manifest["changed_contracts"]:
                    for source in (("index", "working-tree") if manifest["target"] == "quick" else (repo["review_source"],)):
                        for term in contract["search_terms"]:
                            found = workspace.search(context, manifest, term["query"], [repo["id"]], self.api,
                                                     source, contract["id"])
                            record["search_ids"] += [s["id"] for s in found["searches"]]
            dispositions.append(record)
        return {"schema": workspace.RESULT, "summary": "已檢查選定變更及功能鏈。", "coverage": coverage,
                "impact_coverage": nodes, "repository_coverage": dispositions, "findings": [], "limitations": [],
                "next_steps": [{"owner": "開發", "action": "依審查結果處理。"}],
                "tests_executed": [], "tests_not_executed": ["僅做靜態審查。"]}

    def related(self, draft, manifest, repo, *, link=True):
        captured = next(r for r in manifest["repositories"] if r["path"] == str(repo))
        name = captured["review_source"]
        evidence = {"repo": captured["id"], "source": name, "ref": captured["sources"][name]["ref"],
                    "path": "app.py", "line_start": 1, "line_end": 1}
        node = {"id": "I-consumer", "repo": captured["id"], "source": name, "path": "app.py", "status": "reviewed",
                "evidence": [evidence], "related_to": [draft["impact_coverage"][0]["id"]] if link else [],
                "contract_ids": draft["impact_coverage"][0]["contract_ids"],
                "reason": "Consumer reads the changed API contract."}
        draft["impact_coverage"].append(node)
        next(r for r in draft["repository_coverage"] if r["repo"] == captured["id"])["status"] = "related"
        return node

    def test_staged_and_uncommitted_freeze_distinct_versions_without_remote_or_mutation(self):
        self.change()
        write_text(self.repo / "app.py", "contract = 'working'\n")
        index = self.repo / ".git/index"
        before, head, status = index.read_bytes(), git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain")
        for target, content in (("staged", b"contract = 'new'\n"), ("uncommitted", b"contract = 'working'\n")):
            context, manifest = self.capture(target)
            repo = manifest["repositories"][0]
            self.assertEqual(content, workspace.fixed_bytes(context, repo, repo["review_source"], "app.py", self.api))
            result = workspace_report.validate(self.draft(context, manifest), manifest, context)
            self.assertTrue(result["review_complete"])
        self.assertEqual(before, index.read_bytes())
        self.assertEqual(head, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(status, git(self.repo, "status", "--porcelain"))
        self.assertEqual("", git(self.repo, "remote"))

    def test_uncommitted_preserves_forced_ignored_and_excludes_generated_reports(self):
        write_text(self.repo / ".gitignore", "*.ignored\n")
        write_text(self.repo / "forced.ignored", "selected\n")
        git(self.repo, "add", "-f", "forced.ignored")
        write_text(self.repo / "skip.ignored", "ignored\n")
        write_text(self.repo / "review-reports/previous.md", "generated\n")
        write_text(self.repo / "untracked.py", "new = True\n")
        context, manifest = self.capture("uncommitted")
        paths = {r["path"] for r in manifest["required_coverage"]}
        self.assertTrue({"forced.ignored", "untracked.py"} <= paths)
        self.assertNotIn("skip.ignored", paths)
        self.assertNotIn("review-reports/previous.md", paths)

    def test_uncommitted_reads_saved_hidden_index_flags_without_changing_real_index(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                git(self.repo, "update-index", "--no-assume-unchanged", "--no-skip-worktree", "app.py")
                git(self.repo, "update-index", flag, "app.py")
                write_text(self.repo / "app.py", "saved = True\n")
                before = (self.repo / ".git/index").read_bytes()
                context, manifest = self.capture("uncommitted", "repo")
                repo = manifest["repositories"][0]
                self.assertEqual(b"saved = True\n", workspace.fixed_bytes(context, repo, "working-tree", "app.py", self.api))
                self.assertEqual(["app.py"], [r["path"] for r in manifest["required_coverage"]])
                self.assertEqual(before, (self.repo / ".git/index").read_bytes())

    def test_missing_skip_worktree_path_does_not_become_a_false_deletion(self):
        git(self.repo, "update-index", "--skip-worktree", "app.py")
        (self.repo / "app.py").unlink()
        write_text(self.repo / "new.py", "saved = True\n")
        context, manifest = self.capture("uncommitted", "repo")
        repo = manifest["repositories"][0]
        self.assertEqual(b"contract = 'old'\n", workspace.fixed_bytes(context, repo, "working-tree", "app.py", self.api))
        self.assertEqual(["new.py"], [r["path"] for r in manifest["required_coverage"]])

    def test_unborn_repo_and_intent_to_add_do_not_require_initial_commit(self):
        self.repo = self.make_repo("new", unborn=True)
        write_text(self.repo / "app.py", "new = True\n")
        git(self.repo, "add", "-N", "app.py")
        _, staged = self.capture("staged", "repo")
        _, working = self.capture("uncommitted", "repo")
        self.assertEqual([], staged["required_coverage"])
        self.assertEqual(["app.py"], [r["path"] for r in working["required_coverage"]])

    def test_branch_seed_uses_explicit_refs_and_related_repo_uses_its_own_head(self):
        consumer = self.make_repo("consumer")
        git(self.repo, "switch", "-c", "feature")
        self.change()
        git(self.repo, "commit", "-m", "contract change")
        write_text(self.repo / "app.py", "local = 'excluded'\n")
        context, manifest = self.capture("branch", "auto", True, "--base", "main", "--head", "feature", "--no-fetch")
        self.assertEqual("group", manifest["scope"])
        api_repo = next(r for r in manifest["repositories"] if r["path"] == str(self.repo))
        other = next(r for r in manifest["repositories"] if r["path"] == str(consumer))
        self.assertEqual(git(consumer, "rev-parse", "HEAD"), other["sources"]["head"]["ref"])
        self.assertNotIn("base", other["sources"])
        self.assertEqual(b"contract = 'new'\n", workspace.fixed_bytes(context, api_repo, "head", "app.py", self.api))
        self.assertIn("merge-preview", api_repo["sources"])
        self.assertTrue(workspace_report.validate(self.draft(context, manifest), manifest, context)["review_complete"])

    def test_explicit_repo_scope_does_not_discover_sibling_evidence(self):
        self.make_repo("consumer")
        self.change()
        _, manifest = self.capture(scope="repo")
        self.assertEqual([str(self.repo)], [r["path"] for r in manifest["repositories"]])

    def test_direct_mode_keeps_actual_merge_base_and_deleted_path_comparison_source(self):
        common = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-c", "feature")
        self.change()
        git(self.repo, "commit", "-m", "feature")
        git(self.repo, "switch", "main")
        write_text(self.repo / "main-only.py", "main = True\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "main addition")
        context, manifest = self.capture("branch", "repo", True, "--base", "main", "--head", "feature", "--mode", "direct", "--no-fetch")
        repo = manifest["repositories"][0]
        self.assertEqual(common, repo["sources"]["merge-base"]["ref"])
        self.assertEqual({"base"}, workspace_report.allowed_sources(repo, (repo["id"], "head", "main-only.py")))
        self.assertTrue(workspace_report.validate(self.draft(context, manifest), manifest, context)["review_complete"])

    def test_staged_split_index_snapshot_preserves_shared_index_and_exact_content(self):
        self.change()
        git(self.repo, "update-index", "--split-index")
        index = self.repo / ".git/index"
        before = index.read_bytes()
        shared = {p.name: p.read_bytes() for p in (self.repo / ".git").glob("sharedindex.*")}
        self.assertTrue(shared)
        write_text(self.repo / "app.py", "contract = 'working'\n")
        context, manifest = self.capture(scope="repo")
        repo = manifest["repositories"][0]
        self.assertEqual(b"contract = 'new'\n", workspace.fixed_bytes(context, repo, "index", "app.py", self.api))
        self.assertEqual(before, index.read_bytes())
        self.assertEqual(shared, {p.name: p.read_bytes() for p in (self.repo / ".git").glob("sharedindex.*")})

    def test_duplicate_names_are_selected_by_path_and_use_distinct_ids(self):
        self.repo = self.make_repo("one/service")
        other = self.make_repo("two/service")
        self.change()
        _, manifest = self.capture()
        services = [r for r in manifest["repositories"] if r["name"] == "service"]
        self.assertEqual(2, len(services))
        self.assertEqual(2, len({r["id"] for r in services}))
        self.assertEqual(str(self.repo), next(r["path"] for r in services if r["id"] in manifest["seed_repos"]))
        with self.assertRaises(self.api.ReviewContextError):
            self.api.choose_repository(self.root, None, "service")

    def test_group_local_without_project_only_seeds_changed_repositories(self):
        other = self.make_repo("consumer")
        self.change()
        context, manifest = self.capture(project=False)
        self.assertEqual([str(self.repo)], [r["path"] for r in manifest["repositories"] if r["id"] in manifest["seed_repos"]])
        self.assertTrue(workspace_report.validate(self.draft(context, manifest), manifest, context)["review_complete"])

    def test_staged_review_ignores_subsequent_working_only_edit_but_index_drift_is_incomplete(self):
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        write_text(self.repo / "app.py", "working only\n")
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        git(self.repo, "add", "app.py")
        self.assertEqual("審查未完成", workspace_report.validate(draft, manifest, context)["review_status"])

    def test_uncommitted_bytes_remain_fixed_after_live_edits_and_result_is_incomplete(self):
        self.change()
        context, manifest = self.capture("uncommitted")
        draft = self.draft(context, manifest)
        write_text(self.repo / "app.py", "later edit\n")
        repo = manifest["repositories"][0]
        self.assertEqual(b"contract = 'new'\n", workspace.fixed_bytes(context, repo, "working-tree", "app.py", self.api))
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_excluded_candidate_version_drift_invalidates_its_exclusion(self):
        consumer = self.make_repo("candidate")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        write_text(consumer / "app.py", "contract = 'new consumer'\n")
        # Working-only edits do not affect a staged-source exclusion.
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        git(consumer, "add", "app.py")
        result = workspace_report.validate(draft, manifest, context)
        self.assertEqual("審查未完成", result["review_status"])
        self.assertTrue(any("擷取後改變" in gap for gap in result["limitations"]))

    def test_missing_change_and_impact_coverage_cannot_pass(self):
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        missing = copy.deepcopy(draft)
        missing["coverage"] = []
        with self.assertRaisesRegex(ValueError, "every changed"):
            workspace_report.validate(missing, manifest, context)
        draft["coverage"][0]["impact_ids"] = []
        with self.assertRaisesRegex(ValueError, "impact nodes"):
            workspace_report.validate(draft, manifest, context)

    def test_connected_consumer_and_cycle_are_reviewed_without_expanding_unrelated_repo(self):
        consumer = self.make_repo("consumer")
        unrelated = self.make_repo("unrelated")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        node = self.related(draft, manifest, consumer)
        draft["impact_coverage"][0]["related_to"] = [node["id"]]
        result = workspace_report.validate(draft, manifest, context)
        self.assertTrue(result["review_complete"])
        unrelated_id = next(r["id"] for r in manifest["repositories"] if r["path"] == str(unrelated))
        self.assertNotIn(unrelated_id, result["involved_repos"])

    def test_disconnected_impact_node_and_excluded_reachable_repo_are_rejected(self):
        consumer = self.make_repo("consumer")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        node = self.related(draft, manifest, consumer, link=False)
        with self.assertRaisesRegex(ValueError, "disconnected"):
            workspace_report.validate(draft, manifest, context)
        draft["coverage"][0]["impact_ids"].append(node["id"])
        with self.assertRaisesRegex(ValueError, "own frozen evidence"):
            workspace_report.validate(draft, manifest, context)
        draft["coverage"][0]["impact_ids"].pop()
        node["related_to"] = [draft["impact_coverage"][0]["id"]]
        next(r for r in draft["repository_coverage"] if r["repo"] == node["repo"])["status"] = "not-related"
        with self.assertRaisesRegex(ValueError, "cannot be excluded"):
            workspace_report.validate(draft, manifest, context)

    def test_unreviewed_reachable_node_forces_incomplete_even_without_findings(self):
        consumer = self.make_repo("consumer")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        node = self.related(draft, manifest, consumer)
        node.update(status="not-reviewed", reason="Dynamic request destination cannot be confirmed.", evidence=[])
        self.assertEqual("審查未完成", workspace_report.validate(draft, manifest, context)["review_status"])

    def test_candidate_repo_requires_search_record_not_just_an_exclusion_claim(self):
        self.make_repo("consumer")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        next(r for r in draft["repository_coverage"] if r["status"] == "not-related")["search_ids"] = []
        with self.assertRaisesRegex(ValueError, "completed searches"):
            workspace_report.validate(draft, manifest, context)

    def test_group_search_rejects_unrelated_queries_and_unproven_aliases(self):
        self.make_repo("consumer")
        self.change()
        context, manifest = self.capture()
        self.draft(context, manifest)
        contract = manifest["changed_contracts"][0]
        with self.assertRaisesRegex(ValueError, "unrelated"):
            workspace.search(context, manifest, "unrelated_admin", None, self.api, contract_id=contract["id"])
        invalid = copy.deepcopy(contract)
        invalid["id"] = "C-unproven"
        invalid["search_terms"].append({"query": "unrelated_admin", "reason": "Claimed alias without code evidence"})
        with self.assertRaisesRegex(ValueError, "alias/wrapper evidence"):
            workspace.register_contract(context, manifest, invalid, self.api)

    def test_single_negative_name_search_cannot_exclude_a_candidate(self):
        self.make_repo("consumer")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        candidate = next(r for r in draft["repository_coverage"] if r["status"] == "not-related")
        candidate["search_ids"] = candidate["search_ids"][:1]
        with self.assertRaisesRegex(ValueError, "one negative search"):
            workspace_report.validate(draft, manifest, context)

    def test_search_ledger_and_contract_anchor_tampering_are_rejected(self):
        self.make_repo("consumer")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        invalid = copy.deepcopy(manifest)
        invalid["searches"][0]["output"] = "fabricated match"
        with self.assertRaisesRegex(ValueError, "search ledger"):
            workspace_report.validate(draft, invalid, context)
        invalid = copy.deepcopy(manifest)
        invalid["changed_contracts"][0]["anchor_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "anchor bytes changed"):
            workspace_report.validate(draft, invalid, context)

    def test_registered_downstream_contract_requires_transitive_repo_searches(self):
        consumer = self.make_repo("consumer")
        downstream = self.make_repo("downstream")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        first = self.related(draft, manifest, consumer)
        first["id"] = "I-consumer-B"
        root = manifest["changed_contracts"][0]
        propagated = workspace.register_contract(context, manifest, {
            "id": "C-propagated", "root": root["root"], "parent_ids": [root["id"]],
            "kind": "event", "symbol": "contract", "evidence": first["evidence"][0],
            "search_terms": [{"query": "contract", "reason": "Downstream event key."},
                             {"query": "contract =", "reason": "Event wrapper declaration."}]
        }, self.api)
        first["contract_ids"].append(propagated["id"])
        second = self.related(draft, manifest, downstream)
        second["id"], second["related_to"] = "I-consumer-C", [first["id"]]
        second["contract_ids"] = [propagated["id"]]
        # Root -> B -> C -> root is an allowed cycle.
        draft["impact_coverage"][0]["related_to"] = [second["id"]]
        with self.assertRaisesRegex(ValueError, "every changed/propagated contract"):
            workspace_report.validate(draft, manifest, context)
        for repo in manifest["repositories"]:
            if repo["id"] == first["repo"]:
                continue
            row = next(r for r in draft["repository_coverage"] if r["repo"] == repo["id"])
            for term in propagated["search_terms"]:
                found = workspace.search(context, manifest, term["query"], [repo["id"]], self.api,
                                         contract_id=propagated["id"])
                row["search_ids"] += [s["id"] for s in found["searches"]]
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_group_quick_coverage_cannot_hide_staged_failure_with_working_fix(self):
        self.change()
        write_text(self.repo / "app.py", "contract = 'old'\n")
        args = self.api.parse_args(["--group-root", str(self.root), "--quick"])
        manifest = self.api.build_manifest(args)
        context = Path(args.context_dir)
        self.addCleanup(review_session.cleanup_session, context)
        self.assertEqual({"index"}, {c["source"] for c in manifest["required_coverage"]})
        repo = manifest["repositories"][0]
        self.assertEqual(b"contract = 'new'\n", workspace.fixed_bytes(context, repo, "index", "app.py", self.api))
        self.assertEqual(b"contract = 'old'\n", workspace.fixed_bytes(context, repo, "working-tree", "app.py", self.api))
        draft = self.draft(context, manifest)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        draft["coverage"] = []
        with self.assertRaisesRegex(ValueError, "every changed"):
            workspace_report.validate(draft, manifest, context)

    def test_group_quick_related_consumer_requires_both_matching_source_reviews(self):
        consumer = self.make_repo("consumer")
        self.change()
        write_text(consumer / "app.py", "contract = 'old'\nconsumer = 'staged failure'\n")
        git(consumer, "add", "app.py")
        write_text(consumer / "app.py", "contract = 'old'\nconsumer = 'saved correction'\n")
        args = self.api.parse_args(["--group-root", str(self.root), "--project", str(self.repo), "--quick"])
        manifest = self.api.build_manifest(args)
        context = Path(args.context_dir)
        self.addCleanup(review_session.cleanup_session, context)
        draft = self.draft(context, manifest)
        node = self.related(draft, manifest, consumer)
        node["contract_ids"] = [contract["id"] for contract in manifest["changed_contracts"]]
        node["related_to"] = [identifier for row in draft["coverage"] for identifier in row["impact_ids"]]
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])
        captured = workspace.repository(manifest, node["repo"])
        staged = copy.deepcopy(node)
        staged.update(id="I-consumer-index", source="index", reason="Reviewed the separate staged consumer behavior.")
        for evidence in staged["evidence"]:
            evidence.update(source="index", ref=captured["sources"]["index"]["ref"])
        draft["impact_coverage"].append(staged)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        staged["status"] = "not-reviewed"
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_group_implicit_branch_entry_uses_v2_unless_repo_scope_is_explicit(self):
        self.make_repo("consumer")
        git(self.repo, "switch", "-c", "feature")
        self.change()
        git(self.repo, "commit", "-m", "change")
        args = self.api.parse_args(["--workspace", str(self.root), "--project", str(self.repo),
                                    "--base", "main", "--head", "feature", "--no-fetch"])
        manifest = self.api.build_manifest(args)
        self.addCleanup(review_session.cleanup_session, Path(manifest["context_dir"]))
        self.assertEqual((workspace.CONTEXT, "group", 2),
                         (manifest["schema"], manifest["scope"], len(manifest["repositories"])))

    def test_one_repo_group_keeps_group_scope_and_detects_later_candidates(self):
        self.change()
        context, manifest = self.capture()
        self.assertEqual((workspace.CONTEXT, "group", 1),
                         (manifest["schema"], manifest["scope"], len(manifest["repositories"])))
        draft = self.draft(context, manifest)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        self.make_repo("new-consumer")
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])
        _, narrowed = self.capture(scope="repo")
        self.assertEqual(("repo", 1), (narrowed["scope"], len(narrowed["repositories"])))

    def test_one_repo_group_implicit_branch_entry_uses_group_v2(self):
        git(self.repo, "switch", "-c", "feature")
        self.change()
        git(self.repo, "commit", "-m", "change")
        args = self.api.parse_args(["--workspace", str(self.root), "--project", str(self.repo),
                                    "--base", "main", "--head", "feature", "--no-fetch"])
        manifest = self.api.build_manifest(args)
        self.addCleanup(review_session.cleanup_session, Path(manifest["context_dir"]))
        self.assertEqual((workspace.CONTEXT, "group", 1),
                         (manifest["schema"], manifest["scope"], len(manifest["repositories"])))

    def test_declared_group_quick_aliases_keep_both_local_sources(self):
        self.change()
        write_text(self.repo / "app.py", "contract = 'old'\n")
        for scope in (None, "auto", "group"):
            with self.subTest(scope=scope):
                args = ["--workspace", str(self.root), "--quick"]
                if scope:
                    args += ["--scope", scope]
                manifest = self.api.build_manifest(self.api.parse_args(args))
                context = Path(manifest["context_dir"])
                self.addCleanup(review_session.cleanup_session, context)
                self.assertEqual(("group", "quick"), (manifest["scope"], manifest["target"]))
                repo = manifest["repositories"][0]
                self.assertEqual(b"contract = 'new'\n", workspace.fixed_bytes(context, repo, "index", "app.py", self.api))
                self.assertEqual(b"contract = 'old'\n", workspace.fixed_bytes(context, repo, "working-tree", "app.py", self.api))
                self.assertEqual({"index"}, {row["source"] for row in manifest["required_coverage"]})

    def test_git_root_and_single_repo_workspace_file_keep_auto_repo_scope(self):
        self.change()
        workspace_file = self.root / "single.code-workspace"
        workspace_file.write_text(json.dumps({"folders": [{"path": "api"}]}), encoding="utf-8")
        for extra in ([], ["--workspace-file", str(workspace_file)]):
            args = self.api.parse_args(["--workspace", str(self.repo), "--scope", "auto", "--target", "staged", *extra])
            manifest = self.api.build_manifest(args)
            self.addCleanup(review_session.cleanup_session, Path(manifest["context_dir"]))
            self.assertEqual(("repo", 1), (manifest["scope"], len(manifest["repositories"])))

    def test_actual_vscode_roots_include_external_worktree_without_scanning_other_siblings(self):
        other = self.make_repo("nested/consumer")
        invisible = self.make_repo("not-visible")
        workspace_file = self.root / "visible.code-workspace"
        workspace_file.write_text(json.dumps({"folders": [{"path": "api"}, {"path": "nested/consumer"}]}), encoding="utf-8")
        self.change()
        _, manifest = self.capture("staged", "auto", True, "--workspace-file", str(workspace_file))
        self.assertEqual({str(self.repo), str(other)}, {r["path"] for r in manifest["repositories"]})
        self.assertNotIn(str(invisible), {r["path"] for r in manifest["repositories"]})

    def test_explicit_group_root_cannot_be_narrowed_by_workspace_file(self):
        self.make_repo("consumer")
        self.change()
        workspace_file = self.root / "partial.code-workspace"
        workspace_file.write_text(json.dumps({"folders": [{"path": "api"}]}), encoding="utf-8")
        args = self.api.parse_args(["--group-root", str(self.root), "--target", "staged", "--project", str(self.repo),
                                    "--workspace-file", str(workspace_file)])
        with self.assertRaisesRegex(ValueError, "cannot be narrowed"):
            self.api.build_manifest(args)
        self.assertFalse(Path(args.context_dir).exists())

    def test_new_candidate_or_changed_workspace_boundary_keeps_group_incomplete(self):
        self.change()
        context, manifest = self.capture("staged", "group")
        draft = self.draft(context, manifest)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        self.make_repo("new-consumer")
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])
        workspace_file = self.root / "visible.code-workspace"
        workspace_file.write_text(json.dumps({"folders": [{"path": "api"}]}), encoding="utf-8")
        context, manifest = self.capture("staged", "group", True, "--workspace-file", str(workspace_file))
        draft = self.draft(context, manifest)
        workspace_file.write_text(json.dumps({"folders": [{"path": "api"}, {"path": "new-consumer"}]}), encoding="utf-8")
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_rename_old_path_and_deletion_have_base_evidence_and_separate_coverage(self):
        write_text(self.repo / "deleted.py", "old = True\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "old files")
        git(self.repo, "mv", "app.py", "renamed.py")
        git(self.repo, "rm", "deleted.py")
        context, manifest = self.capture()
        self.assertEqual({"app.py", "renamed.py", "deleted.py"}, {r["path"] for r in manifest["required_coverage"]})
        result = workspace_report.validate(self.draft(context, manifest), manifest, context)
        self.assertTrue(result["review_complete"])

    def test_portable_group_keeps_independent_worktree_git_file(self):
        linked = self.root / "linked"
        git(self.repo, "worktree", "add", "-b", "linked-branch", str(linked))
        self.change()
        _, manifest = self.capture()
        captured = next(r for r in manifest["repositories"] if r["path"] == str(linked))
        self.assertEqual("captured", captured["state"])
        self.assertTrue((linked / ".git").is_file())

    def test_same_path_in_another_repo_cannot_replace_changed_source_evidence(self):
        self.make_repo("consumer")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        other = next(r for r in manifest["repositories"] if r["role"] == "candidate")
        draft["coverage"][0]["evidence"][0].update(repo=other["id"], ref=other["sources"]["index"]["ref"])
        with self.assertRaises(ValueError):
            workspace_report.validate(draft, manifest, context)

    def test_invalid_ref_and_line_ranges_are_rejected(self):
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        for mutation in ({"ref": "f" * 40}, {"line_end": 999}, {"line_start": True}):
            invalid = copy.deepcopy(draft)
            invalid["coverage"][0]["evidence"][0].update(mutation)
            with self.assertRaises(ValueError):
                workspace_report.validate(invalid, manifest, context)

    def test_finding_must_link_back_to_reviewed_seed_change(self):
        consumer = self.make_repo("consumer")
        self.change()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        node = self.related(draft, manifest, consumer)
        finding = {"id": "F-001", "priority": "P1", "title": "Consumer fails", "impact": "Request fails",
                   "trigger": "Read new response", "expected": "Value", "actual": "Missing value",
                   "recommendation": "Preserve compatibility", "owner": "開發", "scenario_source": "code-derived",
                   "evidence": node["evidence"][:]}
        draft["findings"] = [finding]
        with self.assertRaisesRegex(ValueError, "seed change"):
            workspace_report.validate(draft, manifest, context)
        finding["evidence"] += draft["coverage"][0]["evidence"]
        result = workspace_report.validate(draft, manifest, context)
        self.assertEqual(1, result["priority_counts"]["P1"])
        self.assertEqual("修正後再提交", result["recommendation"])

    def test_publication_includes_repo_details_and_cleans_context_on_success_and_failure(self):
        self.change()
        report_api = load_module("workspace_test_report", SCRIPTS / "review_report.py")
        for valid in (True, False):
            context, manifest = self.capture()
            draft = self.draft(context, manifest)
            if not valid:
                draft["coverage"] = []
            path = context / "draft.json"
            path.write_text(json.dumps(draft), encoding="utf-8")
            if valid:
                markdown, result = report_api.publish_report(context, path, self.root / "reports", include_json=True)
                self.assertTrue(markdown.exists() and result.exists())
                self.assertTrue((markdown.parent / "repo-000.md").exists())
            else:
                with self.assertRaises(ValueError):
                    report_api.publish_report(context, path, self.root / "reports")
            self.assertFalse(context.exists())

    def test_native_legacy_group_quick_keeps_both_sources(self):
        self.change()
        write_text(self.repo / "app.py", "working = True\n")
        args = self.api.parse_args(["--group-root", str(self.root), "--quick"])
        manifest = self.api.build_manifest(args)
        context = Path(args.context_dir)
        self.addCleanup(review_session.cleanup_session, context)
        self.assertEqual(workspace.CONTEXT, manifest["schema"])
        self.assertEqual({"base", "index", "working-tree"}, set(manifest["repositories"][0]["sources"]))

    def test_explicit_repo_search_without_contract_keeps_fixed_source_validation(self):
        self.change()
        context, manifest = self.capture(scope="repo")
        workspace.search(context, manifest, "contract", None, self.api)
        draft = self.draft(context, manifest)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        manifest["searches"][0]["output"] += "forged match\n"
        with self.assertRaisesRegex(ValueError, "search ledger"):
            workspace_report.validate(draft, manifest, context)

    def test_release_archive_runs_new_modes_without_workspace_overlay(self):
        self.change()
        package = load_module("workspace_package_test", ROOT / "scripts/package_release.py")
        version = package.read_version(ROOT / "skills/merge-reviewer/VERSION")
        archive = package.build_release(tag="v" + version, version_file=ROOT / "skills/merge-reviewer/VERSION",
                                        skill_dir=ROOT / "skills/merge-reviewer", output_dir=self.root / "dist")
        extracted = self.root / "installed"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extracted)
        result = run_python(extracted / "merge-reviewer/scripts/git_review_context.py", "--workspace", str(self.repo),
                            "--scope", "repo", "--target", "staged")
        self.assertEqual(0, result.returncode, result.stderr)
        manifest = json.loads(result.stdout)
        self.addCleanup(review_session.cleanup_session, Path(manifest["context_dir"]))
        self.assertEqual(workspace.CONTEXT, manifest["schema"])

    def test_path_escape_and_binary_evidence_limitations_are_explicit(self):
        for path in ("../file", "C:/outside", "a/../../file", "a\\file"):
            with self.assertRaises(ValueError):
                workspace.safe_path(self.root, path)
        write_text(self.repo / "app.py", "\0binary\n")
        git(self.repo, "add", "app.py")
        context, manifest = self.capture()
        draft = {"schema": workspace.RESULT, "summary": "二進位內容需另行檢查。",
                 "coverage": [{**manifest["required_coverage"][0], "status": "metadata-only", "reason": "Binary content"}],
                 "impact_coverage": [], "repository_coverage": [{"repo": "repo-000", "status": "seed", "reason": "Selected Repo"}],
                 "findings": [], "limitations": [], "next_steps": [{"owner": "QA", "action": "補做檢查。"}],
                 "tests_executed": [], "tests_not_executed": []}
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])


if __name__ == "__main__":
    unittest.main()
