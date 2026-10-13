"""Isolated, observational native Megin historical receipt verifier."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
runtime = Path(__file__).resolve().parent


def main():
    provenance = json.loads((runtime / "source-manifest.json").read_text(encoding="utf-8"))
    for name, digest in provenance["files"].items():
        if Path(name).name != name or not name.endswith(".py"):
            raise ValueError("invalid bundled receipt runtime manifest")
        if hashlib.sha256((runtime / name).read_bytes()).hexdigest() != digest:
            raise ValueError("bundled receipt runtime changed: " + name)
    sys.path.insert(0, str(runtime))
    import unified_workflow
    root, work, mode = sys.argv[1:]
    if mode not in ("repo", "group"):
        raise ValueError("invalid receipt mode")
    print(json.dumps(unified_workflow.export_receipt(Path(root), work, mode == "group"),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
