"""Single-repository Megin fingerprint and explicit writer lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True

import quality_gate


class InvalidWorkspace(Exception):
    """The selected repository or ownership record is invalid."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        raise InvalidWorkspace(result.stderr.strip() or "Git command failed")
    return result.stdout.strip()


def validate_repo(value: Path) -> Path:
    repo = value.resolve()
    if not repo.is_dir() or Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise InvalidWorkspace("--repo must identify the Git repository root")
    return repo


def fingerprint(repo: Path) -> dict[str, object]:
    root = repo / ".agents" / "skills"
    if root.is_symlink() or not root.is_dir():
        raise InvalidWorkspace("Repo .agents/skills directory is missing or unsafe")
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if "__pycache__" in path.parts or path.suffix.lower() == ".pyc":
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise InvalidWorkspace(f"Skill resource escapes the Repo: {path}")
        if path.is_file():
            raw = path.read_bytes()
            entries.append({
                "path": f".agents/skills/{path.relative_to(root).as_posix()}",
                "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            })
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return {
        "schema": "megin-skills-fingerprint/v1", "algorithm": "sha256",
        "sha256": hashlib.sha256(encoded).hexdigest(), "file_count": len(entries),
        "files": entries,
    }


def _lock_path(repo: Path) -> Path:
    directory = repo / ".megin"
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        raise InvalidWorkspace("Repo .megin directory must be a real directory")
    directory.mkdir(exist_ok=True)
    if not directory.resolve().is_relative_to(repo):
        raise InvalidWorkspace("Repo .megin directory escapes the repository")
    path = directory / "workspace.lock.json"
    if path.is_symlink():
        raise InvalidWorkspace("workspace lock must not be a symlink")
    return path


def _fields(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    header = text.split("\n## ", maxsplit=1)[0]
    for match in re.finditer(r"(?m)^- ([a-z0-9_]+): (.+)$", header):
        key, value = match.groups()
        if key in values:
            raise InvalidWorkspace(f"duplicate workflow field: {key}")
        values[key] = value
    return values


def validate_plan(repo: Path, work_id: str, purpose: str) -> tuple[dict, dict]:
    if not quality_gate.WORK_ID_PATTERN.fullmatch(work_id):
        raise InvalidWorkspace("invalid Work ID")
    fields, contract, _records = quality_gate.load_contract(repo, work_id)
    if fields.get("schema") not in ("megin-repo-workflow/v1", "megin-repo-workflow/v2"):
        raise InvalidWorkspace("Repo lock requires megin-repo-workflow/v1")
    if fields.get("work_id") != work_id:
        raise InvalidWorkspace("workflow Work ID mismatch")
    if purpose == "claim" and (fields.get("phase"), fields.get("status")) != (
        "implementation", "active",
    ):
        raise InvalidWorkspace("claim requires an approved implementation/active workflow")
    from quality_policy import validate_contract
    try:
        validate_contract(repo, work_id, fields, contract, _records,
                          live=purpose == "claim" and not (repo / ".megin/workspace.lock.json").exists())
    except (ValueError, OSError, TypeError, KeyError) as exc:
        raise InvalidWorkspace(f"approved discovery/plan policy: {exc}") from exc
    if contract.get("schema") == "megin-repo-quality-contract/v2":
        quality = quality_gate.evidence_file(repo, work_id, contract["quality_ref"])
        if quality.is_file():
            from quality_policy import validate_reviews
            try:
                recorded = quality_gate.read_json(quality)
                validate_reviews(repo, work_id, contract, _records, recorded, recorded.get("snapshot"), "review")
            except (ValueError, OSError, KeyError, TypeError) as exc:
                raise InvalidWorkspace(f"review history policy: {exc}") from exc
    expected = contract.get("skills_sha256")
    actual = fingerprint(repo)["sha256"]
    if not isinstance(expected, str) or actual != expected:
        raise InvalidWorkspace("installed Megin Skills fingerprint differs from approved plan")
    return fields, contract


def read_lock(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidWorkspace(f"workspace lock is unreadable; preserve it for inspection: {exc}") from exc
    if (not isinstance(value, dict) or value.get("schema") != "megin-repo-workspace-lock/v1"
            or not isinstance(value.get("work_id"), str)
            or not isinstance(value.get("writer"), str)):
        raise InvalidWorkspace("workspace lock has an invalid schema; preserve it for inspection")
    return value


def claim(repo: Path, work_id: str, writer: str) -> dict[str, object]:
    repo = validate_repo(repo)
    if quality_gate._unified_repo(repo, work_id):
        from unified_workflow import claim as unified_claim
        return unified_claim(repo, work_id, writer)
    from workspace_lock import repo_lock
    if repo_lock(repo).exists():
        raise InvalidWorkspace('Repo is owned by a unified workflow; preserve its common-dir lock')
    fields, _contract = validate_plan(repo, work_id, "claim")
    if not writer.strip() or any(ord(char) < 32 for char in writer):
        raise InvalidWorkspace("writer identity must be non-empty and contain no control characters")
    path = _lock_path(repo)
    value = {
        "schema": "megin-repo-workspace-lock/v1", "work_id": work_id,
        "writer": writer, "claimed_at": datetime.now(timezone.utc).isoformat(),
        "skills_sha256": fingerprint(repo)["sha256"],
    }
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        current = read_lock(path)
        if current.get("work_id") == work_id and current.get("writer") == writer:
            return current
        raise InvalidWorkspace(
            f"Repo is owned by work {current.get('work_id')} / {current.get('writer')}; explicit handoff is required"
        )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return value


def check_owner(repo: Path, work_id: str, writer: str) -> dict[str, object]:
    repo = validate_repo(repo)
    if quality_gate._unified_repo(repo, work_id):
        from unified_workflow import check_owner as unified_check
        return unified_check(repo, work_id, writer)
    _fields_value, _contract = validate_plan(repo, work_id, "check")
    value = read_lock(_lock_path(repo))
    if value.get("work_id") != work_id or value.get("writer") != writer:
        raise InvalidWorkspace("Repo lock owner does not match this Work ID and writer")
    current = fingerprint(repo)["sha256"]
    if value.get("skills_sha256") != current:
        raise InvalidWorkspace("installed Megin Skills fingerprint changed while the Repo is locked")
    return value


def release(repo: Path, work_id: str, writer: str) -> dict[str, object]:
    repo = validate_repo(repo)
    if quality_gate._unified_repo(repo, work_id):
        from unified_workflow import release as unified_release
        return unified_release(repo, work_id, writer)
    fields = _fields(repo / "docs" / "work" / work_id / "workflow.md")
    if fields.get("phase") != "delivery" or fields.get("status") != "complete":
        raise InvalidWorkspace("Repo lock can be released only after a completed delivery")
    owner = check_owner(repo, work_id, writer)
    result = quality_gate.check_completion(repo, work_id)
    if not result.get("ok"):
        raise InvalidWorkspace("completion gate failed; keep the Repo lock: " + "; ".join(result["reasons"]))
    path = _lock_path(repo)
    current = read_lock(path)
    if current != owner:
        raise InvalidWorkspace("Repo lock changed during release; preserve it for inspection")
    path.unlink()
    return {"released": True, "work_id": work_id, "writer": writer}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("fingerprint", "claim", "check", "release"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--work-id")
    parser.add_argument("--writer")
    args = parser.parse_args()
    try:
        repo = validate_repo(args.repo)
        if args.action == "fingerprint":
            result = fingerprint(repo)
        else:
            if not args.work_id or not args.writer:
                raise InvalidWorkspace(f"{args.action} requires --work-id and --writer")
            if args.action == "claim":
                result = claim(repo, args.work_id, args.writer)
            elif args.action == "check":
                result = check_owner(repo, args.work_id, args.writer)
            else:
                result = release(repo, args.work_id, args.writer)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidWorkspace, quality_gate.InvalidEvidence, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
