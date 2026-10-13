"""Git-normalized snapshots and dependency validation reused from the Group overlay."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
from quality_gate import InvalidEvidence, git, line, canonical_relative

def validate_handoff_contract(contract: dict, repo_paths: list[str], check_ids: set[str]) -> None:
    handoff = contract.get("handoff")
    if not isinstance(handoff, dict):
        raise InvalidEvidence("v3 contract needs a handoff object")
    dependencies = handoff.get("dependencies")
    order = handoff.get("merge_order")
    compatibility = handoff.get("compatibility_check_ids")
    partial = handoff.get("partial_delivery")
    independent_reason = handoff.get("independent_reason")
    expected = set(repo_paths)
    if not isinstance(dependencies, dict) or set(dependencies) != expected:
        raise InvalidEvidence("handoff dependencies must declare every selected Repo exactly once")
    if (not isinstance(order, list) or any(not isinstance(item, str) for item in order)
            or len(order) != len(set(order)) or set(order) != expected):
        raise InvalidEvidence("handoff merge order must cover every selected Repo exactly once")
    adjacency: dict[str, list[str]] = {}
    for repo_path, prereqs in dependencies.items():
        if (not isinstance(prereqs, list) or any(not isinstance(item, str) for item in prereqs)
                or len(prereqs) != len(set(prereqs)) or repo_path in prereqs
                or any(item not in expected for item in prereqs)):
            raise InvalidEvidence(f"invalid handoff dependencies for {repo_path}")
        adjacency[repo_path] = prereqs
    marks: dict[str, int] = {}

    def visit(repo_path: str) -> None:
        mark = marks.get(repo_path, 0)
        if mark == 1:
            raise InvalidEvidence("handoff dependency cycle detected")
        if mark == 2:
            return
        marks[repo_path] = 1
        for dependency in adjacency[repo_path]:
            visit(dependency)
        marks[repo_path] = 2

    for repo_path in repo_paths:
        visit(repo_path)
    positions = {repo_path: index for index, repo_path in enumerate(order)}
    for repo_path, prereqs in adjacency.items():
        if any(positions[dependency] >= positions[repo_path] for dependency in prereqs):
            raise InvalidEvidence("handoff merge order violates a declared dependency")
    if not isinstance(compatibility, list) or any(
        not isinstance(item, str) or not item for item in compatibility
    ) or len(compatibility) != len(set(compatibility)):
        raise InvalidEvidence("handoff compatibility_check_ids must be a unique list")
    if len(repo_paths) > 1 and not compatibility:
        raise InvalidEvidence("multi-Repo handoff needs at least one compatibility check ID")
    for identifier in compatibility:
        if identifier not in check_ids:
            raise InvalidEvidence(f"handoff compatibility check does not exist: {identifier}")
    if len(repo_paths) > 1 and not any(adjacency.values()):
        if not isinstance(independent_reason, str) or not independent_reason.strip():
            raise InvalidEvidence("independent Repos need an explicit handoff reason")
    if not isinstance(partial, str) or not partial.strip():
        raise InvalidEvidence("handoff needs instructions for partial delivery")


def group_index_entries(repo: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for record in git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", maxsplit=1)
        mode, object_id, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise InvalidEvidence("unmerged Group repository index cannot be checked")
        entries.append({
            "path": raw_name.decode("utf-8"), "mode": mode, "content": object_id,
        })
    return sorted(entries, key=lambda item: item["path"])


def group_file_mode(path: Path, indexed_mode: str | None, filemode: bool) -> str | None:
    if path.is_symlink():
        return "120000"
    if path.is_dir() or not path.is_file():
        return None
    if not filemode:
        if indexed_mode in ("100644", "100755"):
            return indexed_mode
        return "100644"
    return "100755" if path.stat().st_mode & 0o111 else "100644"


def group_repo_entries(repo: Path) -> list[dict[str, str]]:
    paths = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    indexed = {entry["path"]: entry for entry in group_index_entries(repo)}
    filemode_result = subprocess.run(
        ["git", "-C", str(repo), "config", "--bool", "--get", "core.filemode"],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if filemode_result.returncode == 0:
        filemode = filemode_result.stdout.strip().lower() == "true"
    elif filemode_result.returncode == 1:
        filemode = os.name != "nt"
    else:
        raise InvalidEvidence("could not read Git core.filemode configuration")

    entries: list[dict[str, str]] = []
    for raw in sorted(set(paths.split(b"\0")) - {b""}):
        relative = canonical_relative(raw.decode("utf-8"))
        path = repo / relative
        resolved = path.resolve()
        if not resolved.is_relative_to(repo):
            raise InvalidEvidence(f"repository path escapes its Git root: {relative}")
        index_entry = indexed.get(relative)
        indexed_mode = index_entry["mode"] if index_entry else None

        if indexed_mode == "160000":
            content = index_entry["content"]
            if path.is_dir():
                result = subprocess.run(
                    ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                    text=True, encoding="utf-8", errors="replace",
                    capture_output=True, check=False,
                )
                if result.returncode == 0:
                    status = subprocess.run(
                        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
                        text=True, encoding="utf-8", errors="replace",
                        capture_output=True, check=False,
                    )
                    if status.returncode != 0:
                        raise InvalidEvidence(f"could not inspect submodule status: {relative}")
                    if status.stdout:
                        raise InvalidEvidence(f"dirty submodule cannot be snapshotted: {relative}")
                    content = result.stdout.strip()
            entries.append({"path": relative, "mode": "160000", "content": content})
            continue

        mode = group_file_mode(path, indexed_mode, filemode)
        if mode is None:
            # Missing tracked paths are absent from the worktree tree and must
            # compare with their staged deletion at delivery.
            continue
        if mode == "120000":
            result = subprocess.run(
                ["git", "-C", str(repo), "hash-object", "--stdin"],
                input=os.fsencode(os.readlink(path)), capture_output=True, check=False,
            )
            if result.returncode != 0:
                raise InvalidEvidence(result.stderr.decode("utf-8", errors="replace").strip())
            content = result.stdout.decode("ascii").strip()
        else:
            content = line(repo, "hash-object", f"--path={relative}", relative)
        entries.append({"path": relative, "mode": mode, "content": content})
    return entries
