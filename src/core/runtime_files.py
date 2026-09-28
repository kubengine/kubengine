"""Private runtime files, opened relative to verified directory
descriptors.
"""

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from core.config import Application


def runtime_path(name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Runtime filename must be a single path component")
    return Path(Application.ROOT_DIR) / "tmp" / "private-runtime" / name


@contextmanager
def private_runtime_file(
    name: str, *, truncate: bool = False
) -> Iterator[TextIO]:
    """
    Reject symlinks, foreign-owned files, hard links and special files.

    Directory-relative opens keep the checks and the eventual file
    operation on the same directory. Existing private files retain their
    lock inode.
    """
    runtime_path(name)  # Validate before creating or opening anything.
    directory = os.open(Application.ROOT_DIR, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in ("tmp", "private-runtime"):
            try:
                os.mkdir(component, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
            if os.fstat(directory).st_uid != os.geteuid():
                raise PermissionError(
                    "Runtime directory must be owned by the service user"
                )
            os.fchmod(directory, 0o700)

        descriptor = os.open(
            name,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
            ):
                raise PermissionError(
                    "Runtime file must be a private, singly linked regular"
                    " file"
                )
            os.fchmod(descriptor, 0o600)
            if truncate:
                os.ftruncate(descriptor, 0)
            stream = os.fdopen(descriptor, "r+", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            yield stream
    finally:
        os.close(directory)
