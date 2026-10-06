"""Local Group review snapshots and reports, separate from the schema-4 API."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

CONTEXT = "merge-reviewer-group-context/v1"
RESULT = "merge-reviewer-group-result/v1"
SOURCES = ("index", "working-tree")


def safe(root: Path, relative: str) -> Path:
    if (not relative or Path(relative).is_absolute() or "\\" in relative
            or any(p in ("", ".", "..") for p in relative.split("/"))):
        raise ValueError("invalid context-relative path")
    value = root.joinpath(*relative.split("/"))
    if not value.resolve().is_relative_to(root.resolve()):
        raise ValueError("context path escapes root")
    return value


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def capture_repo(api, repo: Path, output: Path, key: str) -> dict:
    if repo.is_symlink() or Path(str(api.run_git(repo, ["rev-parse", "--show-toplevel"])).strip()).resolve() != repo.resolve():
        raise ValueError("Repo must be a real direct-child Git root")
    before = api.snapshot(repo)
    if before["dirty_submodules"]:
        raise ValueError("dirty/unavailable submodules prevent a stable Group snapshot")
    head = api.verified_commit(repo, "HEAD")
    changes_by_source, files = {}, []
    with tempfile.TemporaryDirectory(prefix="group-review-snapshot-") as temporary:
        env = api.snapshot_environment(repo, Path(temporary))
        empty = str(api.run_git(repo, ["hash-object", "-w", "-t", "tree", "--stdin"],
                                env=env, input_bytes=b"")).strip()
        base = str(api.run_git(repo, ["rev-parse", f"{head}^{{tree}}"])).strip() if head else empty
        if str(api.run_git(repo, ["ls-files", "--unmerged", "-z"])):
            raise ValueError("unmerged index cannot be snapshotted")
        index = api.git_path(repo, "index")
        if index.exists():
            # Preserve intent-to-add, sparse/skip flags and index extensions, not just ls-files blobs.
            shutil.copyfile(index, env["GIT_INDEX_FILE"])
            shared = str(api.run_git(repo, ["rev-parse", "--shared-index-path"])).strip()
            if shared:
                shared_path = Path(shared) if Path(shared).is_absolute() else repo / shared
                shutil.copyfile(shared_path, Path(env["GIT_INDEX_FILE"]).parent / shared_path.name)
        else:
            api.run_git(repo, ["read-tree", "--empty"], env=env)
        index_tree = str(api.run_git(repo, ["write-tree"], env=env)).strip()
        # The alternate index includes forced ignored staged files. Real index is never changed.
        api.run_git(repo, ["add", "--all", "--", "."], env=env)
        working_tree = str(api.run_git(repo, ["write-tree"], env=env)).strip()
        versions = {"index": index_tree, "working-tree": working_tree}
        for source, tree in versions.items():
            changes = api.collect_changes(repo, base, tree, env=env)
            changes_by_source[source] = changes
            patch = api.run_git(repo, ["diff", *api.DIFF_FLAGS, "--binary", "--find-renames", base, tree, "--"],
                                binary=True, env=env)
            (output / f"{source}.patch").write_bytes(patch)
            for change in changes:
                for path in dict.fromkeys([change.get("old_path"), change["path"]]):
                    if not path:
                        continue
                    for side, ref in (("base", base), ("result", tree)):
                        # A deletion has only base evidence; an addition has only result evidence.
                        exists = api.git_ok(repo, ["cat-file", "-e", f"{ref}:{path}"]) if ref == base and head else None
                        if exists is False:
                            continue
                        try:
                            data = api.run_git(repo, ["cat-file", "blob", f"{ref}:{path}"], binary=True, env=env)
                        except api.GitCommandError:
                            continue
                        name = f"{key}/{source}/{side}/{path}"
                        destination = safe(output.parent, name)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(data)
                        files.append({"source": source, "side": side, "path": path, "ref": ref,
                                      "file": name, "sha256": hashlib.sha256(data).hexdigest(),
                                      "blob_sha": str(api.run_git(repo, ["hash-object", "--stdin"],
                                                                  input_bytes=data, env=env)).strip()})
        after = api.snapshot(repo)
        unchanged = before == after
        return {"repo": repo.name, "repo_path": str(repo.resolve()), "head_sha": head,
                "base_tree": base, "versions": versions, "changed_files": changes_by_source,
                "evidence_files": files, "before": before, "unchanged": unchanged,
                "state": "captured" if any(changes_by_source.values()) else "clean",
                "limitations": [] if unchanged else ["Repo changed during capture; rerun review."]}


def build(args, api) -> dict:
    if not args.quick or args.project or args.base or args.head or args.mr_context or getattr(args, "mr_context_base64", None):
        raise ValueError("--group-root requires --quick and cannot combine with project/base/head/MR context")
    root = Path(args.group_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Group root does not exist")
    candidates = sorted([p for p in root.iterdir() if p.is_dir() and (p / ".git").exists()], key=lambda p: p.name)
    context = Path(args.context_dir).expanduser().resolve() if args.context_dir else Path(tempfile.mkdtemp(prefix="group-review-")) / "bundle"
    if any(context.is_relative_to(p.resolve()) for p in candidates):
        raise ValueError("Group context must be outside all target Repos")
    context.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    result = {"schema": CONTEXT, "group_root": str(root), "context_dir": str(context),
              "context_cleanup_required": True,
              "context_cleanup_note": "本次 context 位於系統暫存目錄；審查結束或中止時請執行 review_session.py cleanup --context-dir。",
              "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
              "repositories": [], "limitations": [], "network_used": False}
    if not candidates:
        result["limitations"].append("No direct-child Git repositories were found.")
    for index, repo in enumerate(candidates):
        key = f"repo-{index:03}"
        output = context / key
        output.mkdir()
        try:
            value = capture_repo(api, repo, output, key)
        except (OSError, ValueError, api.ReviewContextError) as exc:
            value = {"repo": repo.name, "repo_path": str(repo), "state": "error",
                     "limitations": [str(exc)], "changed_files": {}, "evidence_files": []}
        value["key"] = key
        result["repositories"].append(value)
    save(context / "manifest.json", result)
    return result


def validate(draft: dict, manifest: dict, context: Path) -> dict:
    if draft.get("schema") != RESULT or manifest.get("schema") != CONTEXT:
        raise ValueError("Group context/result schema mismatch")
    if not isinstance(draft.get("summary"), str) or not draft["summary"].strip():
        raise ValueError("Group summary is required")
    if any(not isinstance(draft.get(k), list) for k in ("coverage", "findings", "limitations")):
        raise ValueError("Group coverage/findings/limitations arrays are required")
    repos = {r["repo"]: r for r in manifest["repositories"]}
    expected = {(r["repo"], source, path)
                for r in repos.values() for source, changes in r["changed_files"].items()
                for c in changes for path in (c.get("old_path"), c["path"]) if path}
    coverage = {(c.get("repo"), c.get("source"), c.get("path")): c for c in draft["coverage"]}
    if set(coverage) != expected or len(coverage) != len(draft["coverage"]):
        raise ValueError("coverage must include each Repo/source/path exactly once")

    def evidence(item):
        key = item.get("repo"), item.get("source"), item.get("path")
        if key not in coverage:
            raise ValueError("evidence is outside covered changed paths")
        records = [x for x in repos[key[0]]["evidence_files"] if
                   (x["source"], x["path"], x["ref"], x["side"]) ==
                   (key[1], key[2], item.get("ref"), item.get("side", "result"))]
        if len(records) != 1:
            raise ValueError("evidence must identify one frozen file version")
        record = records[0]
        data = safe(context, record["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("frozen evidence digest changed")
        start, end = item.get("line_start"), item.get("line_end")
        if b"\0" in data or type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(data.splitlines()):
            raise ValueError("invalid text evidence line range")
        return key

    for key, item in coverage.items():
        if item.get("status") not in ("reviewed", "metadata-only", "not-reviewed"):
            raise ValueError("invalid coverage state")
        if item["status"] == "reviewed":
            if not item.get("evidence") or any(evidence(e) != key for e in item["evidence"]):
                raise ValueError("reviewed coverage requires its frozen source evidence")
        elif not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("unreviewed paths require a reason")
    unique = {}
    fields = ("title", "impact", "trigger", "expected", "actual", "recommendation")
    for item in draft["findings"]:
        if (item.get("priority") not in ("P0", "P1", "P2", "P3")
                or any(not isinstance(item.get(k), str) or not item[k].strip() for k in fields)
                or not item.get("evidence")):
            raise ValueError("finding needs priority, behavior, impact, fix and evidence")
        keys = [evidence(e) for e in item["evidence"]]
        if any(coverage[k]["status"] != "reviewed" for k in keys):
            raise ValueError("findings require reviewed coverage")
        fingerprint = tuple(item[k] for k in ("priority", *fields)) + (tuple(sorted({(k[0], k[2]) for k in keys})),)
        if fingerprint in unique:
            prior = unique[fingerprint]["evidence"]
            prior.extend(e for e in item["evidence"] if e not in prior)
        else:
            unique[fingerprint] = {**item, "evidence": list(item["evidence"])}
    findings = sorted(unique.values(), key=lambda x: x["priority"])
    for index, item in enumerate(findings, 1):
        item["id"] = f"F-{index:03}"
    incomplete = (bool(manifest["limitations"]) or bool(draft["limitations"])
                  or any(r["state"] == "error" or r.get("unchanged") is False for r in repos.values())
                  or any(c["status"] != "reviewed" for c in coverage.values()))
    return {**draft, "findings": findings, "review_complete": not incomplete,
            "submission_recommendation": "審查未完成" if incomplete else "先修正後提交" if findings else "可進行提交",
            "priority_counts": {p: sum(f["priority"] == p for f in findings) for p in ("P0", "P1", "P2", "P3")},
            "context_sha256": hashlib.sha256((context / "manifest.json").read_bytes()).hexdigest()}


def render(result: dict, manifest: dict, repo_name: str | None = None) -> str:
    repos = [r for r in manifest["repositories"] if repo_name is None or r["repo"] == repo_name]
    findings = [f for f in result["findings"] if repo_name is None or any(e["repo"] == repo_name for e in f["evidence"])]
    lines = ["# Group 未提交內容審查", "", result["summary"], "",
             f"提交建議：{result['submission_recommendation']}", "",
             "問題數量：" + " · ".join(f"{p}: {sum(f['priority'] == p for f in findings)}" for p in ("P0", "P1", "P2", "P3")), "",
             "| Repo | 擷取狀態 | 限制 |", "| --- | --- | --- |"]
    for repo in repos:
        lines.append(f"| {repo['repo']} | {repo['state']} | {'; '.join(repo['limitations'])} |")
    lines += ["", "## 問題與版本證據", ""]
    for finding in findings:
        lines += [f"### {finding['id']} · {finding['priority']} · {finding['title']}", "",
                  finding["impact"], f"情境：{finding['trigger']}", f"預期：{finding['expected']}",
                  f"實際：{finding['actual']}", f"建議：{finding['recommendation']}", ""]
        for item in finding["evidence"]:
            lines.append(f"- `{item['repo']}/{item['path']}` · {item['source']} · {item.get('side', 'result')} · `{item['ref']}` · L{item['line_start']}–{item['line_end']}")
        lines.append("")
    lines += ["## 覆蓋範圍", ""]
    for item in result["coverage"]:
        if repo_name is None or item["repo"] == repo_name:
            lines.append(f"- `{item['repo']}/{item['path']}` · {item['source']} · {item['status']} {item.get('reason', '')}")
    lines += ["", "## 限制與基準", "", "靜態審查；未執行產品測試。兩個來源各自比較本機 HEAD，沒有查詢遠端。"]
    lines += [f"- {value}" for value in manifest["limitations"] + result["limitations"]]
    return "\n".join(lines) + "\n"


def publish(context: Path, draft: dict, manifest: dict, report_dir: Path | None, include_json: bool):
    result = validate(draft, manifest, context)
    import git_review_context as api
    for repo in manifest["repositories"]:
        if repo["state"] not in ("clean", "captured"):
            continue
        try:
            unchanged = api.snapshot(Path(repo["repo_path"])) == repo["before"]
        except (OSError, ValueError, api.ReviewContextError):
            unchanged = False
        if not unchanged:
            result["review_complete"] = False
            result["submission_recommendation"] = "審查未完成"
            result["limitations"].append(f"{repo['repo']}: state changed since capture; rerun before submission.")
    output = report_dir or Path(manifest["group_root"]) / "review-reports" / manifest["run_id"]
    if any(output.resolve().is_relative_to(Path(r["repo_path"]).resolve()) for r in manifest["repositories"]):
        raise ValueError("Group reports must be outside target Repos")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".group-review-", dir=output.parent))
    try:
        (staging / "summary.md").write_text(render(result, manifest), encoding="utf-8")
        for repo in manifest["repositories"]:
            (staging / f"{repo['key']}.md").write_text(render(result, manifest, repo["repo"]), encoding="utf-8")
        if include_json:
            save(staging / "result.json", result)
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output / "summary.md", output / "result.json" if include_json else None
