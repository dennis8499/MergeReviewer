#!/usr/bin/env python3
"""Validate a structured review result and render consistent JSON/Markdown."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Sequence

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

PRIORITIES = ("P0", "P1", "P2", "P3")
ACTION_LABELS = {
    "P0": "必須立即處理",
    "P1": "合併前必修",
    "P2": "近期排程修正",
    "P3": "可後續改善",
}
OWNERS = {"開發", "維運設定", "QA"}
STATUSES = {"reviewed", "metadata-only", "not-reviewed"}
EVIDENCE_SOURCES = {"head", "base", "working-tree", "merge-preview", "merge-parent"}
OBJECT_ID_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
STATUS_EXPLANATIONS = {
    "發現具體問題": "審查範圍完整，且找到有證據支持的問題。",
    "沒有差異": "兩個固定版本沒有需要審查的變更。",
    "未發現具體問題": "已檢查完整範圍，沒有找到有證據支持的問題。",
    "審查未完成": "部分程式或證據未能確認，結果不能視為完整通過。",
}


class ReportValidationError(ValueError):
    """A draft report that cannot be safely published."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReportValidationError(message)


def _valid_object_id(value: Any) -> bool:
    return isinstance(value, str) and OBJECT_ID_PATTERN.fullmatch(value) is not None


def _safe_path(root: Path, value: str) -> Path:
    relative = Path(value)
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    _require(
        bool(value)
        and not relative.is_absolute()
        and not posix_path.is_absolute()
        and not windows_path.is_absolute()
        and not windows_path.drive,
        f"證據路徑必須是 repository 相對路徑：{value!r}",
    )
    _require(
        ".." not in relative.parts
        and ".." not in posix_path.parts
        and ".." not in windows_path.parts,
        f"證據路徑不可離開 context：{value!r}",
    )
    candidate = root / relative
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ReportValidationError(f"證據路徑超出 repository 或 context：{value!r}") from exc
    return candidate


def _git_blob(repo: Path, reference: str, path: str, timeout: float) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{reference}:{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReportValidationError(f"無法查證證據物件 {reference}:{path}：{detail}")
    return result.stdout


def _git_blob_id(repo: Path, content: bytes, timeout: float) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "--stdin"],
        input=content,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReportValidationError(f"無法計算固定檔案的 Git blob SHA：{detail}")
    return result.stdout.decode("ascii", errors="strict").strip()


