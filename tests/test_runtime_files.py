import os

import pytest

from core.config import Application
from core.runtime_files import private_runtime_file, runtime_path


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    return tmp_path


def test_private_files_preserve_lock_inode_and_restrict_permissions(runtime):
    with private_runtime_file("task.lock") as stream:
        stream.write("existing lock")
    path = runtime_path("task.lock")
    inode = path.stat().st_ino
    with private_runtime_file("task.lock") as stream:
        assert stream.read() == "existing lock"
    assert path.stat().st_ino == inode
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.parent.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_runtime_file_is_rejected_without_modifying_target(
    runtime, kind
):
    with private_runtime_file("setup.lock"):
        pass
    target = runtime / "preserve"
    target.write_text("untouched")
    path = runtime_path("unsafe.lock")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path)
    with pytest.raises(OSError):
        with private_runtime_file("unsafe.lock", truncate=True):
            pytest.fail("Unsafe runtime file was opened")
    assert target.read_text() == "untouched"


def test_symlink_runtime_directory_is_rejected(runtime):
    outside = runtime / "outside"
    outside.mkdir()
    (runtime / "tmp").symlink_to(outside)
    with pytest.raises(OSError):
        with private_runtime_file("task.lock"):
            pytest.fail("Symlink directory was accepted")
    assert not list(outside.iterdir())


def test_foreign_owned_runtime_directory_is_rejected(runtime, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: runtime.stat().st_uid + 1)
    with pytest.raises(PermissionError):
        with private_runtime_file("task.lock"):
            pytest.fail("Foreign directory was accepted")


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "..", ".", ""])
def test_runtime_filename_cannot_escape(runtime, name):
    with pytest.raises(ValueError):
        with private_runtime_file(name):
            pytest.fail("Escaping path was accepted")
