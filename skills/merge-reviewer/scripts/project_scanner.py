"""Shared, read-only project inventory used by Audit and NotebookLM export.

The scanner deliberately knows nothing about Wiki prose.  It inventories the
explicit project root first, records every safe text file (including ignored,
untracked and nested-repository source), and exposes conservative entrypoint
candidates for the caller to trace.  It never executes project code and never
follows links or reparse points outside the root.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
from typing import Any, Iterable, Mapping, Sequence


SCAN_SCHEMA_VERSION = 2
SCAN_PROFILES = frozenset({"target", "framework"})
DEFAULT_SCAN_PROFILE = "target"
DEFAULT_OUTPUT_DIRECTORY = ".notebooklm"
SOURCE_CATEGORIES = frozenset(
    {
        "runtime_source",
        "runtime_config",
        "data_schema",
        "documentation",
        "business_documentation",
        "behavioral_test",
        "ci_cd",
        "iac",
        "engineering_tooling",
    }
)
EXCLUSION_REASON_TO_CATEGORY = {
    "sensitive_filename": "sensitive",
    "binary_or_generated": "binary_or_generated",
    "binary_or_unsupported_encoding": "binary_or_unsupported_encoding",
    "framework_adapter": "framework_adapter",
    "wiki_knowledge_layer": "wiki_knowledge_layer",
    "export_output": "export_output",
    "configured_exclude": "configured_exclude",
    "scan_scope_tests": "scan_scope_tests",
    "link_boundary": "link_boundary",
    "unreadable": "unreadable",
}
EXCLUSION_CATEGORIES = frozenset(EXCLUSION_REASON_TO_CATEGORY.values())
REPORT_CATEGORIES = SOURCE_CATEGORIES | EXCLUSION_CATEGORIES
FRAMEWORK_ADAPTER_PREFIXES = (
    ".agents/skills/codebase-wiki",
    ".codex",
    ".github/prompts",
    ".github/hooks",
    ".github/instructions",
)
FRAMEWORK_ADAPTER_FILES = frozenset(
    {".github/copilot-instructions.md", ".github/copilot-instructions.yaml"}
)
DEFAULT_GENERATED_PARTS = {
    ".git",
    ".notebooklm",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "cache",
    "coverage",
    "dist",
    "logs",
    ".codex-hook-logs",
    ".github-hook-logs",
    ".test-tmp",
    "node_modules",
    "obj",
    "target",
    "vendor",
}
DEFAULT_DEPENDENCY_PARTS = {
    ".bundle",
    ".gradle",
    ".m2",
    ".nuget",
    ".pnpm-store",
    ".yarn",
}
DEFAULT_TEST_PARTS = {"__tests__", "spec", "specs", "test", "tests"}
DEFAULT_SENSITIVE_NAMES = {
    ".env",
    ".npmrc",
    "credentials.json",
    "service-account.json",
}
SENSITIVE_SUFFIXES = (".pem", ".p12", ".pfx", ".key")
SENSITIVE_NAME_PATTERNS = (
    "*.env",
    ".env.*",
    "*credential*",
    "*secret*",
    "id_rsa*",
)
CI_FILENAMES = {
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    "jenkinsfile",
}
DOCUMENTATION_EXTENSIONS = {".adoc", ".md", ".rst", ".txt"}
DATA_EXTENSIONS = {".gql", ".graphql", ".prisma", ".proto", ".sql"}
CONFIG_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".csproj",
    ".gradle",
    ".ini",
    ".json",
    ".lock",
    ".props",
    ".properties",
    ".sln",
    ".toml",
    ".xml",
    ".yaml",
    ".yml",
}
CONFIG_NAMES = {
    ".gitattributes",
    ".gitignore",
    "dockerfile",
    "gemfile",
    "go.mod",
    "go.sum",
    "makefile",
    "package.json",
    "packages.lock.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
}
DATA_PARTS = {"data", "database", "db", "migration", "migrations", "schema", "schemas"}
TRANSACTION_MARKERS = (
    ".codebase-wiki-install-transaction.",
    ".notebooklm-transaction.",
)
WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")
ENTRYPOINT_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("route", "route registration", re.compile(r"@(?:app|api|router|blueprint)\.(?:route|get|post|put|patch|delete|options)\b|\b(?:GET|POST|PUT|PATCH|DELETE)\s+/")),
    ("cli", "command registration", re.compile(r"(?:argparse|click|typer|cobra|commander|yargs|\.command\s*\()")),
    ("job", "scheduled or worker registration", re.compile(r"(?:cron|schedule|scheduled|celery|rq\.|apscheduler|@(?:job|task)\b)")),
    ("event", "event or message consumer", re.compile(r"(?:consumer|subscriber|subscribe|on_message|message_handler|event_handler|add_listener)")),
    ("public-api", "public interface", re.compile(r"(?:export\s+(?:default\s+)?(?:async\s+)?function|^\s*(?:pub\s+)?(?:public\s+)?(?:def|func|fn)\s+[A-Za-z_]\w*)", re.MULTILINE)),
    ("process", "executable module", re.compile(r"if\s+__name__\s*==\s*[\"']__main__[\"']|^#!", re.MULTILINE)),
)


class ScanError(ValueError):
    """Raised when the requested project root cannot be safely inventoried."""


@dataclass(frozen=True)
class ScanSettings:
    """Validated settings shared by the standalone scanner and exporter."""

    scan_profile: str
    output_directory: str
    analysis_include_tests: bool
    business_source_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        attributes = os.stat(path, follow_symlinks=False).st_file_attributes
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def repo_relative(path: Path, root: Path) -> str:
    try:
        relative = Path(os.path.relpath(os.fspath(path), os.fspath(root))).as_posix()
    except ValueError as exc:
        raise ScanError(f"path escapes repository root: {path}") from exc
    if relative in {"", "."}:
        return "."
    if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
        raise ScanError(f"path escapes repository root: {path}")
    return relative


def _has_prefix(relative: str, prefixes: Iterable[str]) -> bool:
    return any(relative == prefix or relative.startswith(prefix + "/") for prefix in prefixes)


def is_framework_adapter(relative: str, scan_profile: str) -> bool:
    """Return whether a repo-relative path belongs to the installed adapter."""

    if scan_profile != "target":
        return False
    normalized = relative.replace("\\", "/").lower()
    return _has_prefix(normalized, FRAMEWORK_ADAPTER_PREFIXES) or normalized in FRAMEWORK_ADAPTER_FILES


def _is_test_file(relative: str) -> bool:
    path = PurePosixPath(relative.lower())
    name = path.name
    if set(path.parts) & DEFAULT_TEST_PARTS:
        return True
    if name in {".coveragerc", "jest.config.js", "jest.config.ts", "playwright.config.js", "playwright.config.ts", "pytest.ini", "tox.ini"}:
        return True
    stem = Path(name).stem
    return stem.startswith("test_") or stem.endswith("_test") or ".test." in name or ".spec." in name


def _is_sensitive(relative: str) -> bool:
    for component in PurePosixPath(relative).parts:
        lower = component.lower()
        if lower in DEFAULT_SENSITIVE_NAMES or lower.endswith(SENSITIVE_SUFFIXES):
            return True
        if any(fnmatch.fnmatch(lower, pattern) for pattern in SENSITIVE_NAME_PATTERNS):
            return True
    return False


def _is_transaction_artifact(relative: str) -> bool:
    for component in PurePosixPath(relative.lower()).parts:
        if component.startswith(("codebase-wiki-stage-", "codebase-wiki-backup-")):
            return True
        if ".staging-" in component or ".backup-" in component:
            return True
        if any(marker in component for marker in TRANSACTION_MARKERS):
            return True
    return False


def _settings_value(settings: Any, name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _framework_adapter(relative: str, scan_profile: str) -> bool:
    return is_framework_adapter(relative, scan_profile)


def _validate_config_path(value: Any, root: Path, field: str) -> str:
    if not isinstance(value, str):
        raise ScanError(f"{field} must be a string")
    candidate = PurePosixPath(value.replace("\\", "/"))
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or re.fullmatch(r"[A-Za-z]:.*", candidate.as_posix())
    ):
        raise ScanError(f"{field} must be a repo-relative path: {value!r}")
    path = root.joinpath(*candidate.parts)
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError) as exc:
        raise ScanError(f"{field} must stay inside the repository: {value!r}") from exc
    return candidate.as_posix().rstrip("/") or "."


def _read_scan_config(root: Path) -> dict[str, Any]:
    config = root / "notebooklm.toml"
    if _is_reparse_point(config):
        raise ScanError(f"config file must not be a symlink or reparse point: {config}")
    if not config.exists():
        return {}
    if not config.is_file():
        raise ScanError(f"config file is not a file: {config}")
    try:
        with config.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ScanError(f"unable to read config: {config}: {exc}") from exc
    nested = raw.get("notebooklm")
    if nested is None:
        return raw
    if not isinstance(nested, dict):
        raise ScanError("[notebooklm] must be a TOML table")
    return nested


def parse_scan_settings(
    root: Path,
    values: Mapping[str, Any],
    *,
    profile_override: str | None = None,
) -> ScanSettings:
    """Validate scanner-owned TOML values and return normalized settings."""

    if not isinstance(values, Mapping):
        raise ScanError("scanner configuration must be a TOML table")
    configured_profile = values.get("scan_profile", DEFAULT_SCAN_PROFILE)
    if not isinstance(configured_profile, str) or configured_profile not in SCAN_PROFILES:
        raise ScanError("scan_profile must be 'target' or 'framework'")
    scan_profile = profile_override if profile_override is not None else configured_profile
    if not isinstance(scan_profile, str) or scan_profile not in SCAN_PROFILES:
        raise ScanError("scan profile must be 'target' or 'framework'")

    output_directory = _validate_config_path(
        values.get("output_directory", DEFAULT_OUTPUT_DIRECTORY),
        root,
        "output_directory",
    )
    if output_directory == ".":
        raise ScanError("output_directory must be a child directory of repository root")

    analysis_include_tests = values.get("analysis_include_tests", True)
    if not isinstance(analysis_include_tests, bool):
        raise ScanError("analysis_include_tests must be true or false")

    def path_tuple(field: str) -> tuple[str, ...]:
        raw = values.get(field, [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ScanError(f"{field} must be an array of strings")
        return tuple(_validate_config_path(item, root, field) for item in raw)

    # Validate exporter-owned path arrays here as well, so the standalone
    # scanner never accepts a malformed configuration that the exporter would
    # later reject.
    path_tuple("extra_paths")

    return ScanSettings(
        scan_profile=scan_profile,
        output_directory=output_directory,
        analysis_include_tests=analysis_include_tests,
        business_source_paths=path_tuple("business_source_paths"),
        exclude_paths=path_tuple("exclude_paths"),
    )


def load_scan_settings(root: Path, profile: str | None = None) -> ScanSettings:
    """Load and validate the scanner-owned settings from notebooklm.toml."""

    root = Path(root).absolute()
    return parse_scan_settings(root, _read_scan_config(root), profile_override=profile)


def exclusion_reason(relative: str, settings: Any, *, business_override: bool = False) -> str | None:
    """Return a stable safety/scope reason without reading file contents."""

    path = PurePosixPath(relative)
    lower = relative.lower()
    parts = set(PurePosixPath(lower).parts)
    output = str(_settings_value(settings, "output_directory", ".notebooklm")).lower().replace("\\", "/")
    if _is_transaction_artifact(relative) or path.name.lower().endswith((".notebooklm-transaction.json", ".notebooklm-transaction.lock")):
        return "binary_or_generated"
    if lower == output or lower.startswith(output + "/"):
        return "export_output"
    if lower == "wiki" or lower.startswith("wiki/"):
        return "wiki_knowledge_layer"
    if parts & DEFAULT_GENERATED_PARTS or parts & DEFAULT_DEPENDENCY_PARTS:
        return "binary_or_generated"
    if _framework_adapter(lower, str(_settings_value(settings, "scan_profile", "target"))):
        return "framework_adapter"
    if _is_sensitive(relative):
        return "sensitive_filename"
    if path.name == ".gitkeep":
        return "binary_or_generated"
    if _has_prefix(relative, _settings_value(settings, "exclude_paths", ())):
        return "configured_exclude"
    if _is_test_file(relative) and not bool(_settings_value(settings, "analysis_include_tests", True)) and not business_override:
        return "scan_scope_tests"
    # CI/CD, IaC, scripts, tools, examples and samples are project-owned
    # evidence.  They are classified and included by default; callers may
    # still exclude an explicit path through exclude_paths.
    return None


def classify_project_file(relative: str) -> str:
    path = PurePosixPath(relative.lower())
    if _is_test_file(relative):
        return "behavioral_test"
    if _has_prefix(path.as_posix(), (".github/workflows", ".circleci")) or path.name in CI_FILENAMES:
        return "ci_cd"
    if set(path.parts) & {".terraform", "helm", "infra", "infrastructure", "k8s", "terraform"} or path.suffix in {".tf", ".tfvars"}:
        return "iac"
    if path.parts and path.parts[0] in {"tools", "scripts", "bin", "examples", "samples"}:
        return "engineering_tooling"
    if path.suffix in DOCUMENTATION_EXTENSIONS or path.name.startswith(("readme", "changelog", "license", "contributing")):
        return "documentation"
    if path.suffix in DATA_EXTENSIONS or set(path.parts) & DATA_PARTS:
        return "data_schema"
    if path.suffix in CONFIG_EXTENSIONS or path.name in CONFIG_NAMES:
        return "runtime_config"
    return "runtime_source"


def _bounded_excluded_root_summary(path: Path, relative: str, reason: str, *, boundary_only: bool = False) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path": relative,
        "reason": reason,
        "kind": "directory",
        "pruned": True,
        "entry_limit": 4096,
        "observed_entries": 0,
        "observed_files": 0,
        "observed_directories": 0,
        "observed_bytes": 0,
        "errors": 0,
        "truncated": False,
    }
    if boundary_only:
        return summary
    pending = [path]
    while pending and summary["observed_entries"] < summary["entry_limit"]:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            summary["errors"] += 1
            continue
        for entry in entries:
            if summary["observed_entries"] >= summary["entry_limit"]:
                summary["truncated"] = True
                break
            summary["observed_entries"] += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    summary["observed_directories"] += 1
                    child = Path(entry.path)
                    if not _is_reparse_point(child):
                        pending.append(child)
                elif entry.is_file(follow_symlinks=False):
                    summary["observed_files"] += 1
                    summary["observed_bytes"] += entry.stat(follow_symlinks=False).st_size
            except OSError:
                summary["errors"] += 1
    if pending:
        summary["truncated"] = True
    return summary


def _append_excluded_root(excluded_roots: list[dict[str, Any]], skipped: list[dict[str, str]], path: Path, relative: str, reason: str, *, boundary_only: bool = False) -> None:
    excluded_roots.append(_bounded_excluded_root_summary(path, relative, reason, boundary_only=boundary_only))
    skipped.append({"path": relative, "reason": reason})


def _iter_project_files(
    root: Path,
    settings: Any,
    *,
    excluded: list[dict[str, str]],
    excluded_roots: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    start: Path | None = None,
    business_override: bool = False,
) -> Iterable[tuple[Path, str]]:
    base = start or root
    # A configured source boundary must not turn a symlink into an alternate
    # traversal root.  The standalone scanner also rejects a linked project
    # root in ``scan_project`` below; this guard covers business/extra source
    # paths passed by callers after the initial root check.
    if _is_reparse_point(base):
        relative = repo_relative(base, root)
        _append_excluded_root(
            excluded_roots,
            skipped,
            base,
            relative,
            "link_boundary",
            boundary_only=True,
        )
        return
    if base.is_file():
        yield base, repo_relative(base, root)
        return
    pending = [base]
    while pending:
        current = pending.pop()
        if current != root:
            current_relative = repo_relative(current, root)
            root_reason = exclusion_reason(
                current_relative, settings, business_override=business_override
            )
            if root_reason:
                _append_excluded_root(
                    excluded_roots, skipped, current, current_relative, root_reason
                )
                continue
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            _append_excluded_root(excluded_roots, skipped, current, repo_relative(current, root), "unreadable", boundary_only=True)
            continue
        directories: list[Path] = []
        for entry in entries:
            path = Path(entry.path)
            relative = repo_relative(path, root)
            try:
                is_link = entry.is_symlink() or _is_reparse_point(path)
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                _append_excluded_root(excluded_roots, skipped, path, relative, "unreadable", boundary_only=True)
                continue
            if is_link:
                linked_directory = is_dir
                if entry.is_symlink() and not linked_directory:
                    try:
                        # Follow only the directory bit stat; never enumerate
                        # the linked target or include its contents.
                        linked_directory = entry.is_dir(follow_symlinks=True)
                    except OSError:
                        linked_directory = False
                if linked_directory:
                    _append_excluded_root(excluded_roots, skipped, path, relative, "link_boundary", boundary_only=True)
                else:
                    excluded.append({"path": relative, "reason": "link_boundary"})
                    skipped.append({"path": relative, "reason": "link_boundary"})
                continue
            if is_dir:
                reason = exclusion_reason(
                    relative, settings, business_override=business_override
                )
                if reason:
                    _append_excluded_root(excluded_roots, skipped, path, relative, reason)
                else:
                    directories.append(path)
            elif is_file:
                yield path, relative
        pending.extend(reversed(directories))


def _entrypoint_candidates(relative: str, text: str, category: str) -> list[dict[str, Any]]:
    if category not in {"runtime_source", "runtime_config", "data_schema", "engineering_tooling", "ci_cd", "iac"}:
        return []
    candidates: list[dict[str, Any]] = []
    for kind, reason, pattern in ENTRYPOINT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        line = text.count("\n", 0, match.start()) + 1
        stem = re.sub(r"[^a-z0-9]+", "-", PurePosixPath(relative).with_suffix("").as_posix().lower()).strip("-")
        candidates.append({
            "entrypoint_id": f"ENTRY-{stem}-{kind}",
            "path": relative,
            "line": line,
            "kind": kind,
            "symbol": PurePosixPath(relative).stem,
            "capability_hint": f"FUNC-{stem}",
            "confidence": "candidate",
            "reason": reason,
        })
    return candidates


def _coverage_rules(pages: Sequence[Any], root: Path) -> tuple[list[dict[str, Any]], list[str], int]:
    page = next((item for item in pages if getattr(item, "path", "") == "wiki/synthesis/codebase-functional-coverage.md"), None)
    if page is None:
        return [], ["missing coverage ledger: wiki/synthesis/codebase-functional-coverage.md"], 1
    text = str(getattr(page, "text", ""))
    version_match = re.search(r"(?m)^coverage_schema_version:\s*(\d+)\s*$", text)
    version = int(version_match.group(1)) if version_match else 1
    rules: list[dict[str, Any]] = []
    issues: list[str] = []
    seen_paths: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3 or not cells[0].startswith("`") or not cells[0].endswith("`"):
            continue
        raw_path = cells[0][1:-1].strip().replace("\\", "/")
        prefix = raw_path.endswith("/")
        path = raw_path.rstrip("/")
        if path in seen_paths:
            issues.append(f"duplicate coverage disposition row: {raw_path}")
        seen_paths.add(path)
        # Schema v1 rows contain path/disposition/requirements.  Schema v2
        # rows additionally bind the row to the file hash, scanner category,
        # capability/process association and a human-readable reason.
        if len(cells) >= 7:
            hash_value, category, disposition, requirement_cell, association, reason = cells[1:7]
        else:
            hash_value, category, disposition, requirement_cell, association, reason = "", "", cells[1], cells[2], "", ""
        hash_value = hash_value.strip().strip("`")
        if disposition not in {"functional-evidence", "supporting-technical", "no-observable-behavior", "analysis-gap"}:
            issues.append(f"invalid coverage disposition for {raw_path}: {disposition}")
            continue
        requirements = sorted(WIKI_LINK.findall(requirement_cell))
        if disposition in {"functional-evidence", "supporting-technical"} and not requirements:
            issues.append(f"coverage disposition {disposition} requires a requirement link: {raw_path}")
        if version >= 2 and not prefix:
            if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", hash_value, re.IGNORECASE):
                issues.append(f"v2 coverage row requires a sha256 hash: {raw_path}")
            if not category:
                issues.append(f"v2 coverage row requires a scanner category: {raw_path}")
            if not association:
                issues.append(f"v2 coverage row requires a function/process association: {raw_path}")
            if not reason:
                issues.append(f"v2 coverage row requires a disposition reason: {raw_path}")
        rules.append({
            "path": path,
            "prefix": prefix,
            "disposition": disposition,
            "requirements": requirements,
            "hash": hash_value.lower().removeprefix("sha256:"),
            "category": category,
            "association": association,
            "reason": reason,
        })
    if not rules:
        issues.append("coverage ledger contains no disposition rows")
    return sorted(rules, key=lambda item: (-len(item["path"]), item["path"])), issues, version


def _coverage_match(relative: str, rules: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    for rule in rules:
        if relative == rule["path"] or (rule["prefix"] and relative.startswith(rule["path"] + "/")):
            return rule
    return None


def scan_project(root: Path, settings: Any, pages: Sequence[Any] = ()) -> dict[str, Any]:
    root = Path(root).absolute()
    if not root.exists() or not root.is_dir():
        raise ScanError(f"project root is not a directory: {root}")
    if _is_reparse_point(root):
        raise ScanError(f"project root must not be a symlink or reparse point: {root}")
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    excluded_roots: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    entrypoints: list[dict[str, Any]] = []
    read_issues: list[dict[str, str]] = []
    def add_file(path: Path, relative: str, *, business: bool = False) -> None:
        if business:
            # The ordinary walk may have recorded a test-scope exclusion before
            # the configured business-source pass deliberately re-includes the
            # same file.  Keep the inventory one-row-per-path for validators.
            excluded[:] = [item for item in excluded if item["path"] != relative]
            read_issues[:] = [item for item in read_issues if item["path"] != relative]
        existing = next((item for item in included if item["path"] == relative), None)
        if existing is not None:
            # A configured business source is a deliberate semantic override.
            # The ordinary walk may have classified a test/example as
            # ``behavioral_test`` already; retain one file row and promote it
            # to business evidence instead of silently ignoring the override.
            if business:
                existing["category"] = "business_documentation"
            return
        reason = exclusion_reason(relative, settings, business_override=business)
        if reason:
            excluded.append({"path": relative, "reason": reason})
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            excluded.append({"path": relative, "reason": "unreadable"})
            read_issues.append({"path": relative, "reason": "unreadable", "detail": str(exc)})
            return
        if b"\x00" in data:
            excluded.append({"path": relative, "reason": "binary_or_unsupported_encoding"})
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            excluded.append({"path": relative, "reason": "binary_or_unsupported_encoding"})
            read_issues.append({"path": relative, "reason": "binary_or_unsupported_encoding", "detail": str(exc)})
            return
        category = "business_documentation" if business else classify_project_file(relative)
        included.append({
            "path": relative,
            "category": category,
            "byte_count": len(data),
            "sha256": sha256_bytes(data),
            "line_count": max(1, len(text.splitlines())),
        })
        entrypoints.extend(_entrypoint_candidates(relative, text, category))

    for path, relative in _iter_project_files(
        root,
        settings,
        excluded=excluded,
        excluded_roots=excluded_roots,
        skipped=skipped,
    ):
        add_file(path, relative)

    for item in skipped:
        if item.get("reason") == "unreadable" and not any(
            issue.get("path") == item.get("path") for issue in read_issues
        ):
            read_issues.append({
                "path": str(item.get("path", "")),
                "reason": "unreadable",
                "detail": "directory or source could not be read",
            })

    business_sources = tuple(_settings_value(settings, "business_source_paths", ()))
    for source in business_sources:
        normalized_source = str(source).replace("\\", "/")
        source_parts = PurePosixPath(normalized_source)
        if source_parts.is_absolute() or ".." in source_parts.parts:
            skipped.append({"path": normalized_source, "reason": "path_escape"})
            continue
        source_path = root.joinpath(*source_parts.parts)
        if not source_path.exists():
            skipped.append({"path": normalized_source, "reason": "business_source_missing"})
            continue
        business_skipped: list[dict[str, str]] = []
        for path, relative in _iter_project_files(
            root,
            settings,
            start=source_path,
            business_override=True,
            excluded=excluded,
            excluded_roots=excluded_roots,
            skipped=business_skipped,
        ):
            add_file(path, relative, business=True)
        skipped.extend(business_skipped)

    included.sort(key=lambda item: item["path"])
    excluded.sort(key=lambda item: (item["path"], item["reason"]))
    excluded_roots.sort(key=lambda item: (item["path"], item["reason"]))
    skipped.sort(key=lambda item: (item["path"], item["reason"]))
    entrypoints.sort(key=lambda item: (item["path"], item["line"], item["kind"]))

    rules, ledger_issues, coverage_schema_version = _coverage_rules(list(pages), root)
    dispositions: list[dict[str, Any]] = []
    uncovered: list[str] = []
    legacy_prefix_paths: list[str] = []
    for item in included:
        rule = _coverage_match(item["path"], rules)
        if rule is None:
            uncovered.append(item["path"])
            continue
        if rule["prefix"]:
            legacy_prefix_paths.append(item["path"])
            if coverage_schema_version >= 2:
                ledger_issues.append(f"directory prefix is not a v2 file disposition: {item['path']}")
        elif coverage_schema_version >= 2:
            if rule.get("hash") and rule["hash"] != item["sha256"]:
                ledger_issues.append(f"coverage ledger hash mismatch: {item['path']}")
            if rule.get("category") and rule["category"] != item["category"]:
                ledger_issues.append(f"coverage ledger category mismatch: {item['path']}")
        dispositions.append({
            "path": item["path"],
            "disposition": rule["disposition"],
            "requirements": rule["requirements"],
            "hash": rule.get("hash", ""),
            "category": rule.get("category", ""),
            "association": rule.get("association", ""),
            "reason": rule.get("reason", ""),
            "match": "prefix" if rule["prefix"] else "exact",
        })
    included_paths = {item["path"] for item in included}
    if coverage_schema_version >= 2:
        for rule in rules:
            if rule["prefix"]:
                ledger_issues.append(f"directory prefix is not a v2 file disposition: {rule['path']}/")
            elif rule["path"] not in included_paths:
                ledger_issues.append(f"coverage disposition is not in current inventory: {rule['path']}")
    if coverage_schema_version >= 2:
        # A v2 ledger must account for every file with an exact row.  A prefix
        # match remains visible for migration diagnostics but does not count.
        uncovered.extend(legacy_prefix_paths)
        dispositions = [item for item in dispositions if item["match"] == "exact"]
    dispositions.sort(key=lambda item: item["path"])
    ledger_issues = sorted(set(ledger_issues))

    # Generated/cache directories can appear as imports and tests create
    # ``__pycache__`` between preview and apply.  They are still reported in
    # ``skipped``/``excluded_roots`` for auditability, but must not change the
    # source snapshot identity or make an otherwise identical preflight stale.
    # Wiki and export/cache trees are outside the raw source snapshot.  Their
    # bounded metadata must not make a report or preflight stale when managed
    # knowledge/output files are added or regenerated.
    snapshot_volatile_reasons = {
        "binary_or_generated",
        "export_output",
        "wiki_knowledge_layer",
    }
    snapshot_excluded = [
        item for item in excluded
        if item.get("reason") not in snapshot_volatile_reasons
    ]
    snapshot_excluded_roots = [
        item for item in excluded_roots
        if item.get("reason") not in snapshot_volatile_reasons
    ]
    snapshot_skipped = [
        item for item in skipped
        if item.get("reason") not in snapshot_volatile_reasons
    ]
    material = {
        "scan_schema_version": SCAN_SCHEMA_VERSION,
        "scan_profile": str(_settings_value(settings, "scan_profile", "target")),
        "included": [{key: item[key] for key in ("path", "category", "byte_count", "sha256")} for item in included],
        "excluded": snapshot_excluded,
        "excluded_roots": [{"path": item["path"], "reason": item["reason"]} for item in snapshot_excluded_roots],
        "skipped": snapshot_skipped,
        "read_issues": read_issues,
        "entrypoints": entrypoints,
    }
    digest = sha256_bytes(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    snapshot_id = f"sha256:{digest}"
    return {
        "scan_schema_version": SCAN_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "root": str(root),
        "scan_profile": str(_settings_value(settings, "scan_profile", "target")),
        "included": included,
        "excluded": excluded,
        "excluded_roots": excluded_roots,
        "skipped": skipped,
        "read_issues": read_issues,
        "entrypoint_candidates": entrypoints,
        "entrypoint_count": len(entrypoints),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "excluded_root_count": len(excluded_roots),
        "included_by_category": dict(sorted(Counter(item["category"] for item in included).items())),
        "business_source_paths": list(business_sources),
        "business_source_count": sum(
            1
            for item in included
            if item["path"] in business_sources
            or any(item["path"].startswith(source.rstrip("/") + "/") for source in business_sources)
        ),
        "excluded_by_reason": dict(sorted(Counter(item["reason"] for item in excluded).items())),
        "excluded_roots_by_reason": dict(sorted(Counter(item["reason"] for item in excluded_roots).items())),
        "declared_source_paths": [],
        "coverage_dispositions": dispositions,
        "coverage_disposition_counts": dict(sorted(Counter(item["disposition"] for item in dispositions).items())),
        "coverage_ledger_issues": ledger_issues,
        "coverage_schema_version": coverage_schema_version,
        "legacy_prefix_paths": sorted(set(legacy_prefix_paths)),
        "analysis_gap_paths": [item["path"] for item in dispositions if item["disposition"] == "analysis-gap"],
        "uncovered_paths": sorted(set(uncovered)),
        "wiki_status_counts": {},
        "required_documents": {},
    }


def cli_settings(root: Path, profile: str) -> Any:
    """Build the small settings object needed by the standalone CLI."""
    return load_scan_settings(root, profile)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Inventory a project root without executing project code.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", choices=("target", "framework"), default="target")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)
    try:
        result = scan_project(args.root, cli_settings(args.root, args.profile), ())
    except (OSError, ScanError, UnicodeError) as exc:
        payload = {"scan_schema_version": SCAN_SCHEMA_VERSION, "ok": False, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    payload = {"scan_schema_version": SCAN_SCHEMA_VERSION, "ok": True, **result}
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"scan snapshot: {result['snapshot_id']}")
        print(f"included: {result['included_count']}; excluded: {result['excluded_count']}; entrypoints: {result['entrypoint_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