def _evidence_bytes(
    evidence: dict[str, Any],
    manifest: dict[str, Any],
    context_dir: Path,
    timeout: float,
) -> bytes:
    source = evidence["source"]
    reference = evidence["ref"]
    path = evidence["path"]
    repo = Path(manifest["repo"])
    if source == "head":
        _require(reference == manifest["head_sha"], "head 證據必須指向記錄的 head_sha。")
        return _git_blob(repo, reference, path, timeout)
    if source == "base":
        allowed = {manifest["base_sha"], manifest.get("merge_base")}
        _require(reference in allowed, "base 證據必須指向 manifest 記錄的基礎版本或共同起點。")
        return _git_blob(repo, reference, path, timeout)
    if source == "merge-parent":
        parents = {
            parent
            for merge in manifest.get("merge_commits", [])
            for parent in merge.get("parents", [])
        }
        _require(reference in parents, "merge-parent 證據必須指向 manifest 記錄的 merge parent。")
        return _git_blob(repo, reference, path, timeout)
    if source == "working-tree":
        _require(reference == manifest.get("review_tree_sha"), "工作區證據必須指向固定的 review_tree_sha。")
        candidate = _safe_path(context_dir / "working-tree-files", path)
        try:
            content = candidate.read_bytes()
        except OSError as exc:
            raise ReportValidationError(f"工作區證據不在固定 bundle 中：{path}") from exc
        _check_snapshot_file(content, evidence, manifest, source, repo, timeout)
        return content
    _require(
        reference == (manifest.get("merge_preview") or {}).get("tree_sha"),
        "合併預覽證據必須指向 manifest 記錄的合併樹。",
    )
    merge_preview = manifest.get("merge_preview") or {}
    info = merge_preview.get("manifest_info") or {}
    object_directory = info.get("object_directory")
    alternate_objects = info.get("alternate_objects")
    _require(bool(object_directory and alternate_objects), "合併預覽 objects 未保留，無法驗證證據。")
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{reference}:{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env={
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OBJECT_DIRECTORY": str(object_directory),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(alternate_objects),
        },
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReportValidationError(f"無法查證合併預覽證據 {reference}:{path}：{detail}")
    _check_snapshot_file(result.stdout, evidence, manifest, source, repo, timeout)
    return result.stdout


def _check_snapshot_file(
    content: bytes,
    evidence: dict[str, Any],
    manifest: dict[str, Any],
    source: str,
    repo: Path,
    timeout: float,
) -> None:
    matching = [
        item
        for item in manifest.get("evidence_files", [])
        if item.get("source") == source and item.get("path") == evidence["path"]
    ]
    _require(len(matching) == 1, f"{source} 證據缺少唯一的固定檔案摘要：{evidence['path']}")
    record = matching[0]
    _require(record.get("blob_sha"), f"固定檔案缺少 Git blob SHA：{evidence['path']}")
    _require(
        _git_blob_id(repo, content, timeout) == record["blob_sha"],
        f"固定檔案 Git blob SHA 不相符：{evidence['path']}",
    )
    _require(
        hashlib.sha256(content).hexdigest() == record.get("sha256"),
        f"固定檔案摘要不相符：{evidence['path']}",
    )


def validate_result(
    draft: dict[str, Any], manifest: dict[str, Any], context_dir: Path, *, timeout: float = 180.0
) -> dict[str, Any]:
    _require(isinstance(draft, dict), "審查結果最上層必須是 JSON 物件。")
    _require(isinstance(manifest, dict), "context manifest 最上層必須是 JSON 物件。")
    _require(type(draft.get("schema_version")) is int and draft["schema_version"] == 1, "review-result schema_version 必須為 1。")
    _require(isinstance(draft.get("summary"), str) and bool(draft["summary"].strip()), "summary 不可空白。")
    for field in ("coverage", "findings", "next_steps", "tests_executed", "tests_not_executed", "limitations"):
        _require(field in draft and isinstance(draft[field], list), f"{field} 必須是必要的 JSON 陣列。")
    _require(bool(draft["next_steps"]), "至少要提供一項有負責角色的 next_step。")
    _require(type(manifest.get("schema_version")) is int and manifest["schema_version"] == 4, "僅接受 schema_version 4 的固定審查 context。")
    for field in ("tests_executed", "tests_not_executed", "limitations"):
        _require(
            all(isinstance(item, (str, dict)) for item in draft.get(field, [])),
            f"{field} 含有無法呈現的項目。",
        )
    for field in (
        "repo",
        "project",
        "base_input",
        "base_sha",
        "head_input",
        "head_sha",
        "mode",
        "review_scope",
        "generated_at",
        "fetch_status",
        "review_right",
    ):
        _require(isinstance(manifest.get(field), str) and bool(manifest[field]), f"context manifest 缺少 {field}。")
    _require(_valid_object_id(manifest["base_sha"]), "base_sha 必須是完整 Git object ID。")
    _require(_valid_object_id(manifest["head_sha"]), "head_sha 必須是完整 Git object ID。")
    _require(len(manifest["base_sha"]) == len(manifest["head_sha"]), "base_sha 與 head_sha 的 object 格式不一致。")
    merge_base = manifest.get("merge_base")
    _require(merge_base is None or _valid_object_id(merge_base), "merge_base 必須是完整 Git object ID 或 null。")
    _require(manifest["mode"] in {"merge", "direct"}, "context manifest.mode 無效。")
    _require(manifest["review_scope"] in {"committed", "working-tree"}, "context manifest.review_scope 無效。")
    _require(type(manifest.get("context_complete")) is bool, "context_complete 必須是 boolean。")
    _require(type(manifest.get("review_complete")) is bool, "review_complete 必須是 boolean 相容欄位。")
    _require(manifest["review_complete"] == manifest["context_complete"], "review_complete 必須與 context_complete 相同。")
    for field in ("changed_files", "merge_commits", "context_gaps", "review_limitations", "evidence_files"):
        _require(isinstance(manifest.get(field), list), f"context manifest.{field} 必須是陣列。")
    for field in ("context_gaps", "review_limitations"):
        _require(all(isinstance(item, str) for item in manifest[field]), f"context manifest.{field} 必須只含文字。")
    _require(type(manifest.get("working_tree_unchanged")) is bool, "working_tree_unchanged 必須是 boolean。")
    review_tree_sha = manifest.get("review_tree_sha")
    if manifest["review_scope"] == "working-tree":
        _require(_valid_object_id(review_tree_sha), "working-tree scope 必須有完整 review_tree_sha。")
        _require(manifest["review_right"] == review_tree_sha, "working-tree review_right 必須指向 review_tree_sha。")
    else:
        _require(review_tree_sha is None, "committed scope 的 review_tree_sha 必須為 null。")
        _require(manifest["review_right"] == manifest["head_sha"], "committed review_right 必須指向 head_sha。")
    _require(manifest["context_complete"] == (not manifest["context_gaps"]), "context_complete 必須和 context_gaps 一致。")
    _require(
        all(isinstance(item, dict) for item in manifest["changed_files"]),
        "context manifest.changed_files 必須只含物件。",
    )
    _require(
        all(isinstance(item, dict) for item in manifest["merge_commits"]),
        "context manifest.merge_commits 必須只含物件。",
    )
    _require(
        all(isinstance(item, dict) for item in manifest["evidence_files"]),
        "context manifest.evidence_files 必須只含物件。",
    )
    for index, item in enumerate(manifest["changed_files"], start=1):
        _require(
            isinstance(item.get("path"), str) and bool(item["path"]),
            f"context manifest.changed_files[{index}].path 不可空白。",
        )
        _require(
            isinstance(item.get("status"), str) and bool(item["status"]),
            f"context manifest.changed_files[{index}].status 無效。",
        )
        old_path = item.get("old_path")
        _require(
            old_path is None or (isinstance(old_path, str) and bool(old_path)),
            f"context manifest.changed_files[{index}].old_path 無效。",
        )
    _require(type(manifest.get("commit_count")) is int and manifest["commit_count"] >= 0, "commit_count 無效。")
    preview_value = manifest.get("merge_preview")
    _require(preview_value is None or isinstance(preview_value, dict), "merge_preview 必須是物件或 null。")
    if manifest["mode"] == "merge":
        _require(preview_value is not None, "merge 模式必須有合併預覽紀錄。")
    else:
        _require(preview_value is None, "direct 模式不應包含合併預覽。")
    if preview_value is not None:
        _require(
            preview_value.get("status") in {"clean", "conflicts", "unavailable"},
            "merge_preview.status 無效。",
        )
        _require(isinstance(preview_value.get("changed_files"), list), "merge_preview.changed_files 必須是陣列。")
        _require(
            all(isinstance(item, dict) for item in preview_value["changed_files"]),
            "merge_preview.changed_files 必須只含物件。",
        )
        for index, item in enumerate(preview_value["changed_files"], start=1):
            _require(
                isinstance(item.get("path"), str) and bool(item["path"]),
                f"merge_preview.changed_files[{index}].path 不可空白。",
            )
            _require(
                isinstance(item.get("status"), str) and bool(item["status"]),
                f"merge_preview.changed_files[{index}].status 無效。",
            )
            old_path = item.get("old_path")
            _require(
                old_path is None or (isinstance(old_path, str) and bool(old_path)),
                f"merge_preview.changed_files[{index}].old_path 無效。",
            )
        tree_sha = preview_value.get("tree_sha")
        if preview_value["status"] == "unavailable":
            _require(tree_sha is None, "不可用的 merge_preview 不應有 tree_sha。")
        else:
            _require(_valid_object_id(tree_sha), "merge_preview.tree_sha 必須是完整 Git object ID。")
        if preview_value["status"] in {"conflicts", "unavailable"}:
            _require(not manifest["context_complete"], "合併預覽有缺口時 context_complete 必須為 false。")

    required_paths: set[str] = set()
    required_source_options: dict[str, list[set[str]]] = {}

    def require_source(path: str, sources: set[str]) -> None:
        required_source_options.setdefault(path, []).append(sources)

    for change in manifest["changed_files"]:
        if change.get("path"):
            path = change["path"]
            required_paths.add(path)
            if str(change.get("status", "")).startswith("D"):
                source_options = {"head", "base"} if manifest["review_scope"] == "working-tree" else {"base"}
            else:
                source = "working-tree" if manifest["review_scope"] == "working-tree" else "head"
                source_options = {source}
            require_source(path, source_options)
        if change.get("old_path"):
            old_path = change["old_path"]
            required_paths.add(old_path)
            require_source(old_path, {"head", "base"})
    preview = manifest.get("merge_preview") or {}
    for change in preview.get("changed_files", []):
        if change.get("path"):
            path = change["path"]
            required_paths.add(path)
            require_source(path, {"head" if str(change.get("status", "")).startswith("D") else "merge-preview"})
        if change.get("old_path"):
            old_path = change["old_path"]
            required_paths.add(old_path)
            require_source(old_path, {"head"})

    coverage_by_path: dict[str, dict[str, Any]] = {}
    for item in draft["coverage"]:
        _require(isinstance(item, dict), "每筆 coverage 必須是物件。")
        path = item.get("path")
        _require(isinstance(path, str) and bool(path), "coverage.path 不可空白。")
        _require(path not in coverage_by_path, f"coverage 不可重複列出路徑：{path}")
        _require(item.get("status") in STATUSES, f"coverage.status 無效：{path}")
        coverage_by_path[path] = item
        evidence_list = item.get("evidence", [])
        _require(isinstance(evidence_list, list), f"coverage.evidence 必須為陣列：{path}")
        evidence_sources: set[str] = set()
        for evidence in evidence_list:
            _validate_evidence(evidence, manifest, context_dir, timeout)
            if isinstance(evidence, dict) and evidence.get("path") == path:
                evidence_sources.add(evidence.get("source"))
        if item["status"] == "reviewed":
            _require(bool(evidence_list), f"已審查的路徑必須附上證據：{path}")
            _require(bool(evidence_sources), f"coverage 證據必須包含該路徑：{path}")
            for accepted_sources in required_source_options.get(path, []):
                _require(
                    bool(evidence_sources & accepted_sources),
                    f"coverage 缺少該路徑必要固定版本的證據（{', '.join(sorted(accepted_sources))}）：{path}",
                )
        else:
            _require(
                isinstance(item.get("reason"), str) and bool(item["reason"].strip()),
                f"未完整檢查的路徑必須說明原因：{path}",
            )
    missing = sorted(required_paths - coverage_by_path.keys())
    _require(not missing, "coverage 遺漏必要路徑：" + ", ".join(missing))
    reviewed_paths = {
        path for path, item in coverage_by_path.items() if item["status"] == "reviewed"
    }

    findings = draft["findings"]
    _require(all(isinstance(item, dict) for item in findings), "每筆 finding 必須是物件。")
    seen_ids: set[str] = set()
    expected_order = sorted(findings, key=lambda item: (PRIORITIES.index(item.get("priority", "P3")) if item.get("priority") in PRIORITIES else 99, item.get("id", "")))
    _require(findings == expected_order, "findings 必須依 P0、P1、P2、P3 及編號排序。")
    for index, finding in enumerate(findings, start=1):
        _require(isinstance(finding, dict), "每筆 finding 必須是物件。")
        expected_id = f"F-{index:03}"
        _require(finding.get("id") == expected_id, f"finding 編號必須依序為 {expected_id}。")
        _require(finding["id"] not in seen_ids, f"finding 編號不可重複：{finding['id']}")
        seen_ids.add(finding["id"])
        _require(finding.get("priority") in PRIORITIES, f"finding 嚴重度無效：{finding['id']}")
        for field in ("title", "impact", "trigger", "expected", "actual", "recommendation"):
            _require(
                isinstance(finding.get(field), str) and bool(finding[field].strip()),
                f"{finding['id']} 必須填寫 {field}。",
            )
        _require(finding.get("owner") in OWNERS, f"{finding['id']} 的 owner 無效。")
        _require(
            finding.get("scenario_source") in {"code-derived", "reproduced"},
            f"{finding['id']} 必須標示操作情境來源。",
        )
        evidence_list = finding.get("evidence")
        _require(isinstance(evidence_list, list) and bool(evidence_list), f"{finding['id']} 必須附上證據。")
        for evidence in evidence_list:
            _validate_evidence(evidence, manifest, context_dir, timeout)
        _require(
            any(evidence["path"] in (required_paths & reviewed_paths) for evidence in evidence_list),
            f"{finding['id']} 至少要引用一個已完成審查的變更路徑。",
        )

    for next_step in draft.get("next_steps", []):
        _require(isinstance(next_step, dict), "每個 next_step 必須是物件。")
        _require(isinstance(next_step.get("action"), str) and bool(next_step["action"].strip()), "next_step.action 不可空白。")
        _require(next_step.get("owner") in OWNERS, "next_step.owner 必須是 開發、維運設定 或 QA。")
    _require(len(draft.get("next_steps", [])) <= 3, "next_steps 最多列出三項。")

    incomplete = (
        not manifest.get("context_complete", manifest.get("review_complete", False))
        or any(item["status"] != "reviewed" for item in coverage_by_path.values())
        or preview.get("status") in {"conflicts", "unavailable"}
    )
    if incomplete:
        status = "審查未完成"
    elif not required_paths and not findings:
        status = "沒有差異"
    elif findings:
        status = "發現具體問題"
    else:
        status = "未發現具體問題"

    highest_priority = next((item for item in PRIORITIES if any(f["priority"] == item for f in findings)), None)
    if status == "審查未完成":
        recommendation = "需補做審查"
    elif highest_priority == "P0":
        recommendation = "暫緩合併"
    elif highest_priority == "P1":
        recommendation = "修正後再合併"
    else:
        recommendation = "可以合併"

    result = dict(draft)
    result.update(
        {
            "schema_version": 1,
            "review_status": status,
            "status_explanation": STATUS_EXPLANATIONS[status],
            "merge_recommendation": recommendation,
            "priority_counts": {
                priority: sum(f["priority"] == priority for f in findings)
                for priority in PRIORITIES
            },
            "context_schema_version": manifest["schema_version"],
            "base_sha": manifest["base_sha"],
            "head_sha": manifest["head_sha"],
            "review_tree_sha": manifest.get("review_tree_sha"),
            "context_sha256": hashlib.sha256(
                (context_dir / "manifest.json").read_bytes()
            ).hexdigest(),
        }
    )
    result["limitations"] = list(result.get("limitations", []))
    result["limitations"].extend(manifest.get("context_gaps", []))
    result["limitations"].extend(manifest.get("review_limitations", []))
    if preview.get("status") == "conflicts":
        result["limitations"].append("基礎版本與比較版本的合併預覽存在衝突，合併相容性檢查尚未完成。")
    elif preview.get("status") == "unavailable":
        result["limitations"].append(
            "無法產生合併預覽：" + str(preview.get("detail") or "Git merge-tree 執行失敗。")
        )
    result["limitations"] = list(dict.fromkeys(result["limitations"]))
    return result


def _validate_evidence(
    evidence: Any, manifest: dict[str, Any], context_dir: Path, timeout: float
) -> None:
    _require(isinstance(evidence, dict), "evidence 必須是物件。")
    _require(evidence.get("source") in EVIDENCE_SOURCES, "evidence.source 無效。")
    path = evidence.get("path")
    _require(isinstance(path, str) and bool(path), "evidence.path 不可空白。")
    _require(
        ".." not in Path(path).parts
        and ".." not in PurePosixPath(path).parts
        and ".." not in PureWindowsPath(path).parts
        and not Path(path).is_absolute()
        and not PurePosixPath(path).is_absolute()
        and not PureWindowsPath(path).is_absolute()
        and not PureWindowsPath(path).drive,
        f"evidence.path 無效：{path}",
    )
    reference = evidence.get("ref")
    _require(isinstance(reference, str) and bool(reference), f"evidence.ref 不可空白：{path}")
    line_start = evidence.get("line_start")
    line_end = evidence.get("line_end")
    _require(
        (line_start is None and line_end is None)
        or (
            type(line_start) is int
            and type(line_end) is int
            and 1 <= line_start <= line_end
        ),
        f"evidence 行號必須同時省略或為有效區間：{path}",
    )
    content = _evidence_bytes(evidence, manifest, context_dir, timeout)
    if line_start is not None:
        lines = content.splitlines()
        _require(line_end <= len(lines), f"evidence 行號超出固定版本檔案：{path}:{line_start}-{line_end}")


def render_markdown(result: dict[str, Any], manifest: dict[str, Any]) -> str:
    preview = manifest.get("merge_preview") or {}
    lines = [
        f"# Merge Review: {manifest['project']}",
        "",
        "## 審查結論",
        "",
        f"- 合併建議：`{result['merge_recommendation']}`",
        f"- 審查狀態：`{result['review_status']}`（{result['status_explanation']}）",
        f"- 一句話摘要：{result['summary']}",
        "- 問題數量：" + "／".join(
            f"{priority} {ACTION_LABELS[priority]} `{result['priority_counts'][priority]}`"
            for priority in PRIORITIES
        ),
        "- 下一步：" + (
            "；".join(f"{step['action']}（{step['owner']}）" for step in result.get("next_steps", []))
            or "無需動作"
        ),
        "- 檢查缺口：" + ("；".join(result["limitations"]) or "無"),
        "",
        "## 問題總覽",
        "",
        "| 編號 | 優先程度 | 問題 | 影響 | 建議處理者 |",
        "| --- | --- | --- | --- | --- |",
    ]
    findings = result["findings"]
    for finding in findings:
        label = f"{finding['priority']} {ACTION_LABELS[finding['priority']]}"
        lines.append(
            f"| {finding['id']} | {label} | {finding['title']} | {finding['impact']} | {finding['owner']} |"
        )
    if not findings:
        lines.append("| — | — | 未發現具體問題 | — | — |")
    lines += [
        "",
        "等級：P0 必須立即處理、P1 合併前必修、P2 近期排程修正、P3 可後續改善。",
        "",
        "## 問題詳情",
        "",
    ]
    for finding in findings:
        lines += [
            f"### {finding['id']}｜[{finding['priority']} {ACTION_LABELS[finding['priority']]}] {finding['title']}",
            "",
            f"**影響說明**：{finding['impact']}",
            "",
            f"**操作情境（{'依程式推導；未實際執行' if finding['scenario_source'] == 'code-derived' else '已實際重現'}）**：",
            "",
            f"- 操作或輸入：{finding['trigger']}",
            f"- 預期結果：{finding['expected']}",
            f"- 實際結果：{finding['actual']}",
            "",
            f"**建議處理**：{finding['recommendation']}（{finding['owner']}）。",
            "",
            "#### 技術證據（工程師參考）",
            "",
        ]
        for evidence in finding["evidence"]:
            location = evidence["path"]
            if evidence.get("line_start") is not None:
                location += f":{evidence['line_start']}-{evidence['line_end']}"
            lines.append(f"- {evidence['source']} `{location}`（`{evidence['ref']}`）")
        lines += [
            f"- 觸發條件：{finding['trigger']}",
            f"- 證據說明：{finding.get('evidence_summary', finding['actual'])}",
            f"- 影響範圍：{finding['impact']}",
            f"- 工程修正建議：{finding.get('engineering_fix', finding['recommendation'])}",
            "",
        ]
    if not findings:
        lines.append("此範圍沒有足夠證據建立問題；請勿補造操作範例。")
        lines.append("")
    lines += ["## 範圍與限制", ""]
    lines.append(f"本次共有 {len(result['coverage'])} 個路徑記錄在檢查範圍中。")
    for item in result["coverage"]:
        detail = item.get("reason", "已檢查並附上來源證據。")
        lines.append(f"- `{item['path']}`：{item['status']}；{detail}")
    for item in result.get("tests_executed", []):
        lines.append(f"- 已執行測試：{item}")
    for item in result.get("tests_not_executed", []):
        lines.append(f"- 未執行測試：{item}")
    if result["limitations"]:
        for item in result["limitations"]:
            lines.append(f"- 限制：{item}")
    else:
        lines.append("- 其他限制：無")
    lines += [
        "",
        "## 技術審查紀錄（工程師參考）",
        "",
        f"- 專案與 repository：{manifest['project']}（`{manifest['repo']}`）",
        f"- 比較模式與範圍：`{manifest['mode']}`／`{manifest['review_scope']}`",
        f"- 基礎版本：`{manifest['base_input']}` → `{manifest['base_sha']}`",
        f"- 比較版本：`{manifest['head_input']}` → `{manifest['head_sha']}`",
        f"- 共同起點：`{manifest.get('merge_base')}`",
        f"- 工作區 tree：`{manifest.get('review_tree_sha') or '不適用'}`",
        f"- 遠端同步：`{manifest['fetch_status']}`",
        f"- 工作區執行期間未改變：`{manifest['working_tree_unchanged']}`",
        f"- context manifest SHA-256：`{result['context_sha256']}`",
        "- 合併預覽：" + (
            "不適用（直接比較）" if not preview else
            f"`{preview['status']}`；tree `{preview.get('tree_sha') or '無'}`"
        ),
        f"- 合併提交：`{len(manifest.get('merge_commits', []))}` 筆；commit 數：`{manifest['commit_count']}`",
        "- 固定差異來源：context bundle；問題證據版本與行號見上方技術證據。",
        "",
    ]
    return "\n".join(lines)


def publish_report(
    context_dir: Path,
    result_file: Path,
    report_dir: Path | None = None,
    *,
    timeout: float = 180.0,
    include_json: bool = False,
) -> tuple[Path, Path | None]:
    import review_session

    context_dir = context_dir.expanduser().absolute()
    published: tuple[Path, Path | None] | None = None
    cleanup_required = False
    try:
        manifest_path = context_dir / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest_hint = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest_hint = {}
            cleanup_required = manifest_hint.get("context_cleanup_required") is True
            if cleanup_required:
                review_session.validate_session(context_dir)
        published = _publish_report_contents(
            context_dir, result_file, report_dir, timeout=timeout, include_json=include_json
        )
        return published
    finally:
        try:
            if cleanup_required:
                review_session.cleanup_session(context_dir)
            else:
                review_session.cleanup_if_owned(context_dir)
        except (OSError, ValueError) as exc:
            if published:
                raise review_session.SessionError(
                    f"報告已建立，但暫存清理失敗；報告：{published[0]}"
                    + (f"、{published[1]}" if published[1] else "")
                    + f"；殘留路徑：{context_dir}（{exc}）"
                ) from exc
            raise


def _publish_report_contents(
    context_dir: Path,
    result_file: Path,
    report_dir: Path | None = None,
    *,
    timeout: float = 180.0,
    include_json: bool = False,
) -> tuple[Path, Path | None]:
    result_file = result_file.expanduser().resolve()
    manifest_path = context_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        draft = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportValidationError(f"無法讀取審查 context 或結果 JSON：{exc}") from exc
    if manifest.get("schema") == "merge-reviewer-group-context/v1":
        import group_review
        return group_review.publish(context_dir, draft, manifest, report_dir, include_json)
    result = validate_result(draft, manifest, context_dir, timeout=timeout)
    markdown = render_markdown(result, manifest)
    if manifest.get("mr_context"):
        import mr_contract
        body = mr_contract.normalize_body(markdown)
        markdown, metadata = mr_contract.bind_report(body, manifest, result)
        result.update(report_metadata=metadata, report_body=body)
    if report_dir is None:
        report_dir = Path(manifest["repo"]) / "review-reports"
    report_dir = report_dir.expanduser().resolve()
    try:
        generated = datetime.fromisoformat(manifest["generated_at"].replace("Z", "+00:00"))
    except (ValueError, KeyError, AttributeError) as exc:
        raise ReportValidationError("manifest.generated_at 必須是 ISO-8601 日期時間。") from exc
    if generated.tzinfo is None:
        raise ReportValidationError("manifest.generated_at 必須包含時區。")
    stamp = generated.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"merge-review-{stamp}-{str(manifest['base_sha'])[:8]}-{str(manifest['head_sha'])[:8]}"
    report_dir.mkdir(parents=True, exist_ok=True)
    for suffix in range(0, 1000):
        name = stem if suffix == 0 else f"{stem}-{suffix:02}"
        markdown_path = report_dir / f"{name}.md"
        json_path = report_dir / f"{name}.json"
        if markdown_path.exists() or json_path.exists():
            continue
        result["report_markdown"] = markdown_path.name
        if include_json:
            result["report_json"] = json_path.name
        with tempfile.TemporaryDirectory(prefix="merge-review-report-", dir=report_dir) as temp_dir:
            temp_root = Path(temp_dir)
            temp_md = temp_root / markdown_path.name
            temp_md.write_text(markdown, encoding="utf-8")
            if include_json:
                temp_json = temp_root / json_path.name
                json_payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                temp_json.write_text(json_payload, encoding="utf-8")
            linked_markdown = False
            try:
                os.link(temp_md, markdown_path)
                linked_markdown = True
                if include_json:
                    os.link(temp_json, json_path)
            except FileExistsError:
                if linked_markdown:
                    markdown_path.unlink(missing_ok=True)
                continue
            except OSError:
                if linked_markdown:
                    markdown_path.unlink(missing_ok=True)
                raise
        return markdown_path, json_path if include_json else None
    raise ReportValidationError("同一秒內建立報告次數已達上限。")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-dir", required=True, help="Immutable review context bundle.")
    parser.add_argument("--result", required=True, help="Structured draft result JSON.")
    parser.add_argument("--report-dir", help="Report output directory (defaults to repository/review-reports).")
    parser.add_argument(
        "--include-json", action="store_true", help="Also write the structured JSON report."
    )
    parser.add_argument("--git-timeout", type=float, default=180.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    import review_session

    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not math.isfinite(args.git_timeout) or args.git_timeout <= 0:
        print("review-report: --git-timeout 必須大於零。", file=sys.stderr)
        return 2
    try:
        markdown_path, json_path = publish_report(
            Path(args.context_dir),
            Path(args.result),
            Path(args.report_dir) if args.report_dir else None,
            timeout=args.git_timeout,
            include_json=args.include_json,
        )
    except review_session.SessionError as exc:
        print(f"review-report: cleanup failed: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"review-report: 審查報告未建立：{exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "markdown_report": str(markdown_path),
                "json_report": str(json_path) if json_path is not None else None,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
