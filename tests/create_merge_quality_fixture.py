"""Create additional source-only MergeReviewer semantic acceptance cases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

if __package__:
    from . import create_quality_fixture as common
else:
    import create_quality_fixture as common

CASES = ("p2", "p3", "outflow", "cross-branch", "security-safe")
P2_BASE = "def price(total, percent):\n    return total - (total * percent // 100)\n"
P2_HEAD = "def price(total, percent):\n    return total - ((total * percent // 100) // 100)\n"
P3_BASE = 'def label(count):\n    return f"{count} orders"\n'
P3_HEAD = 'def label(count):\n    return "0 orders"\n'
OUTFLOW_BASE = 'TOPIC = "invoice.created"\n\ndef event(total_cents):\n    _amount = total_cents\n    return {"amount_cents": _amount}\n'
OUTFLOW_HEAD = OUTFLOW_BASE.replace("_amount = total_cents\n", "_amount = total_cents // 100\n")
CONSUMER = 'TOPIC = "invoice.created"\n\ndef consume(payload):\n    return payload["amount_cents"]\n'
CROSS_BASE = 'def normalize(raw):\n    return raw.strip()\n\ndef valid(value):\n    return value is not None\n\ndef accept(raw):\n    return valid(normalize(raw))\n'
CROSS_MAIN = CROSS_BASE.replace("    return raw.strip()\n", "    return raw.strip() or None\n")
CROSS_HEAD = CROSS_BASE.replace("    return value is not None\n", "    return bool(value.strip())\n")
SECURITY_BASE = 'import hmac\n\ndef verify(token: bytes, expected: bytes):\n    return token == expected\n'
SECURITY_HEAD = SECURITY_BASE.replace("    return token == expected\n", "    return hmac.compare_digest(token, expected)\n")

EXPECTED = {
    "p2": "Discount 10% on 1000 should return 900; the changed calculation returns 999 (P2).",
    "p3": "The order-count label always shows zero even when the supplied count is positive (P3).",
    "outflow": "The private amount is divided by 100 before export; the fixed consumer reads cents (P1).",
    "cross-branch": "Both branches handle empty input, but their merge preview raises AttributeError (P2).",
    "security-safe": "The byte-token check is hardened with compare_digest; do not report the fixed timing weakness as newly introduced.",
}


def create(destination, case):
    destination = Path(destination).resolve()
    if case not in CASES or destination.exists():
        raise ValueError("Choose a supported case and a fresh destination")
    workspace = destination / "workspace"
    workspace.mkdir(parents=True)
    repo = workspace / "orders-service"
    common.git(workspace, "init", "-b", "main", repo)
    for key, value in {"user.name": "Quality Fixture", "user.email": "quality@example.invalid",
                       "core.autocrlf": "false", "commit.gpgsign": "false",
                       "core.hooksPath": str(destination / "no-hooks")}.items():
        common.git(repo, "config", key, value)
    old, new = {"p2": (P2_BASE, P2_HEAD), "p3": (P3_BASE, P3_HEAD),
                "outflow": (OUTFLOW_BASE, OUTFLOW_HEAD), "cross-branch": (CROSS_BASE, CROSS_HEAD),
                "security-safe": (SECURITY_BASE, SECURITY_HEAD)}[case]
    common.write(repo, "app.py", old)
    common.write(repo, "README.md", "The public entry point is in app.py.\n")
    common.git(repo, "add", ".")
    common.git(repo, "commit", "-m", "baseline")
    if case == "cross-branch":
        common.git(repo, "branch", "feature/review")
        common.write(repo, "app.py", CROSS_MAIN)
        common.git(repo, "add", ".")
        common.git(repo, "commit", "-m", "base branch change")
        base = common.git(repo, "rev-parse", "HEAD")
        common.git(repo, "switch", "feature/review")
    else:
        base = common.git(repo, "rev-parse", "HEAD")
        common.git(repo, "switch", "-c", "feature/review")
    common.write(repo, "app.py", new)
    common.git(repo, "add", ".")
    common.git(repo, "commit", "-m", "comparison")
    if case == "outflow":
        consumer = workspace / "billing-consumer"
        common.git(workspace, "init", "-b", "main", consumer)
        for key, value in {"user.name": "Quality Fixture", "user.email": "quality@example.invalid",
                           "core.autocrlf": "false", "commit.gpgsign": "false",
                           "core.hooksPath": str(destination / "no-hooks")}.items():
            common.git(consumer, "config", key, value)
        common.write(consumer, "consumer.py", CONSUMER)
        common.git(consumer, "add", ".")
        common.git(consumer, "commit", "-m", "fixed consumer")
    fixture = {"schema": "ReviewerQualitySource/v1", "source_only": True,
               "workspace": str(workspace), "project": str(repo), "base": "main", "head": "feature/review",
               "base_sha": base, "head_sha": common.git(repo, "rev-parse", "HEAD"), "review_draft": None}
    oracle = {"schema": "ReviewerQualityOracle/v1", "visibility": "parent-only", "case": case,
              "expected": EXPECTED[case], "secret_sentinels": []}
    for name, value in (("source-fixture.json", fixture), ("oracle.json", oracle)):
        (destination / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(create(args.output_root, args.case), ensure_ascii=False))


if __name__ == "__main__":
    main()
