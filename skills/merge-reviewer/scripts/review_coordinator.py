"""Local coordination for opt-in review sessions; no daemon or network client.

Only the existing Git runner may use the network. Cache entries are accelerators,
never a replacement for verification of the owned evidence copied into a session.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import copy
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

import git_network_lease

ACTIVE = ContextVar("review_coordination", default=None)
RUNTIME_SCHEMA = "MergeReviewRuntime/v1"
SYNC_SCHEMA = "MergeReviewSync/v2"
PACK_SCHEMA = "MergeReviewPack/v1"
IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
SYNC_TTL = 300
FAILURE_TTL = 30
PACK_LIMIT = 2 * 1024 ** 3


class Cancelled(OSError):
    pass


def plain(path):
    path = Path(path)
    for candidate in (path, *path.parents):
        git_network_lease._plain_directory(candidate)
    return path


def atomic(path, value):
    plain(path.parent)
    git_network_lease._atomic_json(path, value)


def read_json(path):
    plain(path)
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def file_digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


class FileLock:
    """OS-owned local lock; process death releases it without guessing PID/age."""

    def __init__(self, path, timeout=180, cancelled=None):
        self.path, self.timeout = Path(path), timeout
        self.cancelled, self.stream = cancelled, None

    def acquire(self, blocking=True):
        plain(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            if self.cancelled and self.cancelled():
                stream.close()
                raise Cancelled("Review cancelled before the local operation.")
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.stream = stream
                return self
            except OSError as exc:
                if not blocking:
                    stream.close()
                    return None
                if time.monotonic() >= deadline:
                    stream.close()
                    raise TimeoutError(f"Local review operation is busy: {self.path}") from exc
                time.sleep(.04)

    def release(self):
        if self.stream:
            stream, self.stream = self.stream, None
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_UN)
            finally:
                stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def runtime(root, *, create=False):
    root = plain(Path(root).expanduser().absolute()).resolve()
    marker = root / ".merge-reviewer-runtime.json"
    if create:
        if any((parent / ".git").exists() for parent in (root, *root.parents)):
            raise ValueError("Review runtime must be outside every Git Repo.")
        root.mkdir(parents=True, exist_ok=True)
        # The first simultaneous starts must never observe a partial marker.
        with FileLock(root / ".ownership.lock"):
            if not marker.exists():
                if any(item.name != ".ownership.lock" for item in root.iterdir()):
                    raise ValueError(f"Review runtime is not owned: {root}")
                atomic(marker, {"schema": RUNTIME_SCHEMA, "directory": str(root)})
    value = read_json(marker)
    if value != {"schema": RUNTIME_SCHEMA, "directory": str(root)}:
        raise ValueError("Invalid review runtime ownership.")
    return root


def remove_owned(path, parent):
    """Remove only this runtime's concrete artifact, never a calculated ancestor."""
    path, parent = plain(path).resolve(), plain(parent).resolve()
    if not path.is_relative_to(parent) or path == parent:
        raise ValueError("Refusing cleanup outside the owned cache.")
    for directory, folders, files in os.walk(path, followlinks=False):
        for name in folders + files:
            plain(Path(directory) / name)
    shutil.rmtree(path)


def safe_error(error):
    text = str(error)
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1<redacted>@", text)
    return re.sub(r"(?i)(authorization|private-token|access_token|password)([=: ]+)\S+",
                  r"\1\2<redacted>", text)


