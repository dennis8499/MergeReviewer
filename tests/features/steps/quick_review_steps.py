from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import sys
from unittest import mock
from pathlib import Path


def remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def make_writable(function, target, _error):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        function(target)

    shutil.rmtree(path, onerror=make_writable)


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout.strip()


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def make_repo(two_remotes: bool = False) -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="merge-reviewer-feature-"))
    repo = root / "repo"
    remote = root / "origin.git"
    run_git(root, "init", "--bare", str(remote))
    run_git(root, "init", "-b", "main", str(repo))
    run_git(repo, "config", "user.email", "test@example.invalid")
    run_git(repo, "config", "user.name", "Merge Reviewer Test")
    write(repo / "base.txt", "base\n")
    write(repo / "deleted.txt", "kept until the scenario deletes it\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "base")
    run_git(repo, "remote", "add", "origin", str(remote))
    run_git(repo, "push", "-u", "origin", "main")
    run_git(root, "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")
    run_git(repo, "switch", "-c", "feature")
    if two_remotes:
        upstream = root / "upstream.git"
        run_git(root, "init", "--bare", str(upstream))
        run_git(root, "--git-dir", str(upstream), "symbolic-ref", "HEAD", "refs/heads/main")
        run_git(repo, "remote", "add", "upstream", str(upstream))
        run_git(repo, "push", "upstream", "main")
    return repo, root


def helper_path() -> Path:
    return Path(__file__).resolve().parents[3] / "skills" / "merge-reviewer" / "scripts" / "git_review_context.py"


def load_helper_module():
    path = helper_path()
    spec = importlib.util.spec_from_file_location("merge_reviewer_feature_helper", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load helper module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_helper(context, *args: str) -> tuple[int, dict | None, str]:
    completed = subprocess.run(
        ["python", str(helper_path()), "--workspace", str(context.repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    payload = json.loads(completed.stdout) if completed.returncode == 0 else None
    return completed.returncode, payload, completed.stderr


def before_scenario(context, _scenario):
    context.repo = None
    context.root = None
    context.context_dirs = []


def after_scenario(context, _scenario):
    for context_dir in getattr(context, "context_dirs", []):
        remove_tree(context_dir)
    if context.root:
        remove_tree(context.root)


def ensure_repo(context, *, two_remotes: bool = False):
    if context.repo is None:
        context.repo, context.root = make_repo(two_remotes=two_remotes)


def step_given_remote(context):
    ensure_repo(context)


def step_given_two_remotes(context):
    context.repo, context.root = make_repo(two_remotes=True)


def step_given_unpushed_commit(context):
    ensure_repo(context)
    write(context.repo / "local.txt", "local\n")
    run_git(context.repo, "add", "local.txt")
    run_git(context.repo, "commit", "-m", "local")


def step_given_remote_and_local_commit(context):
    ensure_repo(context)
    write(context.repo / "local.txt", "local\n")
    run_git(context.repo, "add", "local.txt")
    run_git(context.repo, "commit", "-m", "local")


def step_given_remote_unrelated_commit(context):
    ensure_repo(context)
    clone = context.root / "remote-work"
    run_git(context.root, "clone", str(context.root / "origin.git"), str(clone))
    run_git(clone, "config", "user.email", "test@example.invalid")
    run_git(clone, "config", "user.name", "Merge Reviewer Test")
    write(clone / "remote.txt", "remote\n")
    run_git(clone, "add", ".")
    run_git(clone, "commit", "-m", "remote")
    run_git(clone, "push", "origin", "main")
    run_git(context.repo, "fetch", "origin", "main")
    write(context.repo / "local.txt", "local\n")
    run_git(context.repo, "add", "local.txt")
    run_git(context.repo, "commit", "-m", "local")


def step_run_without_fetch(context):
    context.returncode, context.payload, context.stderr = run_helper(context, "--quick", "--no-fetch", "--format", "json")


def step_run_without_remote(context):
    context.returncode, context.payload, context.stderr = run_helper(context, "--quick", "--no-fetch", "--format", "json")


def step_run_with_remote(context, remote):
    context.returncode, context.payload, context.stderr = run_helper(context, "--quick", "--remote", remote, "--no-fetch", "--format", "json")


def step_run_working_tree(context):
    before = run_git(context.repo, "status", "--porcelain=v1", "--untracked-files=all")
    context.index_before = run_git(context.repo, "rev-parse", "--git-path", "index")
    index_path = Path(context.index_before)
    if not index_path.is_absolute():
        index_path = context.repo / index_path
    context.index_digest_before = index_path.read_bytes()
    context.returncode, context.payload, context.stderr = run_helper(context, "--quick", "--no-fetch", "--include-working-tree", "--format", "json")
    context.status_before = before
    context.index_path = index_path
    context.context_dirs = getattr(context, "context_dirs", [])
    if context.payload and context.payload.get("context_dir"):
        context.context_dirs.append(Path(context.payload["context_dir"]).parent)


def step_run_with_base(context, base):
    context.returncode, context.payload, context.stderr = run_helper(
        context, "--quick", "--base", base, "--no-fetch", "--format", "json"
    )


def step_prepare_failure(context, failure):
    ensure_repo(context)
    context.failure_kind = failure
    context.failure_context_dir = context.root / "failure-context"
    if failure == "fetch":
        run_git(context.repo, "remote", "set-url", "origin", str(context.root / "missing-origin.git"))
    elif failure == "ambiguous ref":
        upstream = context.root / "upstream.git"
        run_git(context.root, "init", "--bare", str(upstream))
        run_git(context.root, "--git-dir", str(upstream), "symbolic-ref", "HEAD", "refs/heads/main")
        run_git(context.repo, "remote", "add", "upstream", str(upstream))
        for remote in ("origin", "upstream"):
            run_git(context.repo, "push", remote, "HEAD:refs/heads/release")
            run_git(context.repo, "fetch", remote, "release")
    elif failure == "conflict":
        clone = context.root / "conflict-remote-work"
        run_git(context.root, "clone", str(context.root / "origin.git"), str(clone))
        run_git(clone, "config", "user.email", "test@example.invalid")
        run_git(clone, "config", "user.name", "Merge Reviewer Test")
        write(clone / "base.txt", "remote conflict\n")
        run_git(clone, "add", "base.txt")
        run_git(clone, "commit", "-m", "remote conflict")
        run_git(clone, "push", "origin", "main")
        run_git(context.repo, "fetch", "origin", "main")
        write(context.repo / "base.txt", "local conflict\n")
        run_git(context.repo, "add", "base.txt")
        run_git(context.repo, "commit", "-m", "local conflict")
        run_git(context.repo, "merge", "--no-edit", "origin/main", check=False)
    elif failure == "criss-cross":
        write(context.repo / "a.txt", "a\n")
        run_git(context.repo, "add", "a.txt")
        run_git(context.repo, "commit", "-m", "A1")
        a1 = run_git(context.repo, "rev-parse", "HEAD")
        run_git(context.repo, "branch", "side-b", "HEAD~1")
        run_git(context.repo, "switch", "side-b")
        write(context.repo / "b.txt", "b\n")
        run_git(context.repo, "add", "b.txt")
        run_git(context.repo, "commit", "-m", "B1")
        run_git(context.repo, "switch", "feature")
        run_git(context.repo, "merge", "--no-ff", "side-b", "-m", "A2")
        run_git(context.repo, "switch", "side-b")
        run_git(context.repo, "merge", "--no-ff", a1, "-m", "B2")
    elif failure == "unrelated":
        run_git(context.repo, "switch", "--orphan", "unrelated")
        run_git(context.repo, "rm", "-rf", ".")
        write(context.repo / "unrelated.txt", "unrelated\n")
        run_git(context.repo, "add", "-A")
        run_git(context.repo, "commit", "-m", "unrelated root")
        run_git(context.repo, "switch", "feature")
    elif failure == "shallow":
        shallow = context.root / "shallow-repo"
        run_git(context.root, "clone", "--depth", "1", "--no-local", str(context.root / "origin.git"), str(shallow))
        run_git(shallow, "switch", "-c", "feature")
        context.repo = shallow
    elif failure == "snapshot race":
        pass
    else:
        raise AssertionError(f"unknown failure fixture: {failure}")


def step_run_failure(context):
    context.failure_context_dir = getattr(context, "failure_context_dir", context.root / "failure-context")
    if context.failure_kind == "fetch":
        context.returncode, context.payload, context.stderr = run_helper(
            context,
            "--quick",
            "--base",
            "origin/main",
            "--include-working-tree",
            "--context-dir",
            str(context.failure_context_dir),
            "--format",
            "json",
        )
        return
    if context.failure_kind == "ambiguous ref":
        context.returncode, context.payload, context.stderr = run_helper(
            context,
            "--quick",
            "--base",
            "release",
            "--include-working-tree",
            "--no-fetch",
            "--context-dir",
            str(context.failure_context_dir),
            "--format",
            "json",
        )
        return
    if context.failure_kind == "conflict":
        context.returncode, context.payload, context.stderr = run_helper(
            context,
            "--quick",
            "--no-fetch",
            "--include-working-tree",
            "--context-dir",
            str(context.failure_context_dir),
            "--format",
            "json",
        )
        return
    if context.failure_kind == "criss-cross":
        context.returncode, context.payload, context.stderr = run_helper(
            context,
            "--quick",
            "--base",
            "feature",
            "--no-fetch",
            "--include-working-tree",
            "--context-dir",
            str(context.failure_context_dir),
            "--format",
            "json",
        )
        return
    if context.failure_kind == "unrelated":
        context.returncode, context.payload, context.stderr = run_helper(
            context,
            "--quick",
            "--base",
            "unrelated",
            "--no-fetch",
            "--include-working-tree",
            "--context-dir",
            str(context.failure_context_dir),
            "--format",
            "json",
        )
        return
    if context.failure_kind == "shallow":
        context.returncode, context.payload, context.stderr = run_helper(
            context,
            "--quick",
            "--no-fetch",
            "--include-working-tree",
            "--context-dir",
            str(context.failure_context_dir),
            "--format",
            "json",
        )
        return

    helper = load_helper_module()
    args = helper.parse_args(
        [
            "--workspace",
            str(context.repo),
            "--quick",
            "--no-fetch",
            "--include-working-tree",
            "--format",
            "json",
        ]
    )
    before = helper.snapshot(context.repo)
    after = dict(before)
    after["status"] = "race detected\n"
    context.snapshot_temp = context.root / "race-snapshot"
    context.snapshot_temp.mkdir()
    tree_sha = run_git(context.repo, "rev-parse", "HEAD^{tree}")
    try:
        with mock.patch.object(helper, "snapshot", side_effect=[before, after]), mock.patch.object(
            helper,
            "create_worktree_snapshot",
            return_value=(context.snapshot_temp, {}, tree_sha),
        ):
            helper.build_manifest(args)
    except helper.ReviewContextError as exc:
        context.returncode, context.payload, context.stderr = 2, None, str(exc)
    else:
        context.returncode, context.payload, context.stderr = 0, None, ""


def step_run_direct(context):
    context.returncode, context.payload, context.stderr = run_helper(
        context, "--base", "origin/main", "--head", "HEAD", "--mode", "direct", "--no-fetch", "--format", "json"
    )


def step_run_both(context):
    context.first_result = run_helper(context, "--quick", "--no-fetch", "--format", "json")
    context.second_result = run_helper(
        context, "--quick", "--no-fetch", "--include-working-tree", "--format", "json"
    )
    for result in (context.first_result, context.second_result):
        if result[1] and result[1].get("context_dir"):
            context.context_dirs = getattr(context, "context_dirs", [])
            context.context_dirs.append(Path(result[1]["context_dir"]).parent)


def step_working_changes(context):
    ensure_repo(context)
    write(context.repo / "staged.txt", "staged\n")
    run_git(context.repo, "add", "staged.txt")
    write(context.repo / "staged.txt", "staged changed\n")
    write(context.repo / "deleted.txt", "deleted\n")
    run_git(context.repo, "add", "deleted.txt")
    (context.repo / "deleted.txt").unlink()
    write(context.repo / "untracked.txt", "untracked\n")


def step_result_uses_refs(context):
    assert context.returncode == 0, context.stderr
    assert context.payload["quick"] is True
    assert context.payload["base_input"] == "origin/main"
    assert context.payload["head_input"] == "HEAD"


def step_local_change(context):
    assert any(item["path"] == "local.txt" for item in context.payload["changed_files"])


def step_candidates(context):
    assert context.returncode != 0
    assert "origin" in context.stderr and "upstream" in context.stderr


def step_selected_remote(context, remote):
    assert context.returncode == 0, context.stderr
    assert context.payload["remote"] == remote


def step_only_local(context):
    assert context.returncode == 0, context.stderr
    paths = {item["path"] for item in context.payload["changed_files"]}
    assert paths == {"local.txt"}, paths


def step_scope(context, scope):
    assert context.returncode == 0, context.stderr
    assert context.payload["review_scope"] == scope


def step_index_unchanged(context):
    assert context.payload["working_tree_unchanged"] is True
    assert run_git(context.repo, "status", "--porcelain=v1", "--untracked-files=all") == context.status_before
    assert context.index_path.read_bytes() == context.index_digest_before


def step_context_bundle(context):
    assert context.returncode == 0, context.stderr
    context_dir = Path(context.payload["context_dir"])
    assert (context_dir / "working-tree.patch").exists()
    assert (context_dir / "manifest.json").exists()


def step_ref_error(context):
    assert context.returncode != 0
    assert "找不到基礎 ref" in context.stderr


def step_failure_stopped(context):
    assert context.returncode != 0
    assert context.payload is None


def step_no_failure_context(context):
    assert not context.failure_context_dir.exists()
    if hasattr(context, "snapshot_temp"):
        assert not context.snapshot_temp.exists()


def step_direct_result(context):
    assert context.returncode == 0, context.stderr
    assert context.payload["mode"] == "direct"
    assert context.payload["review_scope"] == "committed"


def step_both_successful(context):
    first_code, first_payload, first_error = context.first_result
    second_code, second_payload, second_error = context.second_result
    assert first_code == 0, first_error
    assert second_code == 0, second_error
    assert first_payload["review_scope"] == "committed"
    assert second_payload["review_scope"] == "working-tree"


from behave import given, then, when

given("a local repository with a remote default branch")(step_given_remote)
given("a local repository with two remotes")(step_given_two_remotes)
given("the current branch has an unpushed commit")(step_given_unpushed_commit)
given("a local repository whose remote default branch has an unrelated commit")(step_given_remote_unrelated_commit)
given("the current branch has a local commit")(step_given_remote_and_local_commit)
given("the working tree has staged, unstaged, deleted, and untracked files")(step_working_changes)
given('the repository is prepared for quick-review failure "{failure}"')(step_prepare_failure)
when("I run the quick review without fetching")(step_run_without_fetch)
when("I run the quick review without choosing a remote")(step_run_without_remote)
when('I run the quick review with remote "{remote}"')(step_run_with_remote)
when("I run the quick review with working-tree changes")(step_run_working_tree)
when('I run the quick review with base "{base}"')(step_run_with_base)
when("I run the failing quick review")(step_run_failure)
when("I run an explicit direct comparison")(step_run_direct)
when("I run both quick-review forms")(step_run_both)
then("the result uses the current branch HEAD and the remote default branch")(step_result_uses_refs)
then("the local commit appears in the changed files")(step_local_change)
then("the quick review reports the remote candidates")(step_candidates)
then('the result records remote "{remote}"')(step_selected_remote)
then("the result contains only the local branch changes")(step_only_local)
then('the result scope is "{scope}"')(step_scope)
then("the original working tree and index are unchanged")(step_index_unchanged)
then("the context bundle contains the fixed patch")(step_context_bundle)
then("the quick review is incomplete with a ref error")(step_ref_error)
then("the quick review stops without a successful result")(step_failure_stopped)
then("no failure context is written")(step_no_failure_context)
then("the result is a direct committed review")(step_direct_result)
then("both quick-review results are successful")(step_both_successful)
