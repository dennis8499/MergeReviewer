"""Prepare isolated source fixtures for an actual Codex skill review (not a model simulator)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/merge-reviewer/scripts"
sys.path.insert(0, str(SCRIPTS))
import git_review_context as api
import review_session


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            encoding="utf-8", check=False)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def server(fields):
    return ("declare function publish(topic: string, payload: unknown): void;\n"
            "declare function post(route: string, handler: () => unknown): void;\n"
            "export function createOrder() {\n"
            "  const id = 'order-42';\n"
            "  const totalCents = 1299;\n"
            "  publish('orders.created', { id, " + fields + " });\n"
            "  return { id, " + fields + " };\n"
            "}\n"
            "post('/orders', createOrder);\n")


def prepare(case):
    root = review_session.create_session()
    context = None
    try:
        files = {
            "orders-api": {"server.ts": server("totalCents")},
            "web": {"app.ts": "export async function submitOrder() {\n  const order = await fetch('/orders', { method: 'POST' }).then(r => r.json());\n  return (order.totalCents / 100).toFixed(2);\n}\n"},
            "invoice-worker": {"worker.ts": "import { validateOrderCreated, buildSettlement } from '@demo/contracts';\ndeclare function subscribe(topic: string, handler: (payload: unknown) => void): void;\ndeclare function publish(topic: string, payload: unknown): void;\nsubscribe('orders.created', payload => {\n  const order = validateOrderCreated(payload);\n  publish('settlements.ready', buildSettlement(order));\n});\n",
                               "package.json": '{"name":"invoice-worker","dependencies":{"@demo/contracts":"workspace:*"}}\n'},
            "shared-contracts": {"order.ts": "export interface OrderCreated { id: string; totalCents: number }\nexport function validateOrderCreated(payload: any): OrderCreated {\n  if (!Number.isInteger(payload.totalCents)) throw new Error('Missing totalCents');\n  return payload;\n}\nexport function buildSettlement(order: OrderCreated) {\n  return { orderId: order.id, amount: order.totalCents / 100 };\n}\n",
                                 "package.json": '{"name":"@demo/contracts","version":"1.0.0","exports":"./order.ts"}\n'},
            "settlement-worker": {"settlement.ts": "declare function subscribe(topic: string, handler: (payload: any) => void): void;\ndeclare function settle(orderId: string, amount: number): void;\nsubscribe('settlements.ready', payload => settle(payload.orderId, payload.amount));\n"},
            "unrelated-admin": {"users.ts": "export function countUsers(users: string[]) { return users.length; }\n"},
        }
        for name, sources in files.items():
            repo = root / name
            git(root, "init", "-b", "main", str(repo))
            git(repo, "config", "user.name", "Skill Acceptance")
            git(repo, "config", "user.email", "review@example.invalid")
            for relative, content in sources.items():
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "baseline")
        seed = root / "orders-api"
        git(seed, "switch", "-c", "feature/amount")
        fields = "amountCents: totalCents" if case == "regression" else "totalCents, amountCents: totalCents"
        (seed / "server.ts").write_text(server(fields), encoding="utf-8")
        git(seed, "add", "server.ts")
        git(seed, "commit", "-m", "expose amount field")
        args = api.parse_args(["--workspace", str(root), "--scope", "auto", "--target", "branch",
                               "--project", str(seed), "--base", "main", "--head", "feature/amount", "--no-fetch"])
        manifest = api.build_manifest(args)
        context = manifest["context_dir"]
        return {"case": case, "fixture_root": str(root), "context_dir": context,
                "request": f"Use $merge-reviewer at {ROOT / 'skills/merge-reviewer'} to review orders-api main against feature/amount in this Group; follow the feature chain and produce JSON and Markdown.",
                "repositories": [{"id": r["id"], "name": r["name"]} for r in manifest["repositories"]]}
    except BaseException:
        if context:
            review_session.cleanup_session(context)
        review_session.cleanup_session(root)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("regression", "safe"), required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.case), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
