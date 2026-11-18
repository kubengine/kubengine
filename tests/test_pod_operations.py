"""Pod exec/文件传输的路径、字节完整性、鉴权及连接生命周期。"""

import asyncio
import base64
import io
import json
import subprocess
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes import client

from core.http_api_client.pod_operations import (
    PodOperationError,
    PodOperations,
    PodTarget,
    file_name,
    file_path,
    fs_command,
    shell_command,
)
from core.upload_limits import upload_limit

LINUX = PodTarget("apps", "example", "main", False)
WINDOWS = PodTarget("apps", "example", "main", True)


@pytest.fixture
def pod_limits(monkeypatch):
    from core import upload_limits

    settings = {}
    monkeypatch.setattr(
        upload_limits,
        "ConfigDict",
        SimpleNamespace(
            get_instance=lambda: {"security": {"uploads": settings}}
        ),
    )
    return settings


def test_pod_file_limits_default_and_configured_values(pod_limits):
    assert upload_limit("pod_file_bytes") == 100 * 1024**2
    assert upload_limit("pod_timeout_seconds") == 300
    pod_limits.update(pod_file_bytes=1024**3, pod_timeout_seconds=1800)
    assert upload_limit("pod_file_bytes") == 1024**3
    assert upload_limit("pod_timeout_seconds") == 1800


@pytest.mark.parametrize("name", ["pod_file_bytes", "pod_timeout_seconds"])
@pytest.mark.parametrize("value", [0, -1, True, "100", None, 1.5])
def test_pod_file_limits_require_positive_integers(pod_limits, name, value):
    pod_limits[name] = value
    with pytest.raises(ValueError, match=f"security.uploads.{name}"):
        upload_limit(name)


def local_execute(target, op, path, data=b"", **options):
    result = subprocess.run(
        fs_command(target, op, str(path), **options),
        input=data,
        capture_output=True,
        timeout=10,
    )
    if result.returncode:
        import re

        match = re.search(rb"KUBEENGINE_FS:(\d+):([^\n]+)", result.stderr)
        raise PodOperationError(
            int(match[1]) if match else 502, result.stderr.decode()
        )
    return result.stdout.decode()


@pytest.fixture
def local_ops():
    ops = PodOperations(MagicMock())

    def run(target, command, source=None, **kwargs):
        data = base64.b64encode(source.read()) if source else b""
        result = subprocess.run(
            command, input=data, capture_output=True, timeout=10
        )
        if result.returncode:
            import re

            match = re.search(rb"KUBEENGINE_FS:(\d+):([^\n]+)", result.stderr)
            raise PodOperationError(
                int(match[1]) if match else 502, result.stderr.decode()
            )
        return result.stdout.decode()

    ops.run = run
    return ops


@pytest.mark.parametrize(
    "path,windows",
    [
        ("/", False),
        ("//", False),
        ("/tmp/..", False),
        ("C:\\", True),
        ("c:/", True),
        ("D:\\tmp\\..", True),
    ],
)
def test_mutations_reject_all_root_spellings(path, windows):
    with pytest.raises(PodOperationError):
        file_path(path, windows, mutation=True)
    assert file_path(path, windows)


@pytest.mark.parametrize(
    "name", ["", ".", "..", "../escape", "dir/file", "dir\\file", "bad\0name"]
)
def test_new_names_cannot_escape_current_directory(name):
    with pytest.raises(PodOperationError):
        file_name(name, False)


@pytest.mark.parametrize(
    "name",
    ["CON", "aux.txt", "NUL", "COM1", "bad:stream", "trailing.", "bad?file"],
)
def test_windows_rejects_reserved_devices_and_streams(name):
    with pytest.raises(PodOperationError):
        file_name(name, True)


def test_posix_listing_preserves_unicode_newline_and_shell_metacharacters(
    tmp_path, local_ops
):
    names = [
        "普通文件.txt",
        "two words",
        "quote'and$()",
        "line\nbreak",
        "-leading",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"file")
    (tmp_path / "folder").mkdir()
    value = local_ops.listing(LINUX, str(tmp_path))
    assert value["path"] == str(tmp_path)
    assert value["entries"][0]["name"] == "folder"
    assert {e["name"] for e in value["entries"]} == set(names + ["folder"])
    assert all(e["size"] == 4 for e in value["entries"] if e["type"] == "file")


