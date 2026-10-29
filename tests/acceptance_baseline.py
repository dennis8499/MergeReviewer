"""Identity checks for the frozen helper and model-review baseline."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess


BASELINE_REVISION = "e2430b69b005226a1d77402bb090289c74e31dc2"
BASELINE_VERSION = "0.20.1"
BASELINE_SKILL_SHA256 = "2a1d524800860595248ae232dc8ec3aec228ea71e620bd262a08ec44335e1d6a"


def skill_digest(root: Path) -> str:
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError("Frozen skills cannot contain symbolic links")
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def skill_root(source: Path) -> Path:
    """Resolve either a source checkout root or an extracted skill directory."""
    source = Path(source).expanduser().resolve()
    if (source / "SKILL.md").is_file() and (source / "VERSION").is_file():
        return source
    nested = source / "skills" / "merge-reviewer"
    if (nested / "SKILL.md").is_file() and (nested / "VERSION").is_file():
        return nested
    raise ValueError("Baseline must be a MergeReviewer checkout or extracted skill directory")


def validate_baseline(source: Path) -> dict:
    skill = skill_root(source)
    version = (skill / "VERSION").read_text(encoding="utf-8").strip()
    digest = skill_digest(skill)
    if version != BASELINE_VERSION or digest != BASELINE_SKILL_SHA256:
        raise ValueError(f"Baseline skill must match {BASELINE_VERSION} at {BASELINE_REVISION}")

    source = Path(source).expanduser().resolve()
    revision_verified = False
    if skill == source / "skills" / "merge-reviewer":
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "--verify", "HEAD^{commit}"],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        if result.returncode == 0:
            if result.stdout.strip() != BASELINE_REVISION:
                raise ValueError(f"Baseline checkout must be at {BASELINE_REVISION}")
            revision_verified = True
    return {"revision": BASELINE_REVISION, "version": version, "skill_sha256": digest,
            "revision_verified_from_checkout": revision_verified, "skill_root": str(skill)}
