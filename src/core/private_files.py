"""
Open service-owned storage through directory descriptors, without links.
"""

import os
import stat
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def private_directory(root: str | Path, *components: str):
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(directory)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise PermissionError(
                "Storage root must be service-owned and not publicly writable"
            )
        for component in components:
            if (
                not component
                or Path(component).name != component
                or component in {".", ".."}
            ):
                raise ValueError(
                    "Storage names must be single path components"
                )
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
                    "Storage directory must be owned by the service user"
                )
            os.fchmod(directory, 0o700)
        yield directory
    finally:
        os.close(directory)


@contextmanager
def private_file(directory: int, name: str, flags: int = os.O_RDONLY):
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Storage filename must be a single path component")
    fd = os.open(
        name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory
    )
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            raise PermissionError(
                "Storage file must be a service-owned, singly linked regular"
                " file"
            )
        os.fchmod(fd, 0o600)
        yield fd
    finally:
        os.close(fd)


def prepare_database(root: str | Path) -> Path:
    with private_directory(root, "config") as directory:
        with private_file(directory, "sqlite.db", os.O_RDWR | os.O_CREAT):
            pass
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                with private_file(directory, "sqlite.db" + suffix, os.O_RDWR):
                    pass
            except FileNotFoundError:
                pass
    return Path(root) / "config" / "sqlite.db"
