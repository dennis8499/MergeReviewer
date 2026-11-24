"""The added quality oracles describe reproducible source behavior."""
import json
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import tempfile
import unittest
from unittest import mock

from tests import create_merge_quality_fixture as fixtures, skill_acceptance
from tests.support import run_python

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/merge-reviewer/scripts"))
import git_review_context as api
import review_session
import review_workspace as workspace


DEPENDENCY_CASES = (("dependency-httpx-regression", "0.36.3"), ("dependency-httpx-safe", "0.37.2"))


def namespace(text):
    result = {}
    exec(compile(text, "<fixed-quality-fixture>", "exec"), result)
    return result


class MergeQualityFixtureTests(unittest.TestCase):
    def prepare_dependency_fixture(self, case):
        if case == "dependency-httpx-regression":
            source = skill_acceptance.prepare(case)
        else:
            completed = run_python(Path(skill_acceptance.__file__), "--case", case,
                                   env={"PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(0, completed.returncode, completed.stderr)
            source = json.loads(completed.stdout)
        self.addCleanup(review_session.cleanup_session, source["fixture_root"])
        return source

    def assert_dependency_history(self, repo, starlette):
        git = fixtures.common.git
        self.assertTrue((repo / ".git").is_dir())
        self.assertEqual(repo / ".git", Path(git(repo, "rev-parse", "--absolute-git-dir")).resolve())
        self.assertEqual(repo, Path(git(repo, "rev-parse", "--show-toplevel")).resolve())
        self.assertEqual("httpx==0.27.2\nstarlette==0.36.3", git(repo, "show", "main:requirements.txt"))
        self.assertEqual(f"httpx==0.28.0\nstarlette=={starlette}", git(repo, "show", "feature/review:requirements.txt"))
        self.assertEqual(git(repo, "rev-parse", "main"), git(repo, "rev-parse", "feature/review^"))

    def test_dependency_cases_ignore_host_git_signing_and_hooks(self):
        git = fixtures.common.git
        with tempfile.TemporaryDirectory(prefix="dependency-fixture-config-") as temporary:
            root = Path(temporary)
            host_config = root / "gitconfig"
            hooks = root / "hooks"
            hooks.mkdir()
            hook = hooks / "pre-commit"
            hook.write_text("#!/bin/sh\nprintf 'ran\\n' > .unexpected-hook\n", encoding="utf-8", newline="\n")
            hook.chmod(0o755)
            for key, value in (("commit.gpgsign", "true"), ("gpg.program", (root / "missing-gpg").as_posix()),
                               ("core.hooksPath", hooks.as_posix())):
                git(root, "config", "--file", host_config, key, value)

            for case, starlette in DEPENDENCY_CASES:
                with self.subTest(case=case):
                    with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(host_config)}):
                        source = self.prepare_dependency_fixture(case)
                    repo = Path(source["project"])
                    self.assertFalse((repo / ".unexpected-hook").exists())
                    self.assertEqual("false", git(repo, "config", "--local", "--get", "commit.gpgsign"))
                    local_hooks = git(repo, "config", "--local", "--get", "core.hooksPath")
                    self.assertEqual(Path(source["fixture_root"]) / "no-hooks", Path(local_hooks).resolve())
                    self.assert_dependency_history(repo, starlette)

    def test_dependency_cases_ignore_inherited_git_repository_environment(self):
        git = fixtures.common.git
        clean_env = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}

        def caller_state(repo):
            git_dir = repo / ".git"
            return {
                "metadata": {name: (git_dir / name).read_bytes() for name in ("HEAD", "config", "index")},
                "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
                "worktree": {path.relative_to(repo).as_posix(): path.read_bytes()
                             for path in repo.rglob("*") if not path.is_relative_to(git_dir) and path.is_file()},
            }

        with tempfile.TemporaryDirectory(prefix="dependency-repo-env-") as temporary, \
             mock.patch.dict(os.environ, clean_env, clear=True):
            for case, starlette in DEPENDENCY_CASES:
                with self.subTest(case=case):
                    caller_source = fixtures.common.create(Path(temporary) / case, "safe")
                    caller = Path(caller_source["project"])
                    git(caller, "switch", "main")
                    fixtures.common.write(caller, "caller.txt", "staged caller change\n")
                    git(caller, "add", "caller.txt")
                    fixtures.common.write(caller, "caller.txt", "unstaged caller change\n")
                    before = caller_state(caller)
                    inherited = {"GIT_DIR": str(caller / ".git"), "GIT_WORK_TREE": str(caller),
                                 "GIT_INDEX_FILE": str(caller / ".git" / "index")}
                    try:
                        with mock.patch.dict(os.environ, inherited):
                            source = self.prepare_dependency_fixture(case)
                    finally:
                        self.assertEqual(before, caller_state(caller), "fixture changed the caller repository")
                    self.assert_dependency_history(Path(source["project"]), starlette)

    def test_cross_branch_defect_is_in_the_actual_frozen_merge_preview(self):
        with tempfile.TemporaryDirectory(prefix="merge-quality-preview-") as temporary:
            root = Path(temporary)
            source = fixtures.create(root / "case", "cross-branch")
            with mock.patch.dict(os.environ, {"GITLAB_WORKSPACE_RUNTIME_DIR": str(root / "network")}):
                manifest = api.build_manifest(api.parse_args(["--workspace", source["workspace"],
                    "--project", source["project"], "--base", "main", "--head", "feature/review", "--no-fetch"]))
            context = Path(manifest["context_dir"])
            try:
                seed = workspace.repository(manifest, manifest["selected_repo"])
                self.assertIn("merge-preview", seed["sources"])
                merged = namespace(workspace.fixed_bytes(context, seed, "merge-preview", "app.py", api).decode("utf-8"))
                with self.assertRaises(AttributeError):
                    merged["accept"]("  ")
            finally:
                review_session.cleanup_session(context)

    def test_added_cases_preserve_real_regressions_and_parent_only_oracles(self):
        with tempfile.TemporaryDirectory(prefix="merge-quality-fixture-") as temporary:
            root = Path(temporary)
            for case in fixtures.CASES:
                with self.subTest(case=case):
                    source = fixtures.create(root / case, case)
                    repo = Path(source["project"])
                    old = namespace(fixtures.common.git(repo, "show", "main:app.py"))
                    new = namespace(fixtures.common.git(repo, "show", "feature/review:app.py"))
                    self.assertTrue((root / case / "oracle.json").is_file())
                    self.assertFalse((Path(source["workspace"]) / "oracle.json").exists())
                    if case == "p2":
                        self.assertEqual(900, old["price"](1000, 10))
                        self.assertEqual(999, new["price"](1000, 10))
                    elif case == "p3":
                        self.assertEqual("3 orders", old["label"](3))
                        self.assertEqual("0 orders", new["label"](3))
                    elif case == "outflow":
                        consumer = namespace((Path(source["workspace"]) / "billing-consumer/consumer.py").read_text(encoding="utf-8"))
                        self.assertEqual("invoice.created", consumer["TOPIC"])
                        self.assertEqual(500, consumer["consume"](old["event"](500)))
                        self.assertEqual(5, consumer["consume"](new["event"](500)))
                    elif case == "security-safe":
                        self.assertTrue(old["verify"](b"match", b"match"))
                        self.assertTrue(new["verify"](b"match", b"match"))
                        self.assertFalse(new["verify"](b"wrong", b"match"))
                        self.assertIn("hmac.compare_digest", fixtures.SECURITY_HEAD)
                    elif case == "cross-branch":
                        self.assertFalse(old["accept"]("  "))
                        self.assertFalse(new["accept"]("  "))
                        tree = fixtures.common.git(repo, "merge-tree", "--write-tree", "main", "feature/review").splitlines()[0]
                        preview = namespace(fixtures.common.git(repo, "show", f"{tree}:app.py"))
                        with self.assertRaises(AttributeError):
                            preview["accept"]("  ")
                    else:
                        consumer = namespace((Path(source["workspace"]) / "billing-consumer/consumer.py").read_text(encoding="utf-8"))
                        self.assertEqual(500, consumer["consume"](SimpleNamespace(**old), {"amount_cents": 500}))
                        self.assertNotIn("legacy_total", new)
                        self.assertIn("total", new)
                        if case == "deleted-api":
                            with self.assertRaises(AttributeError):
                                consumer["consume"](SimpleNamespace(**new), {"amount_cents": 500})
                        else:
                            self.assertEqual(500, consumer["consume"](SimpleNamespace(**new), {"amount_cents": 500}))


if __name__ == "__main__":
    unittest.main()
