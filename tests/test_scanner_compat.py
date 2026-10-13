"""Python 3.10 scanner behavior without any installed TOML dependency."""
from __future__ import annotations

from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/merge-reviewer/scripts"


class ScannerCompatibilityTests(unittest.TestCase):
    def run_scanner(self, directory, root, operation):
        # Block the standard-library module even on newer test interpreters.
        code = ("import sys; from pathlib import Path; "
                "sys.modules['tomllib'] = None; sys.path.insert(0, sys.argv[1]); "
                "import project_scanner as scanner; root = Path(sys.argv[2]); " + operation)
        return subprocess.run([sys.executable, "-X", "utf8", "-I", "-B", "-c", code, str(directory), str(root)],
                              capture_output=True, text=True, encoding="utf-8")

    def test_both_scanners_support_defaults_and_toml_without_stdlib_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in (SCRIPTS, SCRIPTS / "receipt_runtime"):
                with self.subTest(scanner=directory.name):
                    config = root / "notebooklm.toml"
                    config.unlink(missing_ok=True)
                    result = self.run_scanner(directory, root, "assert scanner.cli_settings(root, 'target').exclude_paths == ()")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    config.write_text('[notebooklm]\noutput_directory = "reports"\nexclude_paths = ["skip"]\n', encoding="utf-8")
                    result = self.run_scanner(directory, root,
                                              "settings = scanner.cli_settings(root, 'target'); "
                                              "assert settings.output_directory == 'reports'; "
                                              "assert settings.exclude_paths == ('skip',)")
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_both_scanners_reject_malformed_toml_without_stdlib_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "notebooklm.toml").write_text('[notebooklm]\nexclude_paths = [\n', encoding="utf-8")
            for directory in (SCRIPTS, SCRIPTS / "receipt_runtime"):
                with self.subTest(scanner=directory.name):
                    result = self.run_scanner(directory, root, "scanner.cli_settings(root, 'target')")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unable to read config", result.stderr)
                    self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_isolated_verifier_rejects_modified_bundled_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            shutil.copytree(SCRIPTS / "receipt_runtime", runtime,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            parser = runtime / "tomli/_parser.py"
            parser.write_bytes(parser.read_bytes() + b"\n# altered parser\n")
            result = subprocess.run([sys.executable, "-X", "utf8", "-I", "-B", str(runtime / "verify.py"),
                                     temporary, "unused-work", "repo"], capture_output=True, text=True, encoding="utf-8")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bundled TOML parser changed", result.stderr)
