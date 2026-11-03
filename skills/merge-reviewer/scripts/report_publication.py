"""Output-only report staging and atomic, no-overwrite publication."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import sys
import uuid


class PublicationError(OSError):
    """An output failure, distinct from invalid or unreadable review evidence."""

    def __init__(self, operation, path, cause):
        super().__init__(cause.errno, f"report {operation}: {cause.strerror or cause}", str(path))
        self.operation = operation
        self.winerror = getattr(cause, "winerror", None)
        self.recoverable = (isinstance(cause, PermissionError)
                            or cause.errno in {errno.EACCES, errno.EPERM, errno.EBUSY, errno.ENOSPC,
                                              errno.EROFS, errno.EMFILE, errno.ENFILE,
                                              getattr(errno, "EDQUOT", -1)}
                            or self.winerror in {5, 32, 33, 112})


@contextmanager
def staged_report(directory, content):
    """Create an exclusive sibling file with normal output-directory permissions.

    mkdtemp(mode=0700) installs a protected Windows DACL. An exclusive sibling file
    avoids transferring that private directory's ACL to the published report.
    Exclusive file creation also retains the normal file umask/default ACL on
    POSIX, matching the former report.md rather than mkstemp's forced 0600.
    """
    directory, stage = Path(directory), None
    try:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for _ in range(10):
                candidate = directory / (".merge-review-" + uuid.uuid4().hex + ".tmp")
                try:
                    stream = candidate.open("xb")
                except FileExistsError:
                    continue
                stage = candidate  # Only a file we created may be removed below.
                with stream:
                    stream.write(content)
                    stream.flush()
                break
            else:
                raise FileExistsError(errno.EEXIST, "report staging collisions exceeded limit", str(directory))
            if stage.read_bytes() != content:
                raise OSError(errno.EIO, "staged report read differs")
        except OSError as exc:
            raise PublicationError("staging", directory, exc) from exc
        yield stage
    finally:
        if stage is not None:
            unwinding = sys.exc_info()[0] is not None
            try:
                stage.unlink(missing_ok=True)
            except OSError as exc:
                if not unwinding:
                    raise PublicationError("staging cleanup", stage, exc) from exc


def commit_report(stage, output):
    """Atomically expose a complete file; never replace an existing report."""
    try:
        try:
            os.link(stage, output)
        except FileExistsError:
            raise
        except OSError as exc:
            # Windows rename is also atomic and refuses an existing destination.
            if os.name != "nt" or not (getattr(exc, "winerror", None) == 5
                                       or exc.errno in (1, 13, 17, 95)):
                raise
            os.rename(stage, output)
    except FileExistsError:
        raise
    except OSError as exc:
        raise PublicationError("commit", output, exc) from exc


def preflight(directory):
    """Exercise the exact staging, commit, readback and cleanup used to publish."""
    output = Path(directory) / (".review-publish-probe-" + uuid.uuid4().hex)
    content = b"merge-reviewer publication probe\n"
    committed = False
    try:
        with staged_report(directory, content) as stage:
            commit_report(stage, output)
            committed = True
            try:
                if output.read_bytes() != content:
                    raise OSError(errno.EIO, "published probe read differs")
            except OSError as exc:
                raise PublicationError("readback", output, exc) from exc
    finally:
        if committed:
            unwinding = sys.exc_info()[0] is not None
            try:
                output.unlink()
            except OSError as exc:
                if not unwinding:
                    raise PublicationError("probe cleanup", output, exc) from exc
