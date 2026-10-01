from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def run_python(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    command_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(script), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=command_env,
    )


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def remove_temporary_tree(path: Path) -> None:
    """Remove a test-owned directory only when it is under the system temp root."""
    target = Path(path).resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if target == temporary_root or not target.is_relative_to(temporary_root):
        raise ValueError(f"refusing to remove a path outside the temporary directory: {target}")
    if not target.exists():
        return

    def make_writable(function, item: str, _error) -> None:
        try:
            os.chmod(item, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        function(item)

    shutil.rmtree(target, onerror=make_writable)


def create_repository_fixture(
    root: Path,
    *,
    name: str = "repo",
    two_remotes: bool = False,
) -> tuple[Path, Path]:
    repo = root / name
    remote = root / "origin.git"
    run_git(root, "init", "--bare", str(remote))
    run_git(root, "init", "-b", "main", str(repo))
    run_git(repo, "config", "user.email", "test@example.invalid")
    run_git(repo, "config", "user.name", "Merge Reviewer Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    (repo / "deleted.txt").write_text("kept until the scenario deletes it\n", encoding="utf-8")
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
    return repo, remote
