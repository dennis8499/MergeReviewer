"""The published snippets run in separate Python processes, as actual tools do."""
from pathlib import Path
import json
import re
import tempfile
import unittest

from tests.fixtures import SCRIPTS
from tests.support import run_python
import review_paging as paging


class PagingExamplesTests(unittest.TestCase):
    def test_documented_creation_and_confirmation_work_in_independent_processes(self):
        text = (SCRIPTS.parent / "references" / "paging-workflow.md").read_text(encoding="utf-8")
        snippets = re.findall(r"~~~python\n(.*?)\n~~~", text, re.S)
        self.assertEqual(2, len(snippets))
        with tempfile.TemporaryDirectory(prefix="paging-example-") as directory:
            root = Path(directory)
            start = {"runtime_root": str(root / "runtime"), "request_id": "a" * 32}
            link = {"command": "review_session.py view", "runtime_root": start["runtime_root"],
                    "request_id": start["request_id"], "artifact": "b" * 64,
                    "artifact_scope": "context", "pointer": "/lines", "offset": 0, "page_size": 70}
            (root / "start.json").write_text(json.dumps(start), encoding="utf-8")
            (root / "page-response.json").write_text(json.dumps({"continue": link}), encoding="utf-8")
            # Absolute data paths keep each example independent of test cwd.
            for index, snippet in enumerate(snippets):
                snippet = snippet.replace('"<skill-dir>/scripts"', repr(str(SCRIPTS)))
                for name in ("start.json", "page-response.json", "paging.json", "batch-response.json", "confirmed-paging.json"):
                    snippet = snippet.replace('"' + name + '"', repr(str(root / name)))
                script = root / ("example_" + str(index) + ".py")
                script.write_text(snippet, encoding="utf-8")
                if index:
                    state = json.loads((root / "paging.json").read_text(encoding="utf-8"))
                    def reader(*args, **kwargs):
                        return {"artifact": "b" * 64, "pointer": "/lines", "scope": "context",
                                "items": [{"line": 1, "text": "return amount * quantity"}], "view_complete": True}, "context"
                    batch = paging.execute(start["runtime_root"], start["request_id"], state, reader)
                    (root / "batch-response.json").write_text(json.dumps(batch), encoding="utf-8")
                result = run_python(script)
                self.assertEqual(0, result.returncode, result.stderr)
            confirmed = json.loads((root / "confirmed-paging.json").read_text(encoding="utf-8"))
            self.assertEqual([], confirmed["pending"])
            self.assertEqual([], confirmed["blocked"])
            self.assertEqual(1, len(confirmed["acknowledged"]))


if __name__ == "__main__":
    unittest.main()