class Coordination:
    def __init__(self, root, request_id, notify=None, refresh=False, auth_scope=""):
        self.root, self.request_id = runtime(root), request_id
        self.notify, self.refresh, self.auth_scope = notify, refresh, auth_scope
        self.cancel_file = self.root / "jobs" / (request_id + ".cancel")
        self.pins, self.syncs, self.pack_hits = {}, [], 0
        self._packing = threading.local()
        self._phase_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._last_phase = None
        self._phase_started = time.perf_counter()
        self.timings = {}
        self._in_sync, self._sync_directory, self._sync_branches = False, None, set()
        self._waiter_path = None
        self._waiter_presence = None

    def cancelled(self):
        return self.cancel_file.exists()

    def check(self):
        if self.cancelled() and (not self._in_sync or self.network_cancelled()):
            raise Cancelled("Review cancelled.")

    def network_cancelled(self):
        if not self.cancelled():
            return False
        if self._sync_directory is not None:
            for path in (self._sync_directory / "waiters").glob("*.json"):
                if path.stem == self.request_id or not IDENTIFIER.fullmatch(path.stem):
                    continue
                if (self.root / "jobs" / (path.stem + ".cancel")).exists():
                    continue
                # The waiter owns this OS lock while alive. Stale files from a
                # dead process cannot keep a cancelled producer running.
                presence = FileLock(path.with_suffix(".live.lock"))
                if presence.acquire(blocking=False):
                    presence.release()
                    continue
                waiter = read_json(path)
                if set(waiter.get("refs", [])) <= self._sync_branches:
                    return False
        return True

    def phase(self, value, **fields):
        self.check()
        with self._phase_lock:
            tick = time.perf_counter()
            if self._last_phase:
                previous = self._last_phase[0]
                self.timings[previous] = self.timings.get(previous, 0) + tick - self._phase_started
            self._phase_started = tick
            signature = (value, fields)
            changed = self._last_phase != signature
            self._last_phase = signature
            if self.notify and changed:
                self.notify(value, fields)

    def finish_phase(self):
        """Close the active phase so ready/published metrics include its tail."""
        with self._phase_lock:
            tick = time.perf_counter()
            if self._last_phase:
                previous = self._last_phase[0]
                self.timings[previous] = self.timings.get(previous, 0) + tick - self._phase_started
            self._phase_started = tick
            self._last_phase = None

    @contextmanager
    def activate(self):
        token = ACTIVE.set(self)
        try:
            yield self
        finally:
            ACTIVE.reset(token)

    @contextmanager
    def packing(self):
        depth = getattr(self._packing, "depth", 0)
        if depth:
            self._packing.depth = depth + 1
            try:
                yield
            finally:
                self._packing.depth -= 1
            return
        waiting = time.perf_counter()
        deadline = time.monotonic() + 180
        chosen = None
        try:
            while chosen is None:
                self.check()
                for slot in range(2):
                    lock = FileLock(self.root / "pack-slots" / f"{slot}.lock")
                    if lock.acquire(blocking=False):
                        chosen = lock
                        break
                if chosen is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Local snapshot packing quota is busy.")
                    time.sleep(.05)
        finally:
            with self._metrics_lock:
                self.timings["packing_wait_seconds"] = (self.timings.get("packing_wait_seconds", 0)
                                                         + time.perf_counter() - waiting)
        self._packing.depth = 1
        started = time.perf_counter()
        try:
            yield
        finally:
            self._packing.depth = 0
            chosen.release()
            with self._metrics_lock:
                self.timings["packing_seconds"] = (self.timings.get("packing_seconds", 0)
                                                   + time.perf_counter() - started)

    def sync(self, repo, remote, targets, api, fetch):
        self._in_sync = True
        try:
            return self._sync(repo, remote, targets, api, fetch)
        finally:
            if self._waiter_path:
                self._waiter_path.unlink(missing_ok=True)
                if self._waiter_presence:
                    self._waiter_presence.release()
            self._in_sync, self._sync_directory = False, None
            self._waiter_path = None

    def _sync(self, repo, remote, targets, api, fetch):
        """Serialize one identity, recheck receipts, then use the global Git lease."""
        common = str(api.run_git(repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"])).strip()
        url = str(api.run_git(repo, ["remote", "get-url", remote])).strip()
        # Effective credential settings are hashed, never persisted or printed.
        _, config, _ = api.run_git_capture(repo, ["config", "--null", "--get-regexp",
                                                 r"^(credential\.|http\.|url\.|remote\.)"])
        auth_env = {key: value for key, value in os.environ.items()
                    if key.startswith(("GIT_CONFIG_", "GIT_ASKPASS", "GIT_SSH", "SSH_AUTH_SOCK",
                                       "GITLAB_WORKSPACE_SCOPED_AUTH"))}
        identity = digest(["ref-sync/v2", str(Path(common).resolve()), remote, url, getpass.getuser(),
                           self.auth_scope, config, auth_env])
        directory = self.root / "sync" / identity
        directory.mkdir(parents=True, exist_ok=True)
        requested = sorted({target.branch for target in targets})
        self._sync_directory, self._sync_branches = directory, set(requested)
        waiters = directory / "waiters"
        waiters.mkdir(exist_ok=True)
        self._waiter_path = waiters / (self.request_id + ".json")
        self._waiter_presence = FileLock(self._waiter_path.with_suffix(".live.lock")).acquire()
        atomic(self._waiter_path, {"refs": requested})
        failures = {branch: directory / (digest(["ref", branch]) + ".failure.json") for branch in requested}
        epoch_path = self.root / "invalidations" / (digest(str(Path(common).resolve())) + ".json")
        epoch = epoch_path.read_bytes().hex() if epoch_path.exists() else ""
        waiting_at = time.time()
        lock = FileLock(directory / "control.lock", cancelled=self.cancelled)
        self.phase("waiting-network", remote=remote)
        with lock:
            self.check()
            now = time.time()
            epoch = epoch_path.read_bytes().hex() if epoch_path.exists() else ""
            receipts = [(path, read_json(path)) for path in directory.glob("*.receipt.json")]
            receipts.sort(key=lambda item: item[1].get("completed_at", 0), reverse=True)
            selected = {}
            for branch in requested:
                failed = read_json(failures[branch]) if failures[branch].exists() else {}
                if failed.get("epoch") != epoch:
                    failed = {}
                for path, receipt in receipts:
                    completed = receipt.get("completed_at", 0)
                    item = receipt.get("refs", {}).get(branch)
                    eligible = (receipt.get("schema") == SYNC_SCHEMA and receipt.get("identity") == identity
                                and receipt.get("epoch") == epoch and isinstance(item, dict)
                                and not item.get("superseded_by") and 0 <= now - completed < SYNC_TTL
                                and (not self.refresh or completed >= waiting_at)
                                and (not failed or completed > failed.get("failed_at", 0)))
                    if eligible and api.verified_commit(repo, item["ref"]) == item["sha"]:
                        selected[branch] = (path, receipt)
                        break
                if branch not in selected and not self.refresh and failed:
                    age = now - failed.get("failed_at", 0)
                    if 0 <= age < FAILURE_TTL:
                        raise api.ReviewContextError("Shared sync cooldown: " + failed["error"],
                                                     hint="Retry explicitly with --refresh after checking the failure.")
            missing = [target for target in targets if target.branch not in selected]
            # Register readers before fetching another ref: a concurrent cleanup
            # must not remove a private ref while this capture still needs it.
            groups = {}
            for target in targets:
                if target.branch in selected:
                    path, receipt = selected[target.branch]
                    groups.setdefault(str(path), (path, receipt, []))[2].append(target)
            for path, receipt, reused in groups.values():
                receipt.setdefault("users", []).append(self.request_id)
                receipt["users"] = sorted(set(receipt["users"]))
                atomic(path, receipt)
                mode = "joined" if receipt["completed_at"] >= waiting_at else "cached"
                self._adopt(receipt, path, reused, mode, requested)
            if not missing:
                return
            sync_id = uuid.uuid4().hex
            refs = {target.branch: {"ref": f"refs/merge-reviewer-sync/{sync_id}/{i}"}
                    for i, target in enumerate(missing)}
            def supersede_previous(*, failed=False):
                for old_path, old in receipts:
                    if old.get("schema") != SYNC_SCHEMA or old.get("identity") != identity:
                        continue
                    previous = old.get("refs", {})
                    changed = False
                    for branch in refs:
                        if branch in previous and not previous[branch].get("superseded_by"):
                            previous[branch]["superseded_by"] = ("failed:" if failed else "") + sync_id
                            changed = True
                    if changed:
                        if all(item.get("superseded_by") for item in previous.values()):
                            old["superseded_by"] = ("failed:" if failed else "") + sync_id
                        atomic(old_path, old)
            try:
                self.phase("fetch", remote=remote)
                fetch([f"refs/heads/{target.branch}:{refs[target.branch]['ref']}" for target in missing])
                current_epoch = epoch_path.read_bytes().hex() if epoch_path.exists() else ""
                if current_epoch != epoch:
                    raise api.ReviewContextError("Remote synchronization was invalidated while fetching.",
                                                 hint="Start again to capture the latest refs.")
                for item in refs.values():
                    item["sha"] = api.verified_commit(repo, item["ref"])
                    if not item["sha"]:
                        raise ValueError("Synced private ref has no commit.")
                receipt = {"schema": SYNC_SCHEMA, "identity": identity, "sync_id": sync_id,
                           "completed_at": time.time(), "epoch": epoch, "refs": refs,
                           "users": [self.request_id]}
                path = directory / (sync_id + ".receipt.json")
                atomic(path, receipt)
                supersede_previous()
                for target in missing:
                    failures[target.branch].unlink(missing_ok=True)
                self._adopt(receipt, path, missing, "owner", requested)
            except Exception as exc:
                if not self.cancelled():
                    for target in missing:
                        atomic(failures[target.branch], {"failed_at": time.time(), "epoch": epoch,
                                                        "error": safe_error(exc)})
                    supersede_previous(failed=True)
                raise
            # Expired receipts have no readers once their captures release them.
            for old_path in directory.glob("*.receipt.json"):
                old = read_json(old_path)
                if old_path == path or now - old.get("completed_at", now) < SYNC_TTL:
                    continue
                live = []
                for user in old.get("users", []):
                    if not IDENTIFIER.fullmatch(user):
                        continue
                    owner = FileLock(self.root / "jobs" / (user + ".owner.lock"))
                    if owner.acquire(blocking=False):
                        owner.release()
                    else:
                        live.append(user)
                if live:
                    old["users"] = live
                    atomic(old_path, old)
                    continue
                if old.get("schema") == SYNC_SCHEMA and old.get("identity") == identity:
                    for item in old.get("refs", {}).values():
                        if item.get("ref", "").startswith("refs/merge-reviewer-sync/" + old["sync_id"] + "/"):
                            api.run_git(repo, ["update-ref", "-d", item["ref"], item["sha"]])
                    old_path.unlink()

    def _adopt(self, receipt, path, targets, mode, requested):
        for target in targets:
            self.pins[target.input_ref] = receipt["refs"][target.branch]
        self.syncs.append({"mode": mode, "sync_id": receipt["sync_id"],
                           "synced_at": receipt["completed_at"], "receipt": str(path),
                           "ref_count": len(targets), "requested_refs": requested,
                           "synced_refs": [target.branch for target in targets] if mode == "owner" else [],
                           "fetch_count": int(mode == "owner"),
                           "refs": [{"branch": target.branch, "input_ref": target.input_ref,
                                     "sha": receipt["refs"][target.branch]["sha"],
                                     "private_ref": receipt["refs"][target.branch]["ref"],
                                     "synced_at": receipt["completed_at"], "mode": mode}
                                    for target in targets]})

    def release_syncs(self):
        for item in self.syncs:
            path = Path(item["receipt"])
            with FileLock(path.parent / "control.lock"):
                receipt = read_json(path)
                receipt["users"] = [user for user in receipt.get("users", []) if user != self.request_id]
                atomic(path, receipt)

    def peer(self, context, repo, api, build):
        """Cache peer HEAD packs, copying verified bytes into an independent store."""
        object_format = "sha256" if len(repo["head_sha"]) == 64 else "sha1"
        key = digest([str(Path(repo["path"]).resolve()), repo["head_sha"], object_format, "peer-head/v1"])
        cache_root = self.root / "packs"
        cache_root.mkdir(parents=True, exist_ok=True)
        entry = cache_root / key
        with FileLock(self.root / "pack-locks" / (key + ".lock"), cancelled=self.cancelled):
            self.check()
            if entry.exists():
                try:
                    with self.packing():
                        self._restore_peer(context, repo, entry, api)
                    os.utime(entry, None)
                    return True
                except (ValueError, KeyError, TypeError, OSError, api.ReviewContextError):
                    from review_workspace import safe_path
                    destination = safe_path(context, f"repos/{repo['id']}/snapshot.git")
                    if destination.exists():
                        remove_owned(destination, context)
                    repo["sources"] = {}
                    repo.pop("snapshot", None)
                    remove_owned(entry, cache_root)
            with self.packing():
                build()
            temporary = cache_root / (".build-" + uuid.uuid4().hex)
            temporary.mkdir()
            try:
                from review_workspace import evidence_repository
                store = evidence_repository(context, repo)
                files = {}
                for path in (store / "objects/pack").iterdir():
                    if path.suffix in (".pack", ".idx", ".rev"):
                        shutil.copyfile(path, temporary / path.name)
                        files[path.name] = file_digest(path)
                payload = {"schema": PACK_SCHEMA, "key": key, "head": repo["head_sha"],
                           "object_format": repo["snapshot"]["object_format"], "files": files,
                           "source": {k: v for k, v in repo["sources"]["head"].items() if k != "objects"},
                           "instructions": repo.get("instructions", [])}
                atomic(temporary / "entry.json", payload)
                temporary.rename(entry)
            finally:
                if temporary.exists():
                    remove_owned(temporary, cache_root)
        self._prune_packs()
        return False

    def _restore_peer(self, context, repo, entry, api):
        from review_workspace import safe_path, source_environment, require, write_snapshot_refs
        data = read_json(entry / "entry.json")
        require(isinstance(data, dict) and isinstance(data.get("source"), dict)
                and isinstance(data.get("files"), dict) and isinstance(data.get("instructions"), list),
                "Cached peer metadata is malformed.")
        object_format = "sha256" if len(repo["head_sha"]) == 64 else "sha1"
        expected = digest([str(Path(repo["path"]).resolve()), repo["head_sha"], object_format, "peer-head/v1"])
        require(data.get("schema") == PACK_SCHEMA and data.get("key") == expected
                and data.get("object_format") == object_format
                and data.get("head") == repo["head_sha"] and data["source"]["ref"] == repo["head_sha"],
                "Cached peer identity differs.")
        relative = f"repos/{repo['id']}/snapshot.git"
        destination = safe_path(context, relative)
        api.run_git(context, ["init", "--bare", f"--object-format={data['object_format']}", str(destination)])
        for name, checksum in data["files"].items():
            require(re.fullmatch(r"snapshot-[a-f0-9]+\.(pack|idx|rev)", name), "Invalid cached pack filename.")
            path = plain(entry / name)
            require(file_digest(path) == checksum, "Cached pack checksum differs.")
            shutil.copyfile(path, destination / "objects/pack" / name)
        source = {**copy.deepcopy(data["source"]), "objects": relative + "/objects"}
        repo["sources"] = {"head": source}
        repo["snapshot"] = {"policy": "frozen-start/v1", "git_dir": relative,
                            "captured_at": api.datetime.now(api.timezone.utc).isoformat(),
                            "object_format": data["object_format"]}
        write_snapshot_refs(context, repo)
        env = source_environment(context, repo, source, api)
        tree = str(api.run_git(destination, ["rev-parse", source["ref"] + "^{tree}"], env=env)).strip()
        require(tree == source["tree_sha"], "Cached tree identity differs.")
        raw = api.run_git(destination, ["ls-tree", "-r", "-z", tree], env=env, binary=True)
        listing = []
        for row in raw.split(b"\0"):
            if row:
                meta, path = row.split(b"\t", 1)
                mode, kind, oid = meta.decode("ascii").split()
                listing.append({"path": path.decode("utf-8"), "mode": mode, "kind": kind, "blob_sha": oid})
        require(listing == source["files"], "Cached inventory differs.")
        for name in data["files"]:
            if name.endswith(".idx"):
                api.run_git(destination, ["verify-pack", str(destination / "objects/pack" / name)], env=env)
        repo["instructions"] = [{**item, "repo": repo["id"]} for item in data["instructions"]]

    def _prune_packs(self):
        root = self.root / "packs"
        lock = FileLock(root / "eviction.lock")
        if not lock.acquire(blocking=False):
            return
        try:
            entries = []
            for path in root.iterdir():
                if DIGEST.fullmatch(path.name) and path.is_dir():
                    plain(path)
                    size = sum(p.stat().st_size for p in path.iterdir() if p.is_file())
                    entries.append((path.stat().st_mtime, path, size))
            size = sum(row[2] for row in entries)
            for _, path, length in sorted(entries):
                if size <= PACK_LIMIT:
                    break
                owned = FileLock(self.root / "pack-locks" / (path.name + ".lock"))
                if owned.acquire(blocking=False):
                    try:
                        if read_json(path / "entry.json").get("schema") == PACK_SCHEMA:
                            remove_owned(path, root)
                            size -= length
                    finally:
                        owned.release()
        finally:
            lock.release()
