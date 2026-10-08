"""Verify a single-Repo Megin receipt using Git and stdlib; no Megin installation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


def canonical_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _path(value) -> str:
    _require(isinstance(value, str) and bool(value) and not any(c in value for c in ("\\", ":", "\0")),
             "receipt path must be canonical and Repo-relative")
    pure = PurePosixPath(value)
    _require(not pure.is_absolute() and pure.as_posix() == value and ".." not in pure.parts,
             "receipt path escapes its Repo")
    return value


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, check=False)
    _require(proc.returncode == 0, "receipt Git object is unavailable")
    return proc.stdout


def _line(repo: Path, *args: str) -> str:
    return _git(repo, *args).decode("utf-8").strip()


def _regular_bytes(repo: Path, relative: str) -> bytes:
    target = repo / _path(relative)
    _require(target.resolve().is_relative_to(repo), "receipt record escapes its Repo")
    for path in (target, *target.parents):
        _require(not path.is_symlink() and not (path.exists() and
                 getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)),
                 "receipt record traverses a link or reparse point")
        if path == repo:
            break
    _require(target.is_file(), "receipt record missing")
    return target.read_bytes()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "duplicate JSON key in receipt evidence")
        value[key] = item
    return value


def _json(raw: bytes) -> dict:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    _require(isinstance(value, dict), "receipt evidence must be an object")
    return value


def validate_receipt(receipt: dict, repo: Path) -> dict:
    repo = Path(repo).resolve()
    _require(isinstance(receipt, dict) and receipt.get("schema") == "megin-repo-delivery-receipt/v1",
             "expected a single-Repo Megin delivery receipt")
    _require(isinstance(receipt.get("repo_root"), str) and Path(receipt["repo_root"]).resolve() == repo,
             "receipt belongs to a different Repo")
    payload = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    _require(canonical_digest(payload) == receipt.get("receipt_sha256"), "receipt digest changed")
    work, version = receipt.get("work_id"), receipt.get("plan_version")
    _require(isinstance(work, str) and re.fullmatch(r"work-[0-9]{8}-[a-z0-9-]+", work) is not None,
             "invalid receipt Work ID")
    _require(isinstance(version, str) and re.fullmatch(r"plan-[a-z0-9-]+", version) is not None,
             "invalid receipt plan version")
    _require(receipt.get("local_delivery_complete") is True, "local delivery is incomplete")
    prefix = f"docs/work/{work}/"
    files = receipt.get("record_files")
    _require(isinstance(files, list) and bool(files), "receipt record hashes missing")
    raw_files = {}
    for file in files:
        _require(isinstance(file, dict) and set(file) == {"path", "sha256"}, "invalid receipt record")
        relative = _path(file["path"])
        _require(relative.startswith(prefix) and relative not in raw_files, "duplicate or foreign receipt record")
        raw = _regular_bytes(repo, relative)
        _require(hashlib.sha256(raw).hexdigest() == file["sha256"], "historical receipt record changed")
        raw_files[relative] = raw
    refs = [receipt.get(k) for k in ("contract_ref", "quality_ref", "delivery_ref")]
    _require(all(isinstance(p, str) and p in raw_files for p in refs), "receipt lacks its contract/quality/delivery records")
    _require(refs[0] == prefix + version + "/quality-contract.json", "receipt contract version differs")
    contract, quality, delivery = [_json(raw_files[p]) for p in refs]
    for value, schema in ((contract, "megin-repo-quality-contract/v1"),
                          (quality, "megin-repo-quality-evidence/v1"),
                          (delivery, "megin-repo-delivery-result/v1")):
        _require((value.get("schema"), value.get("work_id"), value.get("plan_version")) == (schema, work, version),
                 "receipt evidence identity differs")
    _require(contract.get("quality_ref") == refs[1] and contract.get("delivery_ref") == refs[2],
             "receipt evidence references differ")
    records = contract.get("process_records")
    _require(isinstance(records, list) and all(isinstance(p, str) and _path(p).startswith(prefix + "evidence/") for p in records)
             and len(set(records)) == len(records) and refs[1] in records and refs[2] in records,
             "invalid receipt process records")
    excluded = set(records) | {prefix + "workflow.md", ".megin/workspace.lock.json"}
    base, feature, merge = [receipt.get(k) for k in ("base_commit", "feature_commit", "merge_commit")]
    _require(all(isinstance(x, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", x) for x in (base, feature, merge)),
             "receipt commits must be full object IDs")
    _require(contract.get("base_commit") == base and delivery.get("feature_commit") == feature and delivery.get("merge_commit") == merge,
             "receipt commit identity differs")
    _require(_line(repo, "show", "-s", "--format=%P", feature).split() == [base], "feature ancestry differs")
    _require(_line(repo, "show", "-s", "--format=%P", merge).split() == [base, feature], "local merge ancestry differs")
    tree = _line(repo, "rev-parse", feature + "^{tree}")
    _require(tree == receipt.get("tree_sha") == _line(repo, "rev-parse", merge + "^{tree}"), "delivered tree differs")
    entries = []
    for raw in _git(repo, "ls-tree", "-r", "-z", "--full-tree", feature).split(b"\0"):
        if not raw:
            continue
        metadata, name = raw.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        relative = name.decode("utf-8")
        if relative in excluded:
            continue
        entries.append({"path": relative, "content": blob})
        if relative.startswith(prefix):
            _require(relative in raw_files and kind == "blob" and mode in ("100644", "100755"),
                     "protected work record missing")
            _require(_line(repo, "hash-object", "--path=" + relative, relative) == blob, "protected work record differs from delivered commit")
    checks = contract.get("checks")
    _require(isinstance(checks, list) and checks and all(isinstance(c, dict) and isinstance(c.get("id"), str) for c in checks),
             "receipt check obligations missing")
    check_ids = {c["id"] for c in checks}
    _require(len(check_ids) == len(checks), "duplicate receipt checks")
    inputs = []
    seen_inputs = set()
    for item in contract.get("verification_inputs") or []:
        _require(isinstance(item, dict) and set(item) == {"commit", "files", "check_ids"}, "invalid single-Repo fixed input")
        commit = item["commit"]
        _require(isinstance(commit, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit), "invalid fixed input commit")
        _require(_line(repo, "rev-parse", "--verify", commit + "^{commit}") == commit, "fixed input commit unavailable")
        ids = item["check_ids"]
        _require(isinstance(ids, list) and ids and all(isinstance(i, str) and i in check_ids for i in ids)
                 and len(set(ids)) == len(ids), "invalid fixed input check IDs")
        _require(isinstance(item["files"], list) and bool(item["files"]), "fixed input files missing")
        normalized = []
        for file in item["files"]:
            _require(isinstance(file, dict) and set(file) == {"path", "sha256"}, "invalid fixed input file")
            path = _path(file["path"])
            _require((commit, path) not in seen_inputs, "duplicate fixed input")
            seen_inputs.add((commit, path))
            rows = _git(repo, "ls-tree", "-z", commit, "--", path).split(b"\0")
            _require(len(rows) == 2 and bool(rows[0]), "fixed input absent")
            metadata, name = rows[0].split(b"\t", 1)
            mode, kind, blob = metadata.decode().split()
            _require(name.decode() == path and kind == "blob" and mode in ("100644", "100755"), "fixed input is not a regular blob")
            _require(hashlib.sha256(_git(repo, "cat-file", "blob", commit + ":" + path)).hexdigest() == file["sha256"], "fixed input bytes differ")
            normalized.append({"path": path, "sha256": file["sha256"], "mode": mode, "blob_sha": blob})
        inputs.append({"commit": commit, "files": normalized, "check_ids": list(ids)})
    if contract.get("verification_inputs") is not None:
        _require(receipt.get("verification_inputs") == inputs, "receipt fixed inputs differ")
    entries.sort(key=lambda e: e["path"])
    entries.extend({"path": "@verification/" + item["commit"] + "/" + file["path"], "mode": file["mode"], "content": file["blob_sha"]}
                   for item in inputs for file in item["files"])
    snapshot = canonical_digest(entries)
    _require(snapshot == receipt.get("accepted_snapshot") == quality.get("snapshot") == delivery.get("accepted_snapshot"),
             "delivered bytes differ from accepted snapshot")
    gate = delivery.get("delivery_gate", {})
    stdout = gate.get("stdout")
    _require(isinstance(stdout, str) and type(gate.get("exit_code")) is int and gate["exit_code"] == 0
             and hashlib.sha256(stdout.encode("utf-8")).hexdigest() == gate.get("sha256"), "passing delivery receipt missing")
    gate_result = _json(stdout.encode("utf-8"))
    _require(gate_result.get("gate") == "delivery" and gate_result.get("ok") is True and gate_result.get("snapshot") == snapshot,
             "delivery gate does not pass for this snapshot")

    def claims(value, expected, *, require_locator=False):
        _require(isinstance(value, dict) and value.get("path") in raw_files and value["path"] in records,
                 "raw receipt claim missing")
        raw = raw_files[value["path"]]
        _require(hashlib.sha256(raw).hexdigest() == value.get("sha256"), "raw claim digest differs")
        lines = raw.decode("utf-8").splitlines()
        if require_locator:
            number, text = value.get("line"), value.get("text")
            _require(type(number) is int and 0 < number <= len(lines) and
                     isinstance(text, str) and lines[number - 1] == text, "raw output locator differs")
        for key, text in expected.items():
            locator = value.get("claims", {}).get(key, {})
            number = locator.get("line")
            _require(type(number) is int and 0 < number <= len(lines) and locator.get("text") == text and lines[number - 1] == text,
                     "raw claim differs from structured evidence")

    sources = quality.get("sources")
    _require(isinstance(sources, list), "supporting source references missing")
    for source in sources:
        claims(source, {})
    writer, review, acceptance = [quality.get(k, {}) for k in ("writer", "review", "acceptance")]
    for value in (writer, review, acceptance):
        _require(isinstance(value, dict) and value.get("snapshot") == snapshot, "acceptance/review snapshot differs")
    _require(isinstance(writer.get("context"), str) and writer["context"] and isinstance(review.get("context"), str)
             and review["context"] and review["context"] != writer["context"] and review.get("verdict") == "APPROVED", "independent review missing")
    claims(writer.get("source"), {"context": "- context: " + writer["context"], "snapshot": "- snapshot: " + snapshot})
    claims(review.get("source"), {"context": "- context: " + review["context"], "snapshot": "- snapshot: " + snapshot, "verdict": "- verdict: APPROVED"})
    _require(acceptance.get("work_id") == work and acceptance.get("verdict") == "ACCEPTED"
             and isinstance(acceptance.get("version"), str) and acceptance["version"] and acceptance.get("actor_kind", "human") == "human", "human acceptance missing")
    claims(acceptance.get("source"), {"work_id": "- work_id: " + work, "version": "- version: " + acceptance["version"],
                                    "snapshot": "- snapshot: " + snapshot, "verdict": "- verdict: ACCEPTED"})
    results = quality.get("checks")
    _require(isinstance(results, list) and all(isinstance(c, dict) and isinstance(c.get("id"), str) for c in results), "check results missing")
    by_id = {c["id"]: c for c in results}
    _require(set(by_id) == check_ids and len(by_id) == len(results), "check result IDs differ")
    for check in checks:
        result = by_id[check["id"]]
        _require(result.get("snapshot") == snapshot and result.get("status") == "passed"
                 and type(result.get("exit_code")) is int and result["exit_code"] == 0, "required check did not pass")
        expected = {"command": "Command: " + check["command"], "exit_code": "Exit code: 0"}
        if "cwd" in check:
            expected["cwd"] = "Working directory: " + check["cwd"]
        if check.get("kind") == "test":
            _require(all(type(result.get(k)) is int for k in ("executed", "failed", "skipped"))
                     and result["executed"] > 0 and result["failed"] == result["skipped"] == 0, "test counts did not pass")
        if any(check["id"] in item["check_ids"] for item in inputs):
            digest = canonical_digest(inputs)
            _require(result.get("verification_inputs_sha256") == digest, "fixed input proof missing")
            expected["verification_inputs"] = "Verification inputs SHA-256: " + digest
        claims(result.get("output"), expected, require_locator=True)
    return receipt


def bind_delivery(receipt: dict, manifest: dict) -> dict:
    _require(manifest.get("review_scope") == "committed" and manifest.get("review_right") == manifest.get("head_sha"),
             "Megin receipt requires fixed committed refs")
    validate_receipt(receipt, Path(manifest["repo"]))
    _require(manifest["head_sha"] in (receipt["feature_commit"], receipt["merge_commit"]),
             "review head differs from Megin delivery")
    return {"schema": "megin-repo-review-binding/v1", "work_id": receipt["work_id"],
            "plan_version": receipt["plan_version"], "receipt_sha256": receipt["receipt_sha256"],
            "head_sha": manifest["head_sha"], "receipt": receipt}


def verify_manifest_binding(manifest: dict) -> dict | None:
    if "megin_binding" not in manifest:
        return None
    binding = manifest["megin_binding"]
    _require(isinstance(binding, dict), "invalid Megin report binding")
    _require(bind_delivery(binding.get("receipt"), manifest) == binding, "Megin report binding changed")
    return binding
