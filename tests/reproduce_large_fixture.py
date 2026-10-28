"""Reproduce the authored four-chain finding on a generated fixed fixture."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

import create_benchmark_fixture as fixture


class Bus:
    def __init__(self):
        self.handlers = {}

    def subscribe(self, topic, handler):
        self.handlers[topic] = handler

    def send(self, topic, payload):
        return self.handlers[topic](payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", type=Path, required=True)
    root = parser.parse_args().fixture_root.resolve()
    job = json.loads((root / "source-fixture.json").read_text(encoding="utf-8"))
    records = {item["name"]: item for item in job["repositories"]}

    def source(name, version, path, expected):
        record = records[name]
        revision = record[version]
        if not isinstance(revision, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
            raise ValueError(f"Invalid fixed commit identity for {name}")
        code = subprocess.run(["git", "-C", str(root / name), "show", f"{revision}:{path}"],
                              check=True, capture_output=True, encoding="utf-8").stdout
        if code != expected:
            raise ValueError(f"Fixture source does not match its known template: {name}/{path}")
        return code

    def load(code, filename):
        namespace = {}
        exec(compile(code, filename, "exec"), namespace)
        return namespace

    results = []
    for n in range(1, 5):
        path, relay_name, consumer_name = f"src/chain_{n}.py", f"relay-{n}", f"consumer-{n}"
        old = load(source("orders-api", "base_sha", path, fixture.contract_files("orders-api")[path]), path)
        new = load(source("orders-api", "head_sha", path, fixture.contract_files("orders-api", head=True)[path]), path)
        relay = load(source(relay_name, "head_sha", "src/relay.py", fixture.contract_files(relay_name)["src/relay.py"]), "relay.py")
        consumer = load(source(consumer_name, "head_sha", "src/consumer.py", fixture.contract_files(consumer_name)["src/consumer.py"]), "consumer.py")
        bus = Bus()
        relay["register"](bus)
        consumer["register"](bus)
        if old["publish"](bus, 123) != 12300:
            raise AssertionError(f"Unexpected baseline result in chain {n}")
        try:
            new["publish"](bus, 123)
        except KeyError as error:
            if error.args != ("amount",):
                raise
        else:
            raise AssertionError(f"Chain {n} did not expose the changed event field")
        results.append({"chain": n, "baseline": 12300, "head": "KeyError: amount"})
    for n in range(16):
        path = f"notes/note-{n:04}.txt"
        source("orders-api", "base_sha", path, f"orders-api reference {n:04}; revision 1\n")
        source("orders-api", "head_sha", path, f"orders-api reference {n:04}; revision 2\n")
    print(json.dumps({"chain_execution": results, "reviewed_note_changes": 16,
                      "scope": "fixed synthetic fixture; no external application tests"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