def test_create_rename_and_delete_use_literal_names(tmp_path, local_ops):
    path = tmp_path / "$(touch INJECTED)';.txt"
    local_ops.operation(LINUX, "touch", str(path))
    assert path.is_file() and not (tmp_path / "INJECTED").exists()
    with pytest.raises(PodOperationError) as exists:
        local_ops.operation(LINUX, "touch", str(path))
    assert exists.value.status == 409
    local_ops.operation(LINUX, "rename", str(path), name="renamed 文件.txt")
    renamed = tmp_path / "renamed 文件.txt"
    assert renamed.is_file() and not path.exists()
    local_ops.operation(LINUX, "delete", str(renamed))
    assert not renamed.exists()


def test_recursive_delete_requires_opt_in_and_does_not_follow_links(
    tmp_path, local_ops
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    tree = tmp_path / "tree"
    local_ops.operation(LINUX, "mkdir", str(tree))
    (tree / "file").write_text("test")
    (tree / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PodOperationError):
        local_ops.operation(LINUX, "delete", str(tree))
    assert tree.exists()
    local_ops.operation(LINUX, "delete", str(tree), recursive=True)
    assert not tree.exists() and (outside / "keep").read_text() == "keep"


@pytest.mark.parametrize("size", [0, 1, 2, 3, 65535, 150000])
def test_upload_preserves_arbitrary_binary_bytes_and_cleans_temporary_files(
    tmp_path, local_ops, size
):
    data = (bytes(range(256)) * ((size + 255) // 256))[:size]
    result = local_ops.upload(
        LINUX, str(tmp_path), "binary.bin", io.BytesIO(data), size
    )
    assert (tmp_path / "binary.bin").read_bytes() == data and result[
        "size"
    ] == size
    assert not list(tmp_path.glob(".kubengine-upload.*"))


def test_upload_default_refuses_existing_and_explicit_overwrite_preserves_mode(
    tmp_path, local_ops
):
    path = tmp_path / "file"
    path.write_text("original")
    path.chmod(0o600)
    with pytest.raises(PodOperationError) as duplicate:
        local_ops.upload(LINUX, str(tmp_path), "file", io.BytesIO(b"new"), 3)
    assert duplicate.value.status == 409 and path.read_text() == "original"
    local_ops.upload(
        LINUX, str(tmp_path), "file", io.BytesIO(b"new"), 3, overwrite=True
    )
    assert path.read_bytes() == b"new" and path.stat().st_mode & 0o777 == 0o600


def test_upload_incomplete_data_never_replaces_existing_file(
    tmp_path, local_ops
):
    path = tmp_path / "file"
    path.write_text("original")
    with pytest.raises(PodOperationError):
        local_ops.upload(
            LINUX, str(tmp_path), "file", io.BytesIO(b"x"), 100, overwrite=True
        )
    assert path.read_text() == "original" and not list(
        tmp_path.glob(".kubengine-upload.*")
    )


def test_upload_refuses_symlink_and_download_refuses_non_files(
    tmp_path, local_ops
):
    path = tmp_path / "file"
    path.write_text("original")
    (tmp_path / "link").symlink_to(path)
    with pytest.raises(PodOperationError):
        local_ops.upload(
            LINUX, str(tmp_path), "link", io.BytesIO(b"new"), 3, overwrite=True
        )
    assert path.read_text() == "original"
    with pytest.raises(PodOperationError):
        local_ops.download_size(LINUX, str(tmp_path / "link"))
    with pytest.raises(PodOperationError):
        local_ops.download_size(LINUX, str(tmp_path))


def test_upload_limit_rejects_before_opening_exec(local_ops, tmp_path):
    with pytest.raises(PodOperationError):
        local_ops.upload(
            LINUX,
            str(tmp_path),
            "large",
            io.BytesIO(),
            upload_limit("pod_file_bytes") + 1,
        )
    assert not list(tmp_path.iterdir())


def test_listing_exposes_configured_limits(local_ops, tmp_path, pod_limits):
    pod_limits.update(pod_file_bytes=1024**3, pod_timeout_seconds=1800)
    result = local_ops.listing(LINUX, str(tmp_path))
    assert result["maxFileBytes"] == 1024**3
    assert result["transferTimeoutSeconds"] == 1800


def test_upload_above_old_limit_uses_configured_timeout(pod_limits):
    pod_limits.update(pod_file_bytes=1024**3, pod_timeout_seconds=1800)
    ops = PodOperations(MagicMock())
    ops.run = MagicMock()
    source = io.BytesIO()
    size = 100 * 1024**2 + 1
    assert ops.upload(LINUX, "/tmp", "large", source, size)["size"] == size
    assert ops.run.call_args.args[2] is source
    assert ops.run.call_args.kwargs["timeout"] == 1800
    with pytest.raises(PodOperationError) as error:
        ops.upload(LINUX, "/tmp", "large", source, 1024**3 + 1)
    assert error.value.status == 413
    assert "1073741824" in error.value.reason


def test_download_stat_enforces_configured_limit(
    local_ops, tmp_path, pod_limits
):
    pod_limits["pod_file_bytes"] = 8
    path = tmp_path / "file"
    path.write_bytes(b"12345678")
    assert local_ops.download_size(LINUX, str(path)) == 8
    path.write_bytes(b"123456789")
    with pytest.raises(PodOperationError) as error:
        local_ops.download_size(LINUX, str(path))
    assert error.value.status == 413


def test_download_stream_enforces_configured_limit(pod_limits):
    pod_limits["pod_file_bytes"] = 8
    socket = MagicMock()
    socket.read_stdout.return_value = base64.b64encode(b"123456789").decode()
    socket.read_stderr.return_value = ""

    @contextmanager
    def session(*args, **kwargs):
        yield socket

    ops = PodOperations(MagicMock())
    ops.exec_session = session
    with pytest.raises(PodOperationError) as error:
        list(ops.download(LINUX, "/tmp/file"))
    assert error.value.status == 413


def test_download_stream_uses_configured_timeout(pod_limits, monkeypatch):
    from core.http_api_client import pod_operations

    pod_limits["pod_timeout_seconds"] = 1800
    ticks = iter([0, 1801])
    monkeypatch.setattr(
        pod_operations, "time", SimpleNamespace(monotonic=lambda: next(ticks))
    )

    @contextmanager
    def session(*args, **kwargs):
        yield MagicMock()

    ops = PodOperations(MagicMock())
    ops.exec_session = session
    with pytest.raises(PodOperationError) as error:
        list(ops.download(LINUX, "/tmp/file"))
    assert error.value.status == 504


def test_powershell_arguments_are_encoded_data_not_interpolated_commands():
    path = "C:\\tmp\\a';$(Write-Host injected).txt"
    command = fs_command(WINDOWS, "touch", path)
    assert command[:4] == [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
    ]
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    import re

    encoded = re.search(r"FromBase64String\('([^']+)'", script)[1]
    assert json.loads(base64.b64decode(encoded))["path"] == path
    assert path not in script
    assert shell_command(WINDOWS, "powershell")[0] == "powershell.exe"
    assert shell_command(LINUX, "powershell")[0] == "pwsh"
    with pytest.raises(PodOperationError):
        shell_command(LINUX, "sh -c evil")


def test_target_checks_pod_container_and_windows_node():
    view = MagicMock()
    raw = {
        "spec": {"nodeName": "windows-node", "containers": [{"name": "main"}]},
        "status": {"phase": "Running"},
    }
    view._read.side_effect = [
        raw,
        {"status": {"nodeInfo": {"operatingSystem": "windows"}}},
    ]
    assert PodOperations(view).target("apps", "example", "main").windows
    view._read.side_effect = None
    view._read.return_value = raw
    with pytest.raises(PodOperationError):
        PodOperations(view).target("apps", "example", "foreign")
    view._read.return_value = {"spec": {}, "status": {"phase": "Succeeded"}}
    with pytest.raises(PodOperationError):
        PodOperations(view).target("apps", "example", "main")


def test_exec_owns_transport_and_closes_it_without_mutating_shared_sdk():
    api = client.ApiClient(
        client.Configuration(host="https://cluster.invalid")
    )
    view = SimpleNamespace(api_client=api)
    original = api.call_api
    ws = MagicMock()
    with patch(
        "core.http_api_client.pod_operations.stream", return_value=ws
    ) as transport:
        with PodOperations(view).exec_session(
            LINUX, ["sh"], stdin=True, tty=True
        ) as opened:
            assert opened is ws
            method = transport.call_args.args[0]
            assert method.__self__.api_client is not api
            assert api.call_api == original
            assert (
                transport.call_args.kwargs["tty"]
                and not transport.call_args.kwargs["stderr"]
            )
    ws.close.assert_called_once()
    api.close()


class ExecSocket:
    def __init__(self):
        self.open = True
        self.output = "$ "
        self.closed = threading.Event()
        self.sizes = []

    def is_open(self):
        return self.open

    def update(self, timeout=0):
        self.closed.wait(min(timeout, 0.01))

    def read_stdout(self, timeout=0):
        assert timeout == 0
        value = self.output
        self.output = ""
        return value

    def write_stdin(self, value):
        self.output += "executed:" + value
        if value == "exit\r":
            self.open = False

    def write_channel(self, channel, value):
        self.sizes.append((channel, json.loads(value)))

    def read_channel(self, channel, timeout=0):
        return json.dumps({"status": "Success"}) if channel == 3 else ""

    @property
    def returncode(self):
        return 0

    def close(self):
        self.open = False
        self.closed.set()


@pytest.fixture
def api_app(monkeypatch):
    from web.api import pod_operations as routes
    from web.utils import auth

    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/k8s")
    socket = ExecSocket()

    class Ops:
        def target(self, namespace, name, container):
            return PodTarget(namespace, name, container, False)

        @contextmanager
        def exec_session(self, *args, **kwargs):
            try:
                yield socket
            finally:
                socket.close()

        def listing(self, *args):
            return {"path": "/tmp", "entries": [], "windows": False}

        def operation(self, *args, **kwargs):
            return {"action": args[1]}

        def upload(self, *args, **kwargs):
            return {"size": args[4], "path": "/tmp/file"}

        def download_size(self, *args):
            return 3

        def download(self, *args):
            yield b"\x00\xffx"

    ops = Ops()
    monkeypatch.setattr(routes, "get_operations", lambda: ops)
    valid = {"active": True}
    original_auth = auth.get_current_user

    async def identity(authorization=None, **kwargs):
        from fastapi import HTTPException

        if authorization != "Bearer valid" or not valid["active"]:
            raise HTTPException(401, "invalid")
        return SimpleNamespace(username="admin"), "test-identity"

    monkeypatch.setattr(auth, "get_current_user", identity)

    # Binary download uses its own Depends endpoint.
    async def download_identity():
        return await identity("Bearer valid")

    yield app, socket, valid, original_auth, download_identity


def test_terminal_auth_input_resize_exit_and_cleanup(api_app):
    app, socket, _, _, _ = api_app
    with TestClient(app) as http:
        with http.websocket_connect(
            "/api/v1/k8s/pods/apps/example/terminal?container=main"
        ) as ws:
            ws.send_json({"type": "auth", "token": "Bearer valid"})
            while ws.receive_json()["type"] != "ready":
                pass
            ws.send_json({"type": "input", "data": "echo test\r"})
            while "echo test" not in str(ws.receive_json()):
                pass
            ws.send_json({"type": "resize", "cols": 100, "rows": 40})
            ws.send_json({"type": "input", "data": "exit\r"})
            while ws.receive_json()["type"] != "exit":
                pass
    assert socket.closed.wait(2) and socket.sizes == [
        (4, {"Width": 100, "Height": 40})
    ]


def test_terminal_rejects_missing_auth_and_revoked_session(api_app):
    app, socket, valid, _, _ = api_app
    with TestClient(app) as http:
        with http.websocket_connect(
            "/api/v1/k8s/pods/apps/example/terminal?container=main"
        ) as ws:
            ws.send_json({"type": "input", "data": "id\r"})
            assert ws.receive_json()["type"] == "error"
        with http.websocket_connect(
            "/api/v1/k8s/pods/apps/example/terminal?container=main"
        ) as ws:
            ws.send_json({"type": "auth", "token": "Bearer valid"})
            while ws.receive_json()["type"] != "ready":
                pass
            valid["active"] = False
            ws.send_json({"type": "input", "data": "id\r"})
            while True:
                value = ws.receive_json()
                if value["type"] == "error":
                    break
            assert value["message"] == "invalid"
    assert socket.closed.wait(2) and "executed:id" not in socket.output


def test_files_http_auth_mutations_upload_and_binary_download(api_app):
    app, _, _, original_auth, identity = api_app

    async def execute():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            url = "/api/v1/k8s/pods/apps/example/files"
            assert (await http.get(url)).status_code == 401
            assert (
                await http.post(
                    url + "/operations",
                    json={"action": "touch", "path": "/tmp/file"},
                )
            ).status_code == 401
            assert (
                await http.post(
                    url + "/upload", files={"file": ("file", b"x")}
                )
            ).status_code == 401
            assert (
                await http.get(url + "/download", params={"path": "/tmp/file"})
            ).status_code == 401
            http.headers["Authorization"] = "Bearer valid"
            assert (await http.get(url)).json()["data"]["path"] == "/tmp"
            assert (
                await http.post(
                    url + "/operations",
                    json={"action": "mkdir", "path": "/tmp/folder"},
                )
            ).json()["code"] == 200
            assert (
                await http.post(
                    url + "/operations",
                    json={"action": "unsupported", "path": "/tmp/folder"},
                )
            ).status_code == 422
            result = await http.post(
                url + "/upload",
                params={"container": "main"},
                data={"path": "/tmp"},
                files={"file": ("file", b"\x00\xffx")},
            )
            assert result.json()["data"]["size"] == 3
            app.dependency_overrides[original_auth] = identity
            result = await http.get(
                url + "/download", params={"path": "/tmp/file"}
            )
            assert (
                result.content == b"\x00\xffx"
                and result.headers["content-type"]
                == "application/octet-stream"
            )

    asyncio.run(execute())


def test_http_upload_uses_configured_size_and_receive_timeout(
    api_app, pod_limits, monkeypatch
):
    from web.utils import uploads

    app, _, _, _, _ = api_app
    pod_limits.update(pod_file_bytes=8, pod_timeout_seconds=17)
    timeouts = []

    async def wait_for(awaitable, timeout):
        timeouts.append(timeout)
        return await asyncio.wait_for(awaitable, timeout)

    monkeypatch.setattr(
        uploads,
        "asyncio",
        SimpleNamespace(wait_for=wait_for, TimeoutError=asyncio.TimeoutError),
    )
    monkeypatch.setattr(uploads, "time", SimpleNamespace(monotonic=lambda: 0))

    async def execute():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer valid"},
        ) as http:
            url = "/api/v1/k8s/pods/apps/example/files/upload"
            result = await http.post(
                url,
                data={"path": "/tmp"},
                files={"file": ("file", b"12345678")},
            )
            assert result.json()["data"]["size"] == 8
            result = await http.post(
                url,
                data={"path": "/tmp"},
                files={"file": ("file", b"123456789")},
            )
            assert result.status_code == 413

            async def chunks():
                yield (
                    b"--boundary\r\nContent-Disposition: form-data; "
                    b'name="file"; filename="large"\r\n\r\n'
                )
                yield b"x" * 70000
                yield b"\r\n--boundary--\r\n"

            result = await http.post(
                url,
                content=chunks(),
                headers={
                    "Content-Type": "multipart/form-data; boundary=boundary"
                },
            )
            assert result.status_code == 413

    asyncio.run(execute())
    assert timeouts and all(value == 17 for value in timeouts)


def test_upload_polls_stdout_and_stderr_without_blocking_between_chunks():
    data = bytes(range(256)) * 1000
    expected = base64.b64encode(data)

    class Socket:
        def __init__(self):
            self.received = b""

        def is_open(self):
            return len(self.received) < len(expected)

        def write_stdin(self, value):
            self.received += value

        def update(self, timeout=0):
            pass

        def read_stdout(self, timeout=None):
            assert timeout == 0, "SDK defaults to a blocking read"
            return ""

        def read_stderr(self, timeout=None):
            assert timeout == 0, "stderr is usually empty during upload"
            return ""

        @property
        def returncode(self):
            return 0

    socket = Socket()

    @contextmanager
    def session(*args, **kwargs):
        yield socket

    ops = PodOperations(MagicMock())
    ops.exec_session = session
    assert ops.run(LINUX, ["test"], io.BytesIO(data)) == ""
    assert socket.received == expected


def test_download_reassembles_base64_across_arbitrary_frame_boundaries():
    data = bytes(range(256)) * 100
    encoded = base64.encodebytes(data).decode()
    frames = [
        encoded[offset : offset + 17] for offset in range(0, len(encoded), 17)
    ]

    class Socket:
        def is_open(self):
            return bool(frames)

        def update(self, timeout=0):
            pass

        def read_stdout(self, timeout=None):
            assert timeout == 0
            return frames.pop(0) if frames else ""

        def read_stderr(self, timeout=None):
            assert timeout == 0
            return ""

        @property
        def returncode(self):
            return 0

    @contextmanager
    def session(*args, **kwargs):
        yield Socket()

    ops = PodOperations(MagicMock())
    ops.exec_session = session
    assert b"".join(ops.download(LINUX, "/tmp/file")) == data


def test_missing_shell_has_actionable_websocket_error(api_app, monkeypatch):
    from kubernetes.client.exceptions import ApiException

    from web.api import pod_operations as routes

    app, _, _, _, _ = api_app
    ops = routes.get_operations()

    @contextmanager
    def missing(*args, **kwargs):
        raise ApiException(
            status=500,
            reason='exec: "pwsh": executable file not found in $PATH',
        )
        yield

    monkeypatch.setattr(ops, "exec_session", missing)
    with TestClient(app) as http:
        with http.websocket_connect(
            "/api/v1/k8s/pods/apps/example/terminal"
            "?container=main&shell=powershell"
        ) as ws:
            ws.send_json({"type": "auth", "token": "Bearer valid"})
            message = ws.receive_json()
            assert message["type"] == "error"
            assert (
                "pwsh" in message["message"]
                and "切换 Shell" in message["message"]
            )


def test_terminal_preserves_utf8_split_between_websocket_frames(
    api_app, monkeypatch
):
    from web.api import pod_operations as routes

    app, _, _, _, _ = api_app

    class SplitSocket(ExecSocket):
        def __init__(self):
            super().__init__()
            self.frames = [b"\xe4\xb8", b"\xad"]
            self.output = b""

        def update(self, timeout=0):
            self.output = self.frames.pop(0)
            if not self.frames:
                self.open = False

        def read_stdout(self, timeout=0):
            value = self.output
            self.output = b""
            return value

    socket = SplitSocket()

    @contextmanager
    def session(*args, **kwargs):
        assert kwargs["binary"] is True
        try:
            yield socket
        finally:
            socket.close()

    monkeypatch.setattr(routes.get_operations(), "exec_session", session)
    output = b""
    with TestClient(app) as http:
        with http.websocket_connect(
            "/api/v1/k8s/pods/apps/example/terminal?container=main"
        ) as ws:
            ws.send_json({"type": "auth", "token": "Bearer valid"})
            while True:
                value = ws.receive_json()
                if value["type"] == "output":
                    assert value["encoding"] == "base64"
                    output += base64.b64decode(value["data"])
                if value["type"] == "exit":
                    break
    assert output.decode() == "中" and socket.closed.wait(1)


def test_running_init_container_is_accessible_while_pod_is_pending():
    view = MagicMock()
    raw = {
        "spec": {
            "os": {"name": "linux"},
            "containers": [{"name": "main"}],
            "initContainers": [{"name": "setup"}],
        },
        "status": {
            "phase": "Pending",
            "initContainerStatuses": [
                {
                    "name": "setup",
                    "state": {
                        "running": {"startedAt": "2026-01-01T00:00:00Z"}
                    },
                }
            ],
        },
    }
    view._read.return_value = raw
    assert (
        PodOperations(view).target("apps", "example", "setup").container
        == "setup"
    )
    with pytest.raises(PodOperationError):
        PodOperations(view).target("apps", "example", "main")
