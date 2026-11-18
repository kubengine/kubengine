"""Pod 交互终端及文件操作，使用独立 SDK exec 连接。"""

from __future__ import annotations

import base64
import copy
import io
import json
import ntpath
import posixpath
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, cast

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream
from urllib3.exceptions import HTTPError
from websocket import WebSocketException

from core.upload_limits import upload_limit

from .kubernetes_view_client import KubernetesViewClient

MAX_EXEC_OUTPUT = 8 * 1024**2
SCRIPTS = Path(__file__).with_name("pod_scripts")


class _DiscardOutput(io.StringIO):
    """Discard the SDK's duplicate transcript for streamed sessions."""

    def write(self, value: str) -> int:
        return len(value)


class PodOperationError(ApiException):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(status=code, reason=message)


@dataclass(frozen=True)
class PodTarget:
    namespace: str
    name: str
    container: str
    windows: bool


def file_path(value: str, windows: bool, *, mutation: bool = False) -> str:
    if not value or "\x00" in value or len(value) > 4096:
        raise PodOperationError(400, "路径为空、过长或包含无效字符")
    if windows:
        if not re.match(r"^[A-Za-z]:[\\/]", value):
            raise PodOperationError(
                400, "Windows 路径必须以盘符开头，例如 C:\\"
            )
        value = ntpath.normpath(value)
        root = ntpath.splitdrive(value)[0] + "\\"
        if ":" in value[2:]:
            raise PodOperationError(400, "不支持设备路径或备用数据流")
    else:
        if not value.startswith("/"):
            raise PodOperationError(400, "必须使用绝对路径")
        value = posixpath.normpath(value)
        # Normalize the alternate POSIX root spelling //.
        value = "/" + value.lstrip("/")
        root = "/"
    if mutation and value.rstrip("\\/") == root.rstrip("\\/"):
        raise PodOperationError(400, "不能修改或删除根目录")
    return value


def file_name(value: str, windows: bool) -> str:
    if (
        not value
        or value in {".", ".."}
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or len(value) > 255
    ):
        raise PodOperationError(400, "名称不能为空或包含路径分隔符")
    if windows and (
        any(char in value for char in '<>:"|?*')
        or value.endswith((" ", "."))
        or re.fullmatch(
            r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", value
        )
    ):
        raise PodOperationError(400, "Windows 文件名无效或属于保留名称")
    return value


def fs_command(
    target: PodTarget, op: str, path: str, **options: Any
) -> list[str]:
    if op not in {
        "list",
        "stat",
        "download",
        "touch",
        "mkdir",
        "delete",
        "rename",
        "upload",
    }:
        raise PodOperationError(400, "不支持的文件操作")
    path = file_path(
        path, target.windows, mutation=op not in {"list", "stat", "download"}
    )
    if op == "rename":
        options["name"] = file_name(options.get("name", ""), target.windows)
    if target.windows:
        args = base64.b64encode(
            json.dumps({"op": op, "path": path, **options}).encode()
        ).decode()
        prelude = (
            "$a = [Text.Encoding]::UTF8.GetString("
            "[Convert]::FromBase64String('" + args + "')) | ConvertFrom-Json\n"
        )
        script = prelude + (SCRIPTS / "files.ps1").read_text()
        return [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode(),
        ]
    extra = (
        str(options.get("encoded_size", ""))
        if op == "upload"
        else (
            str(options.get("recursive", False)).lower()
            if op == "delete"
            else options.get("name", "")
        )
    )
    return [
        "sh",
        "-c",
        (SCRIPTS / "files.sh").read_text(),
        "kubengine-files",
        op,
        path,
        extra,
        str(options.get("overwrite", False)).lower(),
        str(options.get("size", 0)),
    ]


def exec_returncode(ws: Any, stderr: str) -> None:
    try:
        code = ws.returncode
    except (KeyError, TypeError, ValueError):
        raise PodOperationError(
            502, stderr or "exec 会话缺少退出状态"
        ) from None
    if code != 0:
        marker = re.search(r"KUBEENGINE_FS:(\d+):([^\r\n]+)", stderr)
        if marker:
            raise PodOperationError(int(marker[1]), marker[2])
        raise PodOperationError(
            502, (stderr or f"容器命令退出码 {code}")[:2000]
        )


