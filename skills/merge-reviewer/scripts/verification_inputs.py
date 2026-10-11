"""Freeze and materialize committed verification inputs from one selected Repo."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def canonical_digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def relative_path(value) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in ("\\", ":", "\0")):
        raise ValueError("input path must be canonical and Repo-relative")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(p in (".", "..") for p in path.parts):
        raise ValueError("input path escapes its Repo")
    return value


def git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError("fixed verification input Git object is unavailable")
    return result.stdout


def validate_inputs(repo: Path, inputs, check_ids: set[str]) -> list[dict]:
    if inputs is None:
        return []
    if not isinstance(inputs, list):
        raise ValueError("verification_inputs must be an array")
    repo = Path(repo).resolve()
    if Path(git_bytes(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve() != repo:
        raise ValueError("--repo must identify the selected Git root")
    values, seen = [], set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {"commit", "files", "check_ids"}:
            raise ValueError("single-Repo input needs commit, files and check_ids")
        commit = item["commit"]
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise ValueError("input commit must be a full object ID")
        if git_bytes(repo, "rev-parse", "--verify", commit + "^{commit}").decode().strip() != commit:
            raise ValueError("input must identify an exact commit")
        ids = item["check_ids"]
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(x, str) or x not in check_ids for x in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("input check IDs are missing, unknown or duplicated")
        files = item["files"]
        if not isinstance(files, list) or not files:
            raise ValueError("input files must not be empty")
        normalized = []
        for file in files:
            if not isinstance(file, dict) or set(file) != {"path", "sha256"}:
                raise ValueError("input file needs path and sha256")
            path = relative_path(file["path"])
            if (commit, path) in seen:
                raise ValueError("duplicate verification input file")
            seen.add((commit, path))
            digest = file["sha256"]
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise ValueError("invalid input digest")
            records = git_bytes(repo, "ls-tree", "-z", commit, "--", path).split(b"\0")
            if len(records) != 2 or not records[0]:
                raise ValueError("fixed input is absent or ambiguous")
            metadata, name = records[0].split(b"\t", 1)
            mode, kind, blob = metadata.decode().split()
            if kind != "blob" or mode not in ("100644", "100755") or name.decode() != path:
                raise ValueError("fixed input must be a regular Git blob")
            if hashlib.sha256(git_bytes(repo, "cat-file", "blob", commit + ":" + path)).hexdigest() != digest:
                raise ValueError("fixed verification input digest changed")
            normalized.append({"path": path, "sha256": digest, "mode": mode, "blob_sha": blob})
        values.append({"commit": commit, "files": normalized, "check_ids": list(ids)})
    return values


def snapshot_entries(inputs: list[dict]) -> list[dict]:
    return [{"path": "@verification/" + item["commit"] + "/" + file["path"],
             "mode": file["mode"], "content": file["blob_sha"]}
            for item in inputs for file in item["files"]]


def no_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (candidate.exists() and
                getattr(candidate.lstat(), "st_file_attributes", 0) &
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("output must not traverse a link or reparse point")


def materialize_inputs(repo: Path, inputs, check_ids: set[str], destination: Path) -> dict:
    values = validate_inputs(repo, inputs, check_ids)
    destination = Path(destination).absolute()
    no_links(destination)
    if destination.resolve().is_relative_to(Path(repo).resolve()):
        raise ValueError("materialize inputs outside the product Repo")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("materialization destination must be an empty directory")
    # Resolve all committed bytes before the first write.
    payload = [(item["commit"], file["path"], git_bytes(repo, "cat-file", "blob",
                item["commit"] + ":" + file["path"])) for item in values for file in item["files"]]
    destination.mkdir(parents=True, exist_ok=True)
    for commit, path, raw in payload:
        target = destination / commit / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return {"schema": "megin-repo-verification-inputs/v1",
            "sha256": canonical_digest(values), "inputs": values}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        from quality_gate import load_contract, WORK_ID_PATTERN, InvalidEvidence
        from repo_workspace import validate_repo, InvalidWorkspace
        if not WORK_ID_PATTERN.fullmatch(args.work_id):
            raise ValueError("invalid Work ID")
        _, contract, _ = load_contract(validate_repo(args.repo), args.work_id)
        result = materialize_inputs(args.repo, contract.get("verification_inputs"),
                                    {c["id"] for c in contract["checks"]}, args.destination)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, InvalidEvidence, InvalidWorkspace) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
