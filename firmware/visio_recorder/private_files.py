"""Small state files that must never be torn or world-readable."""

import os
from pathlib import Path


def write_private_text_atomic(path: Path, content: str) -> None:
    """Replace ``path`` with ``content`` atomically, readable only by its owner.

    A power cut halfway through an in-place write would leave a truncated
    file. Writing a temporary file in the same directory, fsyncing it,
    renaming it over the old one and fsyncing the directory means the file
    always holds either the old or the new content in full.
    """
    tmp_path = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
