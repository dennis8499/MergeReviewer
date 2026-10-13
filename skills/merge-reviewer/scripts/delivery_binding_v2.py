"""Verify feature-only Megin receipts with the bundled, isolated native policy.

Only historical read-only export is invoked. No installed Megin, live remote,
project command, checkout, or working-tree snapshot is used to prove delivery.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from delivery_binding import canonical_digest, _path, _regular_bytes, _require

SCHEMAS = {"megin-repo-delivery-receipt/v2", "megin-delivery-receipt/v2"}
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
RUNTIME = Path(__file__).resolve().parent / "receipt_runtime"


def _root(receipt: dict, root: Path) -> Path:
    _require(isinstance(receipt, dict) and receipt.get("schema") in SCHEMAS,
             "expected a feature-only Megin v2 receipt")
    root = Path(root).absolute()
    _require(not root.is_symlink() and root.is_dir(), "receipt workspace root is unavailable")
    declared = receipt.get("workspace_root")
    _require(isinstance(declared, str) and Path(declared).is_absolute()
             and Path(declared).resolve() == root.resolve(), "receipt belongs to a different workspace")
    return root.resolve()


def validate_receipt(receipt: dict, root: Path) -> dict:
    root = _root(receipt, root)
    _require(canonical_digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
             == receipt.get("receipt_sha256"), "receipt digest changed")
    work, version = receipt.get("work_id"), receipt.get("plan_version")
    _require(isinstance(work, str) and re.fullmatch(r"work-[0-9]{8}-[a-z0-9-]+", work),
             "invalid receipt Work ID")
    _require(isinstance(version, str) and re.fullmatch(r"plan-[a-z0-9-]+", version),
             "invalid receipt plan version")
    _require(receipt.get("delivery_mode") == "feature_handoff"
             and receipt.get("local_delivery_complete") is True, "local delivery is incomplete")
    prefix = f"docs/work/{work}/"
    records, seen = receipt.get("record_files"), set()
    _require(isinstance(records, list) and records, "receipt record hashes missing")
    for record in records:
        _require(isinstance(record, dict) and set(record) == {"path", "sha256"}, "invalid receipt record")
        relative = _path(record["path"])
        _require(relative.startswith(prefix) and relative not in seen, "duplicate or foreign receipt record")
        seen.add(relative)
        _require(hashlib.sha256(_regular_bytes(root, relative)).hexdigest() == record["sha256"],
                 "historical receipt record changed")
    # The isolated runtime re-exports from the actual committed objects and all
    # approval/TDD/review/acceptance records, rather than trusting receipt flags.
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    command = [sys.executable, "-I", "-B", str(RUNTIME / "verify.py"), str(root), work,
               "group" if receipt["schema"] == "megin-delivery-receipt/v2" else "repo"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env=env, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("historical Megin receipt verification could not complete") from exc
    _require(result.returncode == 0, "historical Megin receipt proof failed: " + result.stderr.strip()[:1500])
    try:
        reconstructed = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ValueError("historical verifier returned invalid evidence") from exc
    _require(reconstructed == receipt, "receipt differs from verified historical delivery")
    return receipt


def receipt_seeds(receipt: dict, root: Path) -> list[dict]:
    validate_receipt(receipt, root)
    rows, result, seen = receipt.get("repositories"), [], set()
    _require(isinstance(rows, list) and rows, "receipt repositories missing")
    group = receipt["schema"] == "megin-delivery-receipt/v2"
    for row in rows:
        _require(isinstance(row, dict), "invalid receipt repository")
        name = row.get("repo_path")
        _require(isinstance(name, str) and name not in seen and
                 (group and name not in (".", "..") and "/" not in name and "\\" not in name and ":" not in name
                  or not group and name == "."), "invalid receipt repository path")
        seen.add(name)
        base, feature = row.get("base_commit"), row.get("feature_commit")
        _require(isinstance(base, str) and SHA.fullmatch(base) and isinstance(feature, str) and SHA.fullmatch(feature),
                 "receipt commits must be full object IDs")
        result.append({"repo_path": name, "base_commit": base, "head_commit": feature})
    return result


def bind_delivery(receipt: dict, manifest: dict) -> dict:
    portable = str(manifest.get("schema", "")).startswith("merge-reviewer-workspace-context/")
    # A native Repo receipt can seed a broader declared Group review. Its
    # historical records still live at the delivered Repo root; captured seed
    # paths below bind that root to the review instead of guessing a parent.
    root = Path(receipt["workspace_root"] if portable else manifest["repo"])
    seeds = receipt_seeds(receipt, root)
    reviewed = seeds
    if portable:
        _require(manifest.get("target") == "branch", "Megin receipt requires fixed committed refs")
        repos = {Path(r["path"]).resolve(): r for r in manifest["repositories"]}
        selected = set(manifest["seed_repos"])
        if manifest.get("scope") == "repo":
            _require(len(selected) == 1 and len(repos) == 1, "Repo scope must capture exactly one delivered Repo")
            reviewed = [seed for seed in seeds if
                        (root if seed["repo_path"] == "." else root / seed["repo_path"]).resolve() in repos]
            _require(len(reviewed) == 1, "selected Repo is not part of this Megin delivery")
        expected = set()
        for seed in reviewed:
            path = root if seed["repo_path"] == "." else root / seed["repo_path"]
            row = repos.get(path.resolve())
            _require(row is not None and row.get("id") in selected, "receipt delivery Repo is not a review seed")
            expected.add(row["id"])
            native = row.get("native") or {}
            head = row.get("sources", {}).get("head", {}).get("ref")
            _require(head == seed["head_commit"] and native.get("base_sha") == seed["base_commit"],
                     "review versions differ from Megin delivery")
        _require(selected == expected, "review seed set differs from Megin delivery")
    else:
        _require(len(seeds) == 1 and seeds[0]["repo_path"] == "."
                 and manifest.get("review_scope") == "committed"
                 and manifest.get("review_right") == manifest.get("head_sha") == seeds[0]["head_commit"],
                 "Megin receipt requires its exact feature commit")
        _require(manifest.get("base_sha") == seeds[0]["base_commit"], "review base differs from Megin delivery")
    return {"schema": "megin-review-binding/v2", "work_id": receipt["work_id"],
            "plan_version": receipt["plan_version"], "receipt_sha256": receipt["receipt_sha256"],
            "repositories": seeds, "reviewed_repositories": reviewed, "receipt": receipt}


def verify_manifest_binding(manifest: dict) -> dict | None:
    binding = manifest.get("megin_binding")
    if binding is None:
        return None
    _require(isinstance(binding, dict) and binding.get("schema") == "megin-review-binding/v2",
             "invalid Megin v2 report binding")
    _require(bind_delivery(binding.get("receipt"), manifest) == binding, "Megin report binding changed")
    return binding
