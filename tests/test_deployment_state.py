"""CLI checkpoint tests without importing the gevent command runtime."""

import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import core.deployment_inputs as deployment_inputs
from core.config import Application
from core.deployment_inputs import DeploymentInputs
from core.deployment_state import (
    DeploymentState,
    DeploymentStateError,
    deployment_lock,
)


def test_corrupt_or_unknown_state_never_becomes_first_deployment(tmp_path):
    path = tmp_path / "state.json"
    for content in (
        "{broken",
        "{}",
        "[]",
        '{"schema_version":999}',
        '{"completed_files":"bad"}',
    ):
        path.write_text(content)
        with pytest.raises(DeploymentStateError):
            DeploymentState(path)
        assert path.read_text() == content


def test_checkpoint_commits_digest_and_result_together(tmp_path):
    state = DeploymentState(tmp_path / "state.json")
    owner = state.mark_file_running("install.py", "digest")
    state.mark_file_completed("install.py", "digest", owner)
    read = DeploymentState(state.state_file)
    assert (
        read.is_file_completed("install.py")
        and read.get_file_hash("install.py") == "digest"
    )
    assert read.state["schema_version"] == 2
    assert stat.S_IMODE(state.state_file.stat().st_mode) == 0o600


def test_failed_retry_cannot_keep_old_success_marker(tmp_path):
    state = DeploymentState(tmp_path / "state.json")
    state.mark_file_completed("install.py", "old")
    owner = state.mark_file_running("install.py", "new")
    state.mark_file_failed("install.py", owner)
    assert not DeploymentState(state.state_file).is_file_completed(
        "install.py"
    )


def test_stale_checkpoint_writer_is_rejected(tmp_path):
    state = DeploymentState(tmp_path / "state.json")
    old = state.mark_file_running("install.py", "first")
    new = state.mark_file_running("install.py", "second")
    with pytest.raises(DeploymentStateError):
        state.mark_file_completed("install.py", "first", old)
    state.mark_file_completed("install.py", "second", new)


def test_atomic_replace_failure_preserves_previous_state(
    tmp_path, monkeypatch
):
    state = DeploymentState(tmp_path / "state.json")
    state.set_config_hash("original")
    original = state.state_file.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        state.set_config_hash("new")
    assert state.state_file.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_separate_state_objects_merge_updates_without_lost_checkpoints(
    tmp_path,
):
    path = tmp_path / "state.json"
    barrier = threading.Barrier(2)

    def save(name):
        state = DeploymentState(path)
        barrier.wait()
        state.mark_file_completed(name, "digest")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, ["one.py", "two.py"]))
    assert set(DeploymentState(path).state["completed_files"]) == {
        "one.py",
        "two.py",
    }


def test_deploy_scale_and_reset_share_exclusion(tmp_path, monkeypatch):
    monkeypatch.setattr(Application, "ROOT_DIR", str(tmp_path))
    with deployment_lock():
        with pytest.raises(DeploymentStateError):
            with deployment_lock():
                pytest.fail("concurrent mutation acquired lock")
    with deployment_lock():
        pass


def test_content_digest_tracks_same_mtime_changes_and_dependencies(tmp_path):
    script = tmp_path / "infra/install_metallb.py"
    script.parent.mkdir()
    script.write_text("install version one")
    helper = script.parent / "_helper.py"
    helper.write_text("helper one")
    root = tmp_path / "offline"
    (root / "charts/metallb").mkdir(parents=True)
    values = root / "charts/metallb/values.yaml.j2"
    values.write_text("template one")
    inputs = DeploymentInputs()

    def fingerprint():
        return inputs.fingerprint(script, root, {"node": "one"})

    first = fingerprint()
    previous = script.stat()
    script.write_text("install version two")
    os.utime(script, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    second = fingerprint()
    assert first != second
    helper.write_text("helper two")
    third = fingerprint()
    assert third != second
    values.write_text("template two")
    assert fingerprint() != third
    current = fingerprint()
    (root / "charts/metallb/values.yaml").write_text("rendered output")
    assert fingerprint() == current


def test_recent_input_rewrite_with_identical_metadata_is_rehashed(
    tmp_path, monkeypatch
):
    script = tmp_path / "install_metallb.py"
    script.write_text("version one")
    metadata = script.stat()
    original_stat = Path.stat

    def frozen_stat(path, *args, **kwargs):
        if path == script:
            return metadata
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", frozen_stat)
    monkeypatch.setattr(
        deployment_inputs.time,
        "time_ns",
        lambda: metadata.st_ctime_ns + 500_000_000,
    )
    inputs = DeploymentInputs()
    first = inputs.fingerprint(script, tmp_path, {})
    script.write_text("version two")
    assert script.stat() == metadata
    assert inputs.fingerprint(script, tmp_path, {}) != first


def test_stable_archive_digest_is_cached_across_components(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive.tar"
    archive.write_bytes(b"synthetic offline image archive")
    metadata = archive.stat()
    monkeypatch.setattr(
        deployment_inputs.time,
        "time_ns",
        lambda: metadata.st_ctime_ns + 2_000_000_000,
    )
    original_open = Path.open
    reads = []

    def track_reads(path, *args, **kwargs):
        if path == archive:
            reads.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track_reads)
    inputs = DeploymentInputs()
    first = inputs._digest(archive)
    assert inputs._digest(archive) == first
    assert reads == [archive]