class PodOperations:
    def __init__(self, view: KubernetesViewClient) -> None:
        self.view = view

    def target(
        self, namespace: str, name: str, container: str | None
    ) -> PodTarget:
        raw = self.view._read("pod", namespace, name)
        spec, status = raw.get("spec") or {}, raw.get("status") or {}
        if status.get("phase") not in {"Running", "Pending"}:
            raise PodOperationError(
                409, "Pod 当前未运行，无法连接终端或文件管理器"
            )
        containers = (
            (spec.get("containers") or [])
            + (spec.get("initContainers") or [])
            + (spec.get("ephemeralContainers") or [])
        )
        selected = container or next(
            (c["name"] for c in spec.get("containers") or []), ""
        )
        if not selected or selected not in {c["name"] for c in containers}:
            raise PodOperationError(400, "指定容器不属于该 Pod")
        states = (
            (status.get("containerStatuses") or [])
            + (status.get("initContainerStatuses") or [])
            + (status.get("ephemeralContainerStatuses") or [])
        )
        current = next((s for s in states if s.get("name") == selected), None)
        if (
            current is not None
            and (current.get("state") or {}).get("running") is None
        ) or (status.get("phase") == "Pending" and current is None):
            raise PodOperationError(409, "所选容器当前未运行")
        os_name = (spec.get("os") or {}).get("name") or (
            spec.get("nodeSelector") or {}
        ).get("kubernetes.io/os")
        if not os_name and spec.get("nodeName"):
            node = self.view._read("node", None, spec["nodeName"])
            os_name = ((node.get("status") or {}).get("nodeInfo") or {}).get(
                "operatingSystem"
            )
        return PodTarget(namespace, name, selected, os_name == "windows")

    @contextmanager
    def exec_session(
        self,
        target: PodTarget,
        command: list[str],
        *,
        stdin: bool = False,
        tty: bool = False,
        binary: bool = False,
    ) -> Iterator[Any]:
        # Keep the SDK stream transport isolated from resource queries.
        api = client.ApiClient(
            configuration=copy.copy(self.view.api_client.configuration)
        )
        ws = None
        try:
            ws = stream(
                client.CoreV1Api(api).connect_get_namespaced_pod_exec,
                target.name,
                target.namespace,
                container=target.container,
                command=command,
                stdin=stdin,
                stdout=True,
                stderr=not tty,
                tty=tty,
                _preload_content=False,
                _request_timeout=20,
                binary=binary,
            )
            ws._all = _DiscardOutput()
            yield ws
        except (HTTPError, OSError, WebSocketException) as exc:
            raise PodOperationError(503, "无法连接容器 exec 会话") from exc
        finally:
            if ws is not None:
                ws.close()
            api.close()  # type: ignore[no-untyped-call]

    def run(
        self,
        target: PodTarget,
        command: list[str],
        source: BinaryIO | None = None,
        *,
        timeout: int = 45,
    ) -> str:
        stdout, stderr = "", ""
        deadline = time.monotonic() + timeout
        input_done = source is None
        with self.exec_session(
            target, command, stdin=source is not None
        ) as ws:
            while ws.is_open():
                if time.monotonic() > deadline:
                    raise PodOperationError(504, "容器文件操作超时")
                if not input_done and source is not None:
                    chunk = source.read(49152)
                    if chunk:
                        ws.write_stdin(base64.b64encode(chunk))
                    else:
                        input_done = True
                ws.update(timeout=0.05 if input_done else 0)
                stdout += ws.read_stdout(timeout=0)
                stderr += ws.read_stderr(timeout=0)
                if len(stdout) > MAX_EXEC_OUTPUT or len(stderr) > 16384:
                    raise PodOperationError(413, "容器命令响应过大")
            stdout += ws.read_stdout(timeout=0)
            stderr += ws.read_stderr(timeout=0)
            exec_returncode(ws, stderr)
        return cast(str, stdout)

    def listing(self, target: PodTarget, path: str | None) -> dict[str, Any]:
        path = file_path(
            path or ("C:\\" if target.windows else "/"), target.windows
        )
        output = self.run(target, fs_command(target, "list", path))
        if target.windows:
            value = json.loads(output.lstrip("\ufeff"))
        else:
            lines = output.splitlines()
            canonical = base64.b64decode(lines[0], validate=True).decode(
                "utf-8", errors="replace"
            )
            entries = []
            for line in lines[1:]:
                encoded, kind, attributes = line.split("\t", 2)
                size, stamp, mode = attributes.split(" ")
                entries.append(
                    {
                        "name": base64.b64decode(
                            encoded, validate=True
                        ).decode("utf-8", errors="replace"),
                        "type": kind,
                        "size": int(size),
                        "modified": datetime.fromtimestamp(
                            float(stamp), timezone.utc
                        ).isoformat(),
                        "mode": mode,
                    }
                )
            value = {"path": canonical, "entries": entries}
        value["entries"] = sorted(
            value.get("entries") or [],
            key=lambda e: (e["type"] != "directory", e["name"].casefold()),
        )
        value.update(
            windows=target.windows,
            container=target.container,
            maxFileBytes=upload_limit("pod_file_bytes"),
            transferTimeoutSeconds=upload_limit("pod_timeout_seconds"),
        )
        return cast(dict[str, Any], value)

    def operation(
        self,
        target: PodTarget,
        action: str,
        path: str,
        *,
        name: str = "",
        recursive: bool = False,
    ) -> dict[str, Any]:
        if action not in {"mkdir", "touch", "delete", "rename"}:
            raise PodOperationError(400, "不支持的文件操作")
        self.run(
            target,
            fs_command(target, action, path, name=name, recursive=recursive),
        )
        return {"path": path, "action": action}

    def upload(
        self,
        target: PodTarget,
        directory: str,
        name: str,
        source: BinaryIO,
        size: int,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        limit = upload_limit("pod_file_bytes")
        if not 0 <= size <= limit:
            raise PodOperationError(413, f"单个文件最大 {limit} 字节")
        path_mod = ntpath if target.windows else posixpath
        destination = path_mod.join(
            file_path(directory, target.windows),
            file_name(name, target.windows),
        )
        command = fs_command(
            target,
            "upload",
            destination,
            encoded_size=((size + 2) // 3) * 4,
            overwrite=overwrite,
            size=size,
        )
        self.run(
            target,
            command,
            source,
            timeout=upload_limit("pod_timeout_seconds"),
        )
        return {"path": destination, "size": size}

    def download_size(self, target: PodTarget, path: str) -> int:
        size = int(self.run(target, fs_command(target, "stat", path)).strip())
        limit = upload_limit("pod_file_bytes")
        if not 0 <= size <= limit:
            raise PodOperationError(413, f"单个文件最大 {limit} 字节")
        return size

    def download(self, target: PodTarget, path: str) -> Iterator[bytes]:
        deadline = time.monotonic() + upload_limit("pod_timeout_seconds")
        limit = upload_limit("pod_file_bytes")
        pending, stderr, size = "", "", 0
        with self.exec_session(
            target, fs_command(target, "download", path)
        ) as ws:
            while True:
                if time.monotonic() > deadline:
                    raise PodOperationError(504, "文件下载超时")
                opened = ws.is_open()
                if opened:
                    ws.update(timeout=0.1)
                text = ws.read_stdout(timeout=0)
                stderr += ws.read_stderr(timeout=0)
                if len(stderr) > 16384:
                    raise PodOperationError(502, "文件下载失败")
                pending += "".join(text.split())
                count = len(pending) // 4 * 4
                if count:
                    data = base64.b64decode(pending[:count], validate=True)
                    pending = pending[count:]
                    size += len(data)
                    if size > limit:
                        raise PodOperationError(
                            413, f"下载文件已超过 {limit} 字节"
                        )
                    yield data
                if not opened:
                    break
            exec_returncode(ws, stderr)
            if pending:
                raise PodOperationError(502, "文件下载数据不完整")


def shell_command(target: PodTarget, shell: str) -> list[str]:
    if shell in {"bash", "sh"}:
        return [shell, "-c", f"export TERM=xterm-256color; exec {shell}"]
    if shell == "powershell":
        return [
            "powershell.exe" if target.windows else "pwsh",
            "-NoLogo",
            "-NoProfile",
            "-NoExit",
            "-Command",
            "$env:TERM='xterm-256color'",
        ]
    raise PodOperationError(400, "请选择 Bash、sh 或 PowerShell")
