from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from zipfile import ZipFile

from behave import given, then, when


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "package_release.py"


def load_release_module():
    spec = importlib.util.spec_from_file_location("merge_reviewer_release_steps", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load release package module: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def build_fixture(context, version: str) -> None:
    context.root = Path(tempfile.mkdtemp(prefix="merge-reviewer-release-feature-"))
    context.skill = context.root / "skill"
    context.output = context.root / "dist"
    write(context.skill / "SKILL.md", "---\nname: merge-reviewer\n---\n")
    write(context.skill / "VERSION", f"{version}\n")
    write(context.skill / "agents" / "openai.yaml", "interface:\n")
    write(context.skill / "references" / "review-rules.md", "# Rules\n")
    write(context.skill / "scripts" / "git_review_context.py", "print('ok')\n")
    write(context.root / "tests" / "test_should_not_ship.py", "not packaged\n")
    context.release = load_release_module()


@given('a release fixture with version "{version}"')
def step_release_fixture(context, version: str) -> None:
    build_fixture(context, version)


@when('I build a release package for tag "{tag}"')
def step_build_release(context, tag: str) -> None:
    context.package_error = None
    context.package_path = context.release.build_release(
        tag=tag,
        version_file=context.skill / "VERSION",
        skill_dir=context.skill,
        output_dir=context.output,
    )


@when('I try to build a release package for tag "{tag}"')
def step_try_build_release(context, tag: str) -> None:
    context.package_path = None
    try:
        context.release.build_release(
            tag=tag,
            version_file=context.skill / "VERSION",
            skill_dir=context.skill,
            output_dir=context.output,
        )
    except context.release.ReleasePackageError as error:
        context.package_error = str(error)
    else:
        context.package_error = None


@then("the release package is created")
def step_package_created(context) -> None:
    assert context.package_path.exists()


@then("release packaging fails with a version mismatch")
def step_version_mismatch(context) -> None:
    assert context.package_path is None
    assert context.package_error is not None
    assert "does not match" in context.package_error


@then("no release package is created")
def step_no_release_package(context) -> None:
    assert not context.output.exists()


def archive_names(context) -> set[str]:
    with ZipFile(context.package_path) as archive:
        return set(archive.namelist())


@then('the package root is "{root}"')
def step_package_root(context, root: str) -> None:
    assert all(name == root or name.startswith(f"{root}/") for name in archive_names(context))


@then('the package contains "SKILL.md", "VERSION", "agents/openai.yaml", "references/review-rules.md", and "scripts/git_review_context.py"')
def step_package_contains_skill_files(context) -> None:
    names = archive_names(context)
    for relative_path in (
        "SKILL.md",
        "VERSION",
        "agents/openai.yaml",
        "references/review-rules.md",
        "scripts/git_review_context.py",
    ):
        assert f"merge-reviewer/{relative_path}" in names


@then('the package excludes "{relative_path}"')
def step_package_excludes(context, relative_path: str) -> None:
    assert f"merge-reviewer/{relative_path}" not in archive_names(context)
