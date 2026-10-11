"""Validate single-Repo scenario coverage and execute frozen runner probes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True

from verification_inputs import relative_path, no_links


def validate_preflight(contract: dict) -> list[dict]:
    probes = contract.get("runner_preflight", [])
    if not isinstance(probes, list):
        raise ValueError("runner_preflight must be an array")
    seen = set()
    for item in probes:
        if (not isinstance(item, dict) or set(item) != {"id", "argv", "cwd"}
                or not isinstance(item["id"], str) or not item["id"] or item["id"] in seen):
            raise ValueError("invalid or duplicate runner preflight")
        seen.add(item["id"])
        if item["cwd"] != ".":
            relative_path(item["cwd"])
        argv = item["argv"]
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or "\0" in x for x in argv):
            raise ValueError("runner preflight needs exact argv")
    return probes


def validate_trace(contract: dict) -> list[dict]:
    validate_preflight(contract)
    scenarios, trace = contract.get("scenario_ids"), contract.get("behavior_trace")
    if scenarios is None and trace is None:
        return []
    if not isinstance(scenarios, list) or not scenarios or not isinstance(trace, list) or not trace:
        raise ValueError("scenario_ids and behavior_trace must both be nonempty arrays")
    if (any(not isinstance(x, str) or not re.fullmatch(r"SCN-[A-Za-z0-9-]+", x) for x in scenarios)
            or len(set(scenarios)) != len(scenarios)):
        raise ValueError("scenario IDs must be unique")
    checks, seen = {c["id"] for c in contract["checks"]}, set()
    for row in trace:
        if not isinstance(row, dict):
            raise ValueError("scenario row must be an object")
        sid = row.get("scenario_id")
        if not isinstance(sid, str) or sid not in scenarios or sid in seen:
            raise ValueError("scenario is missing, duplicated or unknown")
        seen.add(sid)
        if any(not isinstance(row.get(key), str) or not row[key].strip() for key in ("observable", "assertion")):
            raise ValueError("each scenario needs an observable result and assertion")
        if not isinstance(row.get("check_id"), str) or row["check_id"] not in checks:
            raise ValueError("scenario refers to an unknown check")
        paths = row.get("implementation_paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("scenario implementation paths missing")
        for path in paths:
            relative_path(path)
    if seen != set(scenarios):
        raise ValueError("some scenarios have no observable assertion")
    return trace


def preflight(repo: Path, contract: dict) -> dict:
    repo = Path(repo).resolve()
    results = []
    for probe in validate_preflight(contract):
        cwd = repo if probe["cwd"] == "." else repo / probe["cwd"]
        no_links(cwd)
        if not cwd.resolve().is_relative_to(repo) or not cwd.is_dir():
            raise ValueError("preflight cwd must stay inside the selected Repo")
        try:
            proc = subprocess.run(probe["argv"], cwd=cwd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30, check=False)
            result = {**probe, "exit_code": proc.returncode,
                      "status": "passed" if proc.returncode == 0 else "failed",
                      "output": proc.stdout + proc.stderr}
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = {**probe, "exit_code": None, "status": "environment_error", "output": str(exc)}
        results.append(result)
    return {"schema": "megin-runner-preflight/v1", "ok": all(r["status"] == "passed" for r in results),
            "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--work-id", required=True)
    args = parser.parse_args()
    try:
        from quality_gate import load_contract, WORK_ID_PATTERN, InvalidEvidence
        from repo_workspace import validate_repo, InvalidWorkspace
        if not WORK_ID_PATTERN.fullmatch(args.work_id):
            raise ValueError("invalid Work ID")
        repo = validate_repo(args.repo)
        _, contract, _ = load_contract(repo, args.work_id)
        validate_trace(contract)
        result = preflight(repo, contract)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    except (ValueError, OSError, InvalidEvidence, InvalidWorkspace) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
