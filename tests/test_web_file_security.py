"""Exercise real ASGI routes without Helm, remote services or installed secrets."""

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
import pytest

from core.command import CommandResult
from web import main
from web.api import artifacts
from web.utils.auth import create_access_token


async def _request(method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.fixture
def static_tree(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("test application")
    (static / "umi.js").write_text("test root asset")
    (static / "chunks").mkdir()
    (static / "chunks" / "test.js").write_text("test nested asset")
    (tmp_path / "outside.txt").write_text("PRIVATE_TEST_MARKER")
    (static / "escaped.txt").symlink_to(tmp_path / "outside.txt")
    (static / "escaped-directory").symlink_to(tmp_path, target_is_directory=True)
    (static / "internal.js").symlink_to(static / "umi.js")
    monkeypatch.setattr(main, "STATIC_DIR", str(static))
    return static


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "test application"),
        ("/apps/editor", "test application"),
        ("/umi.js", "test root asset"),
        ("/chunks/test.js", "test nested asset"),
        ("/internal.js", "test root asset"),
    ],
)
def test_root_assets_and_spa_routes_remain_available(static_tree, path, expected):
    response = asyncio.run(_request("GET", path))
    assert response.status_code == 200
    assert response.text == expected


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/outside.txt",
        "/%2e%2e%2foutside.txt",
        "/escaped.txt",
        "/escaped-directory/outside.txt",
        "/%00.txt",
    ],
)
def test_static_paths_cannot_escape_the_root(static_tree, path):
    response = asyncio.run(_request("GET", path))
    assert response.status_code == 404
    assert "PRIVATE_TEST_MARKER" not in response.text


def test_absolute_static_path_is_rejected(static_tree):
    response = asyncio.run(
        _request("GET", "/%2F" + str(static_tree.parent / "outside.txt").lstrip("/"))
    )
    assert response.status_code == 404
    assert "PRIVATE_TEST_MARKER" not in response.text


def test_index_symlink_cannot_escape_the_static_root(static_tree):
    (static_tree / "index.html").unlink()
    (static_tree / "index.html").symlink_to(static_tree.parent / "outside.txt")
    for path in ("/", "/apps/editor"):
        assert asyncio.run(_request("GET", path)).status_code == 404


@pytest.fixture
def chart_environment(tmp_path, monkeypatch):
    original_temporary_directory = artifacts.tempfile.TemporaryDirectory

    def private_directory(**kwargs):
        return original_temporary_directory(dir=tmp_path, **kwargs)

    monkeypatch.setattr(artifacts.tempfile, "TemporaryDirectory", private_directory)
    monkeypatch.setattr(
        artifacts.Application,
        "REGISTRY",
        SimpleNamespace(USERNAME="configured-user", PASSWORD="configured-test-password"),
    )
    from core.orm.auth import AuthSession, RevokedToken
    from core.orm.engine import engine
    AuthSession.__table__.create(engine, checkfirst=True)
    RevokedToken.__table__.create(engine, checkfirst=True)
    token, _ = create_access_token({"sub": "admin"})
    return {"Authorization": f"Bearer {token}"}


def test_chart_filename_cannot_write_or_execute_outside_owned_temp_directory(
    tmp_path, monkeypatch, chart_environment
):
    original = tmp_path / "existing $(printf marker); chart.tgz"
    original.write_bytes(b"original")
    observed_paths = []
    request_thread = threading.get_ident()

    def fake_command(argv, *, env, timeout):
        assert isinstance(argv, list)
        assert argv[:2] == ["helm", "push"]
        path = Path(argv[2])
        assert path.name == "chart.tgz"
        assert path.parent.parent == tmp_path
        assert path.parent.stat().st_mode & 0o077 == 0
        assert path.read_bytes() == b"uploaded chart"
        assert str(original) not in argv
        assert argv[-4:] == [
            "--username", "configured-user", "--password", "configured-test-password"
        ]
        assert env == {"KUBECONFIG": "/etc/kubernetes/admin.conf"}
        assert threading.get_ident() != request_thread
        observed_paths.append(path)
        return CommandResult(0, "", "")

    monkeypatch.setattr(artifacts, "execute_command", fake_command)
    response = asyncio.run(
        _request(
            "POST", "/api/v1/artifacts/upload/chart", headers=chart_environment,
            files={"file": (str(original), b"uploaded chart", "application/gzip")},
        )
    )
    assert response.status_code == 200
    assert response.json()["code"] == 200
    assert "file_path" not in response.json()["data"]
    assert original.read_bytes() == b"original"
    assert len(observed_paths) == 1
    assert not observed_paths[0].parent.exists()


