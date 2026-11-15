"""Paired helper-workflow replay of sanitized log interaction patterns.

Freeze HEAD (or an explicit local commit) and the candidate before timing. Both
versions read the same committed fixture and the same artifact pages. Only a
candidate advertising the new capabilities uses batched views / summary status.
The arithmetic finding is a fixed, independently checked test oracle. This is
not a model-quality evaluation or a measurement of model review time.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TITLE = "乘法改為加法使總額計算錯誤"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def git(path, *arguments, binary=False):
    result = subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, check=True)
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def digest(directory):
    value = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            value.update(path.relative_to(directory).as_posix().encode("utf-8") + b"\0")
            value.update(hashlib.sha256(path.read_bytes()).digest())
    return value.hexdigest()


def freeze(output, revision, candidate, baseline_skill=None):
    commit = git(ROOT, "rev-parse", "--verify", revision + "^{commit}")
    baseline = output / "skills" / "baseline"
    if baseline_skill is not None:
        shutil.copytree(baseline_skill, baseline, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        raw = git(ROOT, "archive", "--format=tar", commit, "skills/merge-reviewer", binary=True)
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                relative = Path(member.name).relative_to("skills/merge-reviewer")
                target = (baseline / relative).resolve()
                if not target.is_relative_to(baseline.resolve()):
                    raise ValueError("baseline archive escaped its destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    target = output / "skills" / "candidate"
    shutil.copytree(candidate, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    identity = {"baseline_revision": commit, "baseline_skill_sha256": digest(baseline),
                "baseline_source": "frozen-directory" if baseline_skill is not None else "git-archive",
                "candidate_skill_sha256": digest(target)}
    save(output / "identity.json", identity)
    return baseline, target, identity


def fixture(root):
    workspace = root / "workspace"
    workspace.mkdir()
    repo = workspace / "orders"
    git(workspace, "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Replay Fixture")
    git(repo, "config", "user.email", "replay@example.invalid")
    source = repo / "pricing.py"
    source.write_text("def total(price, quantity):\n    return price * quantity\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline multiplication")
    git(repo, "switch", "-c", "feature")
    source.write_text("def total(price, quantity):\n    return price + quantity\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "seed arithmetic regression")
    # Execute only this source-only fixture created above, never user log code.
    old, new = {}, {}
    exec(git(repo, "show", "main:pricing.py"), old)
    exec(source.read_text(encoding="utf-8"), new)
    if old["total"](10, 3) != 30 or new["total"](10, 3) != 13:
        raise AssertionError("fixture arithmetic oracle differs")
    return repo


def oracle(signature, report):
    finding = signature["findings"]
    if (len(finding) != 1 or finding[0].get("priority") != "P2"
            or finding[0].get("title") != TITLE or TITLE not in report
            or "30" not in report or "13" not in report
            or signature["outcome"].get("review_complete") is not True
            or signature["outcome"].get("gaps") != []):
        raise AssertionError("finding, arithmetic result or gap oracle differs")


def canonical_pages(pages):
    """Compare necessary evidence once, independently of a redundant call order."""
    unique = {json.dumps(page, ensure_ascii=False, sort_keys=True, separators=(",", ":")): page for page in pages}
    return [unique[key] for key in sorted(unique)]


def paging_module(skill):
    """Load only the frozen candidate's pure state transformer, not live helpers."""
    scripts = skill / "scripts"
    spec = importlib.util.spec_from_file_location("replay_candidate_paging", scripts / "review_paging.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(scripts))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def worker(skill, repo, destination, interaction_mode="legacy", *, duplicate_count=100):
    destination.mkdir()
    runtime = repo.parent.parent / "runtimes" / destination.name
    trace = destination / "trace.jsonl"
    environment = {**os.environ, "GITLAB_WORKSPACE_RUNTIME_DIR": str(repo.parent.parent / "network" / destination.name)}
    context, identifier = None, None
    round_trips = 0
    atomic_author_calls, checkpoint_recoveries, duplicates_avoided = 0, 0, 0
    started = time.perf_counter()

    def cli(*arguments, accepted=(0,)):
        nonlocal round_trips
        round_trips += 1
        command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "tests/agent_trace_cli.py"),
                   "--skill-dir", str(skill), "--trace", str(trace), "--audit-dir", str(destination / "audit"),
                   "review_session", *map(str, arguments)]
        result = subprocess.run(command, capture_output=True, encoding="utf-8", env=environment)
        if result.returncode not in accepted:
            raise RuntimeError(f"{arguments[0]} exited {result.returncode}: {result.stderr}\n{result.stdout}")
        value = json.loads(result.stdout)
        if value.get("schema") == "MergeReviewEvidenceSummary/v1":
            raise ValueError("replay fixture needs extra unmeasured paging; reduce the fixture")
        return value

    try:
        capabilities = cli("capabilities")
        features = capabilities.get("features", {})
        paging_enabled = interaction_mode == "paging" and features.get("paging_state") is True
        start = cli("start", "--workspace", repo.parent, "--project", repo,
                    "--base", "main", "--head", "feature", "--runtime-root", runtime,
                    *(["--draft-format", "compact"] if paging_enabled else []))
        identifier, context = start["request_id"], Path(start["context_dir"])
        common = ["--runtime-root", runtime, "--request-id", identifier]
        step = cli("step", *common, "--requests-file", start["initial_reads_file"])
        if step.get("projection_complete") is False:
            raise ValueError("initial reads require unmeasured continuation")
        reads = [row for row in step["operations"] if row.get("result", {}).get("path") == "pricing.py"
                 and ("lines" in row["result"] or row["result"].get("content_id") in step.get("contents", {}))]
        if not reads:
            raise ValueError("replay initial reads did not return the arithmetic source")
        requests = [{"id": f"{row['id']}-{name}", "artifact": row["artifact"], "pointer": "/" + name}
                    for row in reads for name in ("repo", "source", "ref", "path", "sha256", "lines")]
        if interaction_mode == "paging":
            # The actual logs repeat previously returned pages without moving a
            # cursor. Reproduce that pattern, not the original business source.
            originals = list(requests)
            requests += [{**originals[index % len(originals)], "id": f"repeat-{index + 1}"}
                         for index in range(duplicate_count)]
        pages = []
        checkpoint_revision = 0
        if paging_enabled:
            transformer = paging_module(skill)
            native = [{**request, "command": "review_session.py view", "runtime_root": str(runtime),
                       "request_id": identifier, "artifact_scope": "context", "read_only": True}
                      for request in requests]
            paging = transformer.new_state(runtime, identifier, native)
            requests_file = context / "paging.json"
            while paging["pending"]:
                save(requests_file, paging)
                batch = cli("view", *common, "--read-only", "--requests-file", requests_file, accepted=(0, 3))
                for row in batch["results"]:
                    if "error" in row:
                        raise ValueError("paging replay unexpectedly blocked a valid request")
                    if isinstance(row.get("result"), dict):
                        pages.append(row["result"])
                updated = transformer.apply_delta(paging, batch["paging_delta"])
                if updated["blocked"]:
                    raise ValueError("paging replay contains blocked requests")
                if updated == paging:
                    raise ValueError("paging replay made no progress")
                paging = updated
            duplicates_avoided = len(requests) - len(pages)
            save_file = context / "save-paging.json"
            save(save_file, [{"id": "save-replay-paging", "op": "author",
                             "updates": {"summary": "固定來源的算術回歸已確認。"},
                             "expected_draft_sha256": hashlib.sha256((context / "draft.json").read_bytes()).hexdigest(),
                             "expected_checkpoint_revision": checkpoint_revision,
                             "checkpoint": {"paging": paging, "next_action": "Author the verified arithmetic finding."}}])
            saved = cli("step", *common, "--requests-file", save_file)
            atomic_author_calls += 1
            if not saved["complete"]:
                raise ValueError("paging checkpoint was not saved atomically")
            summary = cli("status", *common, "--summary")
            checkpoint_revision = summary["checkpoint_revision"]
            reference = summary["paging_ref"]
            restored = cli("view", *common, "--read-only", "--artifact", reference["artifact"],
                           "--artifact-scope", reference["artifact_scope"], "--pointer", reference.get("pointer", ""))
            if restored.get("view_complete") is not True or restored.get("value") != paging:
                raise ValueError("complete paging state was not recovered from its saved artifact")
            paging = restored["value"]
            checkpoint_recoveries += 1
        elif features.get("batch_artifact_view") and interaction_mode != "paging":
            requests_file = context / "pages.json"
            save(requests_file, requests)
            offset = 0
            while offset is not None:
                batch = cli("view", *common, "--requests-file", requests_file, "--offset", offset, accepted=(0, 3))
                if batch.get("schema") != "MergeReviewArtifactBatch/v1":
                    raise ValueError("legacy replay received an invalid batch: " + json.dumps(batch))
                for row in batch["results"]:
                    if "error" in row or not row["result"]["view_complete"]:
                        raise ValueError("replay metadata page was not returned completely")
                    pages.append(row["result"])
                offset = batch["next_index"]
        else:
            for request in requests:
                page = cli("view", *common, "--artifact", request["artifact"], "--pointer", request["pointer"])
                if not page["view_complete"]:
                    raise ValueError("baseline metadata page was not returned completely")
                pages.append(page)
        if not paging_enabled:
            cli("status", *common, *(["--summary"] if features.get("summary_status") else []))
        manifest = load(context / "manifest.json")
        seed = next(row for row in manifest["repositories"] if row["id"] == manifest["selected_repo"])
        evidence = {"repo": seed["id"], "source": "head", "ref": seed["sources"]["head"]["ref"],
                    "path": "pricing.py", "line_start": 2, "line_end": 2}
        finding = {"id": "F-001", "priority": "P2", "title": TITLE, "owner": "開發", "scenario_source": "code-derived",
                   "impact": "總額計算錯誤。", "impact_scope": "總額計算錯誤。",
                   "trigger": "以單價 10 與數量 3 計算。", "technical_trigger": "以單價 10 與數量 3 計算。",
                   "operation_scenario": "使用單價與數量計算總額。", "technical_explanation": "固定 head 使用加法取代乘法。",
                   "expected": "總額應為 30。", "actual": "總額為 13。", "recommendation": "恢復乘法。",
                   "engineering_fix": "恢復乘法。", "evidence": [evidence]}
        draft = {"schema": "merge-reviewer-workspace-result/v3", "report_layout": "engineering-review/v1",
                 "summary": "固定來源的算術回歸已確認。", "findings": [finding],
                 "coverage": [{**row, "status": "reviewed", "evidence": [evidence]} for row in manifest["required_coverage"]],
                 "repository_coverage": [{"repo": seed["id"], "status": "seed", "reason": "Selected fixture.", "search_ids": []}],
                 "impact_coverage": [], "boundary_reviews": [], "merge_parent_review": [], "pending_confirmations": [],
                 "next_steps": [], "tests_executed": [], "tests_not_executed": ["Model review is not part of this replay."],
                 "limitations": []}
        if paging_enabled:
            changes = manifest.get("boundary_changes", [])
            updates = {key: value for key, value in draft.items() if key not in ("schema", "report_layout", "coverage")}
            updates["coverage"] = [{"change_id": row["id"], "status": "reviewed",
                                    "reason": "The fixed arithmetic change and its counterexample were inspected.",
                                    "evidence": [evidence]} for row in changes]
            if not changes and manifest["required_coverage"]:
                raise ValueError("compact replay requires fixed change IDs")
            save(save_file, [{"id": "save-replay-finding", "op": "author", "updates": updates,
                             "expected_draft_sha256": hashlib.sha256((context / "draft.json").read_bytes()).hexdigest(),
                             "expected_checkpoint_revision": checkpoint_revision,
                             "checkpoint": {"next_action": "Run check and finish."}}])
            saved = cli("step", *common, "--requests-file", save_file)
            atomic_author_calls += 1
            if not saved["complete"]:
                raise ValueError("arithmetic finding was not saved through atomic author")
        else:
            save(context / "draft.json", draft)
        actual_findings = load(context / "draft.json")["findings"]
        if actual_findings != draft["findings"]:
            raise AssertionError("the saved arithmetic finding differs from the replay oracle")
        cli("check", *common)
        published = cli("finish", *common)
        elapsed = time.perf_counter() - started
        report = Path(published["report_path"]).read_text(encoding="utf-8")
        signature = {"sources": {name: {key: source[key] for key in ("ref", "tree_sha", "files")}
                                 for name, source in seed["sources"].items()},
                     "pages": canonical_pages(pages), "findings": actual_findings, "outcome": published["review_outcome"],
                     "finding_body": report.split('<a id="finding-F-001"></a>', 1)[1].split("## 範圍與限制", 1)[0]}
        oracle(signature, report)
        save(destination / "signature.json", signature)
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        metrics = {"helper_workflow_seconds": elapsed, "tool_round_trips": round_trips,
                   "finish_attempts": sum(row["operation"] == "finish" for row in events),
                   "full_validations": sum(row["full_result_validations"] for row in events),
                   "stdout_bytes": sum(row["output_bytes"] for row in events),
                   "stderr_bytes": sum(row["stderr_bytes"] for row in events),
                   "model_review_seconds": None, "oracle_passed": True,
                   "interaction_mode": interaction_mode, "paging_protocol_used": paging_enabled,
                   "redundant_page_requests": duplicate_count if interaction_mode == "paging" else 0,
                   "atomic_author_calls": atomic_author_calls, "checkpoint_recoveries": checkpoint_recoveries,
                   "duplicates_avoided": duplicates_avoided,
                   "git_commands": published["metrics"]["git_command_count"]}
        save(destination / "metrics.json", metrics)
        return metrics, signature
    finally:
        if context and context.is_dir():
            cli("cancel", "--runtime-root", runtime, "--request-id", identifier)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-revision", default="HEAD")
    parser.add_argument("--baseline-skill", type=Path, help="Use an already frozen local skill payload instead of git archive.")
    parser.add_argument("--candidate-skill", type=Path, default=ROOT / "skills/merge-reviewer")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--interaction-mode", choices=("legacy", "paging"), default="legacy")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("repeats must be positive")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline, candidate, identity = freeze(output, args.baseline_revision, args.candidate_skill.resolve(),
                                          args.baseline_skill.resolve() if args.baseline_skill else None)
    rows, expected = [], None
    with tempfile.TemporaryDirectory(prefix="review-log-workflow-") as temporary:
        repo = fixture(Path(temporary))
        for repetition in range(args.repeats):
            pair = {}
            order = ("baseline", "candidate") if repetition % 2 == 0 else ("candidate", "baseline")
            for label in order:
                metrics, signature = worker(baseline if label == "baseline" else candidate, repo,
                                             output / f"{repetition + 1:02}-{label}", args.interaction_mode)
                if label == "candidate" and args.interaction_mode == "paging" and not metrics["paging_protocol_used"]:
                    raise AssertionError("candidate did not execute the requested paging protocol")
                if expected is None:
                    expected = signature
                if signature != expected:
                    raise AssertionError("frozen source, returned evidence, finding body or outcome differs")
                pair[label] = metrics
            rows.append(pair)
            print(json.dumps({"pair": repetition + 1, **pair}), flush=True)
    summary = {"schema": "MergeReviewLogWorkflowReplay/v1", **identity, "repeats": args.repeats, "pairs": rows,
               "evidence_findings_gaps_equal": True,
               "paired_time_ratio_median": statistics.median(
                   row["candidate"]["helper_workflow_seconds"] / row["baseline"]["helper_workflow_seconds"] for row in rows),
               "scope": "Sanitized deterministic helper interactions; no model analysis or model-quality acceptance.",
               "interaction_mode": args.interaction_mode,
               "model_review_seconds": None}
    save(output / "summary.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
