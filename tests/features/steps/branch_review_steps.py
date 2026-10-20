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


@when("I publish a finding from the frozen evidence")
def publish_finding(context):
    fixture = context.branch_fixture
    owned = Path(context.manifest["context_dir"])
    draft = fixture.draft(owned, context.manifest)
    context.finding = fixture.finding(draft)
    draft["findings"] = [context.finding]
    (fixture.repo / "app.py").write_text("dirty_worktree = True\n", encoding="utf-8")
    (owned / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
    context.markdown = review_report.publish_report(owned, owned / "draft.json")


@then("the report is in the selected Repo")
def selected_output(context):
    assert context.markdown.parent == context.branch_fixture.repo / "review-reports"
    assert context.markdown.read_text(encoding="utf-8").startswith("# 合併審查: api")


@then("the filename contains the timestamp and 8 character branch commits")
def named_commits(context):
    seed = workspace.repository(context.manifest, context.manifest["selected_repo"])
    base, head = seed["sources"]["base"]["ref"], seed["sources"]["head"]["ref"]
    assert re.fullmatch(rf"merge-review-\d{{8}}T\d{{12}}Z-{base[:8]}-{head[:8]}\.md", context.markdown.name)


@then("the report identifies the captured branch comparison")
def comparison_details(context):
    seed = workspace.repository(context.manifest, context.manifest["selected_repo"])
    native = seed["native"]
    text = context.markdown.read_text(encoding="utf-8")
    comparison = text.split("### 比較資訊\n", 1)[1].split("\n### ", 1)[0]
    assert f"- Repo：`{seed['name']}`" in comparison
    assert f"- 本機路徑：`{seed['path']}`" in comparison
    for role, label in (("base", "基礎分支"), ("head", "比較分支")):
        commit = seed["sources"][role]["ref"]
        assert f"| {label} | `{native[role + '_input']}` | `{native[role + '_resolved_ref']}` | `{commit[:8]}` |" in comparison
    assert f"- 合併方向：`{native['head_input']}` → `{native['base_input']}`" in comparison
    assert f"- 主要差異範圍（共同祖先 → 比較分支）：`{native['diff_base'][:8]}` → `{native['review_right'][:8]}`" in comparison


@then("the report includes the validated technical review record")
def no_appendix(context):
    text = context.markdown.read_text(encoding="utf-8")
    for heading in ("逐檔覆蓋", "固定版本搜尋紀錄", "功能鏈與 Repo 判定", "變更與延伸契約"):
        assert heading in text


@then("the finding has the common fields and only its frozen code excerpt")
def finding_details(context):
    text = context.markdown.read_text(encoding="utf-8")
    assert ["問題名稱", "影響說明", "操作情境", "建議處理", "觸發條件", "證據說明", "影響範圍", "工程修正建議", "證據"] == re.findall(
        r"^\*\*([^*]+)\*\*$", text, flags=re.MULTILINE)
    item = context.finding
    assert f"### {item['id']}｜[{item['priority']}] {item['title']}" in text
    assert "問題數量：P0 0／P1 1／P2 0／P3 0" in text
    for field in ("impact", "trigger", "expected", "actual", "recommendation", "owner"):
        assert item[field] in text
    evidence = item["evidence"][0]
    assert f"`api/app.py:1–1`（來源：`head`；固定版本：`{evidence['ref']}`）" in text
    assert "\n```\ncontract = 'new'\n```\n" in text
    assert "dirty_worktree" not in text
    assert "依程式推導，未實際重現" in text


@then("the report is the only published file")
def single_file(context):
    assert list(context.markdown.parent.iterdir()) == [context.markdown]
    assert "問題數量：P0 0／P1 0／P2 0／P3 0" in context.markdown.read_text(encoding="utf-8")


@then("the owned context has been removed")
def cleaned(context):
    assert not Path(context.manifest["context_dir"]).exists()