def test_concurrent_same_name_chart_uploads_keep_separate_files(
    tmp_path, monkeypatch, chart_environment
):
    barrier = threading.Barrier(2, timeout=5)
    observed = []

    def fake_command(argv, *, env, timeout):
        path = Path(argv[2])
        content = path.read_bytes()
        observed.append((path, content))
        barrier.wait()
        assert path.read_bytes() == content
        return CommandResult(0, "", "")

    monkeypatch.setattr(artifacts, "execute_command", fake_command)

    async def upload_both():
        return await asyncio.gather(
            *[
                _request(
                    "POST", "/api/v1/artifacts/upload/chart", headers=chart_environment,
                    files={"file": ("same.tgz", content, "application/gzip")},
                )
                for content in (b"first", b"second")
            ]
        )

    responses = asyncio.run(upload_both())
    assert all(response.status_code == 200 for response in responses)
    assert {content for _, content in observed} == {b"first", b"second"}
    assert len({path.parent for path, _ in observed}) == 2
    assert all(not path.parent.exists() for path, _ in observed)


def test_failed_chart_push_removes_temp_files_without_echoing_command_errors(
    tmp_path, monkeypatch, chart_environment
):
    observed_paths = []

    def fake_command(argv, *, env, timeout):
        observed_paths.append(Path(argv[2]))
        return CommandResult(1, "", "configured-test-password")

    monkeypatch.setattr(artifacts, "execute_command", fake_command)
    response = asyncio.run(
        _request(
            "POST", "/api/v1/artifacts/upload/chart", headers=chart_environment,
            files={"file": ("chart.tgz", b"uploaded chart", "application/gzip")},
        )
    )
    assert response.status_code == 500
    assert "configured-test-password" not in response.text
    assert not observed_paths[0].parent.exists()


@pytest.mark.parametrize(
    ("filename", "content"),
    [("chart.txt", b"invalid extension"), ("chart.tgz", b"x" * (2 * 1024 * 1024 + 1))],
    ids=["invalid-extension", "size-limit"],
)
def test_invalid_chart_upload_never_runs_helm(
    tmp_path, monkeypatch, chart_environment, filename, content
):
    def forbidden_command(*args, **kwargs):
        pytest.fail("invalid uploads must never run Helm")

    monkeypatch.setattr(artifacts, "execute_command", forbidden_command)
    response = asyncio.run(
        _request(
            "POST", "/api/v1/artifacts/upload/chart", headers=chart_environment,
            files={"file": (filename, content, "application/gzip")},
        )
    )
    assert response.status_code == (413 if filename == "chart.tgz" else 400)
    assert not list(tmp_path.iterdir())


def test_validation_errors_do_not_echo_password_inputs(monkeypatch):
    messages = []
    monkeypatch.setattr(main.logger, "warning", lambda message: messages.append(message))
    marker = "private-validation-test-marker"
    response = asyncio.run(
        _request("POST", "/api/v1/login", json={"username": "admin", "password": {"secret": marker}})
    )
    assert response.status_code == 422
    assert marker not in response.text
    assert messages
    assert all(marker not in message for message in messages)
