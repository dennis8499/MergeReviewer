"""Source-only incremental review cases; expected answers stay parent-owned."""
from __future__ import annotations

from pathlib import Path
import json

from tests.create_quality_fixture import create as legacy_create, git, write

CASES = ("arithmetic-large", "interest-alias-safe", "interest-independent", "public-csv", "required-gap")


def sources(case):
    if case == "arithmetic-large":
        initial = "def total(price, quantity):\n    return price * quantity\n\n"
        for index in range(36):
            initial += (f"# Stable local helper {index:02}: " + "operator diagnostics; " * 6 + "\n"
                        f"def diagnostic_{index:02}(value):\n    local_value = value + {index}\n    return local_value\n\n")
        updated = initial.replace("price * quantity", "price + quantity").replace("local_value", "display_value")
        return {"pricing.py": initial, "README.md": "total returns unit price multiplied by quantity; diagnostic helpers are local.\n"}, {"pricing.py": updated}, {
            "findings": 1, "path": "pricing.py", "line": 2, "priorities": ["P1", "P2"], "complete": True,
            "cause": "total(10, 3) changes from 30 to 13; local diagnostic renames preserve behavior."}
    if case in ("interest-alias-safe", "interest-independent"):
        shared = ("class Ledger:\n    def __init__(self):\n        self._history = {}\n        self._allocations = {}\n\n")
        if case == "interest-alias-safe":
            initial = shared + ("    def post(self, event_id, cents):\n        self._history[event_id] = cents\n"
                "        self._allocations[event_id] = cents\n\n    def total(self):\n        return sum(self._history.values())\n")
            updated = initial.replace("sum(self._history.values())", "sum(self._allocations.values())")
            contract = ("Ledger.post records one interest event in two mirrored stores, once per event_id with identical integer cents. "
                        "Only post writes them; callers must not mutate private stores. Reposting replaces both copies. "
                        "The two stores represent the same economic event. total counts each event once.\n")
            oracle = {"findings": 0, "complete": True, "cause": "Both stores contain the same economic events; substitution preserves total."}
        else:
            initial = shared + ("    def post_history_interest(self, event_id, cents):\n        self._history[event_id] = cents\n\n"
                "    def post_adjustment_interest(self, event_id, cents):\n        self._allocations[event_id] = cents\n\n"
                "    def total(self):\n        return sum(self._history.values()) + sum(self._allocations.values())\n")
            updated = initial.replace("sum(self._history.values()) + sum(self._allocations.values())", "sum(self._allocations.values())")
            contract = ("History interest and adjustment interest are independent economic events. Their IDs are scoped to each category. "
                        "All values are integer cents in the same currency. total must include both categories exactly once. "
                        "Callers use the two post methods; they must not mutate private stores.\n")
            oracle = {"findings": 1, "path": "interest.py", "line": 13, "priorities": ["P1", "P2"], "complete": True,
                      "cause": "Posting history=10 and adjustment=20 changes total from 30 to 20; independent history is lost."}
        return {"interest.py": initial, "README.md": contract}, {"interest.py": updated}, oracle
    if case == "public-csv":
        initial = ("import csv\nimport io\n\ndef export_csv(amounts):\n    stream = io.StringIO(newline='')\n"
                   "    writer = csv.writer(stream)\n    writer.writerow(['amount_cents'])\n"
                   "    for value in amounts:\n        writer.writerow([value])\n    return stream.getvalue()\n")
        contract = ("export_csv is a public library API with no in-repository caller. Its input is a list of nonnegative integer cents. "
                    "The stable CSV contract is a header amount_cents and one exact integer cents value per row. "
                    "Consumers receive the returned string. No currency conversion is part of this API.\n")
        return {"exporter.py": initial, "README.md": contract}, {
            "exporter.py": initial.replace("writer.writerow([value])", "writer.writerow([f'{value / 100:.2f}'])")}, {
            "findings": 1, "path": "exporter.py", "line": 9, "priorities": ["P1", "P2"], "complete": True,
            "cause": "Public CSV contract requires integer cents; input 100 is changed to 1.00 despite amount_cents header."}
    raise ValueError("unknown corpus case")


def assert_behavior(case, base, head):
    """Execute only these hand-created fixtures, never user code or log content."""
    before, after = {}, {}
    path = next(name for name in head if name.endswith(".py"))
    exec(base[path], before)
    exec(head[path], after)
    if case == "arithmetic-large":
        assert (before["total"](10, 3), after["total"](10, 3)) == (30, 13)
        for index in range(36):
            assert before[f"diagnostic_{index:02}"](7) == after[f"diagnostic_{index:02}"](7)
    elif case.startswith("interest-"):
        left, right = before["Ledger"](), after["Ledger"]()
        for ledger in (left, right):
            if case == "interest-alias-safe":
                ledger.post("one", 10)
                ledger.post("two", 20)
                ledger.post("one", 5)
            else:
                ledger.post_history_interest("same-id", 10)
                ledger.post_adjustment_interest("same-id", 20)
        assert (left.total(), right.total()) == ((25, 25) if case == "interest-alias-safe" else (30, 20))
    else:
        assert before["export_csv"]([100]) == "amount_cents\r\n100\r\n"
        assert after["export_csv"]([100]) == "amount_cents\r\n1.00\r\n"


def create(destination, case):
    destination = Path(destination).resolve()
    if case not in CASES or destination.exists():
        raise ValueError("Choose a supported case and a fresh destination")
    if case == "required-gap":
        fixture = legacy_create(destination, "gap")
        oracle = {"findings": 1, "path": "app.py", "line": 2, "priorities": ["P1", "P2"], "complete": False,
                  "cause": "Local checkout raises KeyError; explicitly registered required billing consumer has no captured HEAD."}
    else:
        base, head, oracle = sources(case)
        assert_behavior(case, base, head)
        workspace = destination / "workspace"
        workspace.mkdir(parents=True)
        repo = workspace / "orders"
        git(workspace, "init", "-b", "main", repo)
        for key, value in {"user.name": "Incremental Review", "user.email": "review@example.invalid",
                           "core.autocrlf": "false", "commit.gpgsign": "false"}.items():
            git(repo, "config", key, value)
        for path, text in base.items():
            write(repo, path, text)
        git(repo, "add", ".")
        git(repo, "commit", "-m", "source contract")
        base_sha = git(repo, "rev-parse", "HEAD")
        git(repo, "switch", "-c", "feature/review")
        for path, text in head.items():
            write(repo, path, text)
        git(repo, "add", ".")
        git(repo, "commit", "-m", "review change")
        fixture = {"schema": "IncrementalReviewSource/v1", "source_only": True, "workspace": str(workspace),
                   "project": str(repo), "base": "main", "head": "feature/review", "base_sha": base_sha,
                   "head_sha": git(repo, "rev-parse", "HEAD"), "review_draft": None}
    for name, value in (("source-fixture.json", fixture), ("oracle.json", {"case": case, "visibility": "parent-only", **oracle})):
        (destination / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return fixture
