"""Small state files that must never be torn or world-readable."""

import os
import secrets
import threading
from pathlib import Path

_locks_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    """One lock per file for the whole process, whoever is writing it."""
    key = Path(os.path.abspath(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def write_private_text_atomic(path: Path, content: str) -> None:
    """Replace ``path`` with ``content`` atomically, readable only by its owner.

    A power cut halfway through an in-place write would leave a truncated
    file. Writing a temporary file in the same directory, fsyncing it,
    renaming it over the old one and fsyncing the directory means the file
    always holds either the old or the new content in full.

    The temporary name is unique per write (pid plus a random suffix) and
    created with ``O_EXCL``, and writes to one path are serialised for the
    whole process: with a fixed name, a second writer's ``O_TRUNC`` could
    empty the first one's file before it was renamed into place, and one
    writer's rename could take the other's file away from under it.
    """
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with _lock_for(path):
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
