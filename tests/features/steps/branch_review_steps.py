from __future__ import annotations
import json
import re
from pathlib import Path
import shlex

from behave import given, then, when
from tests.fixtures import ReviewFixture, api, workspace, review_report, review_session
from tests.support import run_python, run_git as git


@given("a branch fixture with {count:d} visible Repos")
def fixture(context, count):
    context.branch_fixture = ReviewFixture()
    context.branch_fixture.setUp()
    context.branch_fixture.commit()
    for index in range(1, count):
        context.branch_fixture.make_repo(f"consumer-{index}")
    git(context.branch_fixture.repo, "tag", "v1")
    context.extra_args = []
    context.base = "main"
    context.head = "feature"


@given("a workspace file declaring only the selected Repo")
def workspace_file(context):
    path = context.branch_fixture.root / "only.code-workspace"
    path.write_text(json.dumps({"folders": [{"path": "api"}]}), encoding="utf-8")
    context.extra_args = ["--workspace-file", str(path)]


def prepare(context, extra=()):
    fixture = context.branch_fixture
    result = run_python(Path(api.__file__), "--workspace", str(fixture.root), "--project", str(fixture.repo),
                        "--base", context.base, "--head", context.head, *context.extra_args, *extra)
    context.preparation = result
    context.manifest = json.loads(result.stdout) if result.returncode == 0 else None
    if context.manifest:
        owned = Path(context.manifest["context_dir"])
        fixture.addCleanup(review_session.cleanup_session, owned)


@when("I prepare the explicit base and comparison branches")
def prepare_branches(context):
    prepare(context)


@when('I prepare branches with the removed argument "{argument}"')
def removed_argument(context, argument):
    prepare(context, shlex.split(argument))


@when('I use comparison input "{input}"')
def comparison_input(context, input):
    context.head = input
    prepare(context)


@then("the preparation succeeds with 1 selected Repo")
def one_seed(context):
    assert context.preparation.returncode == 0, context.preparation.stderr
    assert context.manifest["seed_repos"] == [context.manifest["selected_repo"]]
    assert context.manifest["schema"] == workspace.CONTEXT


@then("cross Repo extension is disabled")
def disabled(context):
    assert context.manifest["extension_enabled"] is False


@then("cross Repo extension is enabled")
def enabled(context):
    assert context.manifest["extension_enabled"] is True


@then("preparation is rejected")
def rejected(context):
    assert context.preparation.returncode != 0
    assert context.manifest is None


@when("I publish the completed evidence fixture")
def publish(context):
    fixture = context.branch_fixture
    owned = Path(context.manifest["context_dir"])
    draft = fixture.draft(owned, context.manifest)
    (owned / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
    context.markdown = review_report.publish_report(owned, owned / "draft.json")


@then("the report is in the selected Repo")
def selected_output(context):
    assert context.markdown.parent == context.branch_fixture.repo / "review-reports"
    assert context.markdown.read_text(encoding="utf-8").startswith("# Merge Report: api")


@then("the filename contains the timestamp and branch commits")
def named_commits(context):
    seed = workspace.repository(context.manifest, context.manifest["selected_repo"])
    base, head = seed["sources"]["base"]["ref"], seed["sources"]["head"]["ref"]
    assert re.fullmatch(rf"merge-review-\d{{8}}T\d{{12}}Z-{base}-{head}\.md", context.markdown.name)


@then("the report is the only published file")
def single_file(context):
    assert list(context.markdown.parent.iterdir()) == [context.markdown]
    assert "問題數量：P0 0／P1 0／P2 0／P3 0" in context.markdown.read_text(encoding="utf-8")


@then("the owned context has been removed")
def cleaned(context):
    assert not Path(context.manifest["context_dir"]).exists()
