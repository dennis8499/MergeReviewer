"""Create deterministic source-only review workloads, without a review verdict.

The default large fixture has 12 repositories, 1,000 tracked files per repo,
20 changed files per repo, and four producer -> relay -> consumer contracts.
No coverage, findings, or review draft is generated. A real reviewer must author
those; helper replay can then use that already-reviewed draft unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess


DATE = "2026-10-16T00:00:00+00:00"
NAMES = ("orders-api", *(f"relay-{n}" for n in range(1, 5)),
         *(f"consumer-{n}" for n in range(1, 5)), *(f"unrelated-{n}" for n in range(1, 4)))


def git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_DATE": DATE, "GIT_COMMITTER_DATE": DATE}
    process = subprocess.run(["git", "-C", str(repo), *map(str, args)], capture_output=True,
                             encoding="utf-8", env=env, timeout=180)
    if process.returncode:
        raise RuntimeError(process.stderr)
    return process.stdout.strip()


def write(repo, path, text):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def contract_files(name, *, head=False):
    if name == "orders-api":
        field = "total" if head else "amount"
        return {f"src/chain_{n}.py":
                f'TOPIC = "orders.chain{n}"\n\ndef publish(bus, amount):\n'
                f'    return bus.send(TOPIC, {{"{field}": amount}})\n'
                '\ndef bootstrap(bus, amount):\n    return publish(bus, amount)\n' for n in range(1, 5)}
    if name.startswith("relay-"):
        n = name.rsplit("-", 1)[1]
        return {"src/relay.py": f'SOURCE_TOPIC = "orders.chain{n}"\n'
                f'NEXT_TOPIC = "settlement.chain{n}"\n\ndef forward(bus, payload):\n'
                '    return bus.send(NEXT_TOPIC, payload)\n'
                '\ndef register(bus):\n'
                '    return bus.subscribe(SOURCE_TOPIC, lambda payload: forward(bus, payload))\n'
                '\ndef bootstrap(bus):\n    return register(bus)\n'}
    if name.startswith("consumer-"):
        n = name.rsplit("-", 1)[1]
        return {"src/consumer.py": f'TOPIC = "settlement.chain{n}"\n\ndef consume(payload):\n'
                '    return payload["amount"] * 100\n'
                '\ndef register(bus):\n    return bus.subscribe(TOPIC, consume)\n'
                '\ndef bootstrap(bus):\n    return register(bus)\n'}
    return {"src/clock.py": "def clock():\n    return 42\n"}


def create(root, files_per_repo=1000, changed_per_repo=20):
    root = Path(root).resolve()
    if root.exists():
        raise ValueError("Fixture root already exists; choose a fresh directory.")
    if files_per_repo < changed_per_repo or changed_per_repo < 4:
        raise ValueError("Require files_per_repo >= changed_per_repo >= 4.")
    root.mkdir(parents=True)
    records = []
    for name in NAMES:
        repo = root / name
        git(root, "init", "-b", "main", repo)
        for key, value in {"user.name": "Review Benchmark", "user.email": "review@example.invalid",
                           "core.autocrlf": "false", "commit.gpgsign": "false",
                           "core.hooksPath": str(root / "no-hooks")}.items():
            git(repo, "config", key, value)
        sources = contract_files(name)
        for path, text in sources.items():
            write(repo, path, text)
        for n in range(files_per_repo - len(sources)):
            write(repo, f"notes/note-{n:04}.txt", f"{name} reference {n:04}; revision 1\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "fixed baseline")
        base = git(repo, "rev-parse", "main")
        git(repo, "switch", "-c", "feature/amount")
        behavior_changes = contract_files(name, head=True) if name == "orders-api" else {}
        for path, text in behavior_changes.items():
            write(repo, path, text)
        for n in range(changed_per_repo - len(behavior_changes)):
            write(repo, f"notes/note-{n:04}.txt", f"{name} reference {n:04}; revision 2\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "fixed comparison")
        actual_files = len(git(repo, "ls-files").splitlines())
        actual_changes = len(git(repo, "diff", "--name-only", "main", "feature/amount").splitlines())
        if (actual_files, actual_changes) != (files_per_repo, changed_per_repo):
            raise AssertionError("Fixture size differs from its specification.")
        records.append({"name": name, "path": str(repo), "base_sha": base,
                        "head_sha": git(repo, "rev-parse", "HEAD"),
                        "files": actual_files, "changed_files": actual_changes})
    hints = {"repositories": [{"path": record["path"], "defaultBranch": "main"}
                              for record in records if record["name"] != "orders-api"]}
    job = {"schema": "ReviewerSourceFixture/v1", "source_only": True,
           "workspace": str(root), "project": str(root / "orders-api"),
           "base": "main", "head": "feature/amount", "repositories": records,
           "chains": [{"input_topic": f"orders.chain{n}", "relay_topic": f"settlement.chain{n}",
                       "repositories": ["orders-api", f"relay-{n}", f"consumer-{n}"]}
                      for n in range(1, 5)], "workspace_context": hints,
           "review_draft": None,
           "note": "Source workload only. No model verdict, coverage or review draft is synthesized."}
    (root / "source-fixture.json").write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (root / "release-workspace-context.json").write_text(json.dumps(hints, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--files-per-repo", type=int, default=1000)
    parser.add_argument("--changed-per-repo", type=int, default=20)
    parser.add_argument("--source-only", action="store_true", help="Explicitly declare source-only output (the only supported mode).")
    args = parser.parse_args()
    result = create(args.output_root, args.files_per_repo, args.changed_per_repo)
    print(json.dumps({"fixture": str(Path(result["workspace"]) / "source-fixture.json"),
                      "repositories": len(result["repositories"]), "chains": len(result["chains"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
