#!/usr/bin/env python3
"""Build an installable versioned archive for the Merge Reviewer skill."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
ARCHIVE_ROOT = "merge-reviewer"


class ReleasePackageError(ValueError):
    """Raised when a release package cannot be built safely."""


def read_version(version_file: Path) -> str:
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ReleasePackageError(f"unable to read version file: {version_file}") from error

    if not SEMVER.fullmatch(version):
        raise ReleasePackageError(
            f"version must be valid SemVer in X.Y.Z form: {version!r}"
        )
    return version


def build_release(
    *,
    tag: str,
    version_file: Path,
    skill_dir: Path,
    output_dir: Path,
) -> Path:
    version = read_version(version_file)
    expected_tag = f"v{version}"
    if tag != expected_tag:
        raise ReleasePackageError(
            f"tag {tag!r} does not match version {version!r}; expected {expected_tag!r}"
        )

    if not skill_dir.is_dir():
        raise ReleasePackageError(f"skill directory does not exist: {skill_dir}")

    files = sorted(
        path
        for path in skill_dir.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    if not files:
        raise ReleasePackageError(f"skill directory is empty: {skill_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"merge-reviewer-{version}.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            relative_path = path.relative_to(skill_dir).as_posix()
            archive.write(path, f"{ARCHIVE_ROOT}/{relative_path}")
    return archive_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag, for example v0.1.0")
    parser.add_argument(
        "--version-file",
        type=Path,
        default=Path("skills/merge-reviewer/VERSION"),
    )
    parser.add_argument(
        "--skill-dir",
        type=Path,
        default=Path("skills/merge-reviewer"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        archive_path = build_release(
            tag=args.tag,
            version_file=args.version_file,
            skill_dir=args.skill_dir,
            output_dir=args.output_dir,
        )
    except ReleasePackageError as error:
        print(f"release-package: {error}", file=sys.stderr)
        return 2

    print(archive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
