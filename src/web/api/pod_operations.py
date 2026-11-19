"""Pod 终端与文件管理 API。"""

import asyncio
import base64
import json
import queue
import threading
import time
from contextlib import suppress
from typing import Any, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from core.http_api_client.pod_operations import (
    PodOperations,
    shell_command,
)
from core.logger import get_logger
from core.upload_limits import upload_limit
from web.api.k8s import get_kubernetes_client
from web.utils import auth
from web.utils.auth import User, auth_with_renew
from web.utils.uploads import SecureUploadRoute

router = APIRouter(tags=["Pod 终端与文件"], route_class=SecureUploadRoute)
logger = get_logger(__name__)


def terminal_error(message: str, executable: str) -> str:
    if any(
        text in message.lower()
        for text in (
            "executable file not found",
            "executable not found",
            "not recognized as",
            "cannot find the file specified",
        )
    ):
        return f"容器中未找到 {executable}，请切换 Shell 或使用已安装该 Shell 的镜像。"
    return message[:2000]


def get_operations() -> PodOperations:
    return PodOperations(get_kubernetes_client())


def api_error(exc: ApiException) -> tuple[int, str, dict[str, Any]]:
    message = str(exc.reason or "Kubernetes exec 操作失败")
    if exc.body:
        with suppress(ValueError, TypeError, AttributeError):
            message = json.loads(exc.body).get("message") or message
    return exc.status or 502, message[:2000], {}


class FileOperation(BaseModel):
    action: Literal["mkdir", "touch", "delete", "rename"]
    path: str = Field(min_length=1, max_length=4096)
    name: str = Field(default="", max_length=255)
    recursive: bool = False


@router.get("/pods/{namespace}/{name}/files", summary="浏览 Pod 容器目录")
@auth_with_renew(permission="pod.files.read")
def list_pod_files(
    request: Request,
    namespace: str,
    name: str,
    container: str | None = Query(None),
    path: str | None = Query(None, max_length=4096),
):
    try:
        ops = get_operations()
        return (
            200,
            "success",
            ops.listing(ops.target(namespace, name, container), path),
        )
    except ApiException as exc:
        return api_error(exc)


@router.post(
    "/pods/{namespace}/{name}/files/operations",
    summary="新建、重命名和删除容器文件",
)
@auth_with_renew(permission="pod.files.write")
def operate_pod_file(
    request: Request,
    namespace: str,
    name: str,
    body: FileOperation,
    current_user: User,
    container: str | None = Query(None),
):
    try:
        ops = get_operations()
        target = ops.target(namespace, name, container)
        result = ops.operation(
            target,
            body.action,
            body.path,
            name=body.name,
            recursive=body.recursive,
        )
        logger.info(
            "Pod 文件操作成功 user=%r pod=%r container=%r action=%r path=%r",
            current_user.username,
            f"{namespace}/{name}",
            target.container,
            body.action,
            body.path,
        )
        return 200, "操作成功", result
    except ApiException as exc:
        return api_error(exc)


@router.post(
    "/pods/{namespace}/{name}/files/upload", summary="上传文件到 Pod 容器"
)
@auth_with_renew(permission="pod.files.write")
async def upload_pod_file(
    request: Request,
    namespace: str,
    name: str,
    current_user: User,
    file: UploadFile = File(...),
    path: str = Form(..., max_length=4096),
    overwrite: bool = Form(False),
    container: str | None = Query(None),
):
    def transfer():
        try:
            size = file.size
            limit = upload_limit("pod_file_bytes")
            if size is None or size > limit:
                raise HTTPException(413, f"单个文件最大 {limit} 字节")
            ops = get_operations()
            target = ops.target(namespace, name, container)
            result = ops.upload(
                target,
                path,
                file.filename or "",
                file.file,
                size,
                overwrite=overwrite,
            )
            logger.info(
                "Pod 文件上传成功 user=%r pod=%r container=%r path=%r size=%r",
                current_user.username,
                f"{namespace}/{name}",
                target.container,
                result["path"],
                size,
            )
            return 200, "上传成功", result
        except ApiException as exc:
            return api_error(exc)

    try:
        return await run_in_threadpool(transfer)
    finally:
        await file.close()


async def download_identity(request: Request):
    from core.orm import audit
    from web.utils import access

    try:
        return await auth.get_current_user(
            authorization=request.headers.get("authorization")
        )
    except HTTPException as exc:
        operation = access.begin(
            request, None, "pod.file.download", request.path_params
        )
        audit.finish_event(operation["audit_id"], exc.status_code)
        raise


@router.get("/pods/{namespace}/{name}/files/download", summary="下载容器文件")
def download_pod_file(
    namespace: str,
    name: str,
    path: str = Query(..., max_length=4096),
    container: str | None = Query(None),
    identity: tuple = Depends(download_identity),
    request: Request = None,
):
    from core.authorization import authorize
    from core.orm import audit
    from web.utils import access

    operation = access.begin(
        request,
        identity[0],
        "pod.file.download",
        {"namespace": namespace, "name": name, "type": "pod"},
    )
    try:
        authorize(identity[0], "pod.files.read", "pod", namespace)
        ops = get_operations()
        target = ops.target(namespace, name, container)
        size = ops.download_size(target, path)
    except ApiException as exc:
        code, message, _ = api_error(exc)
        if code == 401:
            code, message = 502, "集群认证失败，请检查 Kubernetes 连接凭据"
        audit.finish_event(operation["audit_id"], code)
        raise HTTPException(code, message) from exc
    except HTTPException as exc:
        audit.finish_event(operation["audit_id"], exc.status_code)
        raise
    except Exception:
        audit.finish_event(operation["audit_id"], 500)
        raise

    def content():
        try:
            yield from ops.download(target, path)
        except BaseException:
            audit.finish_event(operation["audit_id"], 500)
            raise
        else:
            audit.finish_event(operation["audit_id"], 200)

    filename = (path.replace("\\", "/") if target.windows else path).rstrip(
        "/"
    ).rsplit("/", 1)[-1] or "download"
    logger.info(
        "Pod 文件下载 user=%r pod=%r container=%r path=%r",
        identity[0].username,
        f"{namespace}/{name}",
        target.container,
        path,
    )
    return StreamingResponse(
        content(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(filename, safe=""),
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        },
    )


@router.websocket("/pods/{namespace}/{name}/terminal")
async def pod_terminal(
    websocket: WebSocket,
    namespace: str,
    name: str,
    container: str = Query(...),
    shell: str = Query("sh"),
):
    """首条消息鉴权，JWT 不进入 URL；每个连接拥有独立 SDK exec。"""
    await websocket.accept()
    token = ""
    audit_id = None
    audit_status = 200
    stop = threading.Event()
    incoming: queue.Queue = queue.Queue(maxsize=128)
    outgoing: asyncio.Queue = asyncio.Queue(maxsize=64)
    worker = None

    async def authenticate(value):
        if not isinstance(value, str):
            raise HTTPException(401, "请先登录")
        identity = await auth.get_current_user(authorization=value)
        from core.authorization import authorize

        authorize(identity[0], "pod.terminal", "pod", namespace)
        return identity

    try:
        hello = await asyncio.wait_for(websocket.receive_text(), 10)
        if len(hello) > 32768:
            raise HTTPException(400, "鉴权消息过大")
        initial = json.loads(hello)
        if not isinstance(initial, dict) or initial.get("type") != "auth":
            raise HTTPException(401, "首条消息必须进行鉴权")
        token = initial.get("token")
        from core.authorization import value
        from core.orm import audit

        # 身份验证和动作检查分开，拒绝的连接同样留痕。
        identity, _ = await auth.get_current_user(authorization=token)
        audit_id = audit.begin_event(
            actor_id=value(identity, "user_id", ""),
            username=identity.username,
            action="pod.terminal",
            namespace=namespace,
            resource_type="pod",
            resource_name=name,
            source_ip=websocket.client.host if websocket.client else "",
        )
        await authenticate(token)
        ops = get_operations()
        target = await run_in_threadpool(
            ops.target, namespace, name, container
        )
        command = shell_command(target, shell)
        loop = asyncio.get_running_loop()

        def emit(value):
            if stop.is_set():
                return
            future = asyncio.run_coroutine_threadsafe(
                outgoing.put(value), loop
            )
            try:
                future.result(timeout=3)
            except Exception:
                future.cancel()
                stop.set()

        def bridge():
            try:
                with ops.exec_session(
                    target, command, stdin=True, tty=True, binary=True
                ) as session:
                    emit({"type": "ready"})
                    started = last_input = time.monotonic()
                    while session.is_open() and not stop.is_set():
                        if (
                            time.monotonic() - started > 3600
                            or time.monotonic() - last_input > 600
                        ):
                            emit(
                                {
                                    "type": "error",
                                    "message": "终端会话已超时，请重新连接",
                                }
                            )
                            break
                        for _ in range(32):
                            try:
                                item = incoming.get_nowait()
                            except queue.Empty:
                                break
                            if item["type"] == "input":
                                session.write_stdin(item["data"])
                                last_input = time.monotonic()
                            elif item["type"] == "resize":
                                session.write_channel(
                                    4,
                                    json.dumps(
                                        {
                                            "Width": item["cols"],
                                            "Height": item["rows"],
                                        }
                                    ),
                                )
                        session.update(timeout=0.1)
                        text = session.read_stdout(timeout=0)
                        if text:
                            for offset in range(0, len(text), 16384):
                                part = text[offset : offset + 16384]
                                emit(
                                    {
                                        "type": "output",
                                        "data": (
                                            base64.b64encode(part).decode()
                                            if isinstance(part, bytes)
                                            else part
                                        ),
                                        "encoding": (
                                            "base64"
                                            if isinstance(part, bytes)
                                            else "utf8"
                                        ),
                                    }
                                )
                    if not stop.is_set():
                        code = None
                        with suppress(KeyError, TypeError, ValueError):
                            status = json.loads(
                                session.read_channel(3, timeout=0) or "{}"
                            )
                            if status.get("status") == "Success":
                                code = 0
                            elif status.get("status") == "Failure":
                                causes = (status.get("details") or {}).get(
                                    "causes"
                                ) or []
                                code = next(
                                    (
                                        int(c["message"])
                                        for c in causes
                                        if c.get("reason") == "ExitCode"
                                    ),
                                    None,
                                )
                                emit(
                                    {
                                        "type": "error",
                                        "message": terminal_error(
                                            status.get("message")
                                            or (
                                                f"Shell 退出码 {code}，"
                                                "请确认容器中已安装所选 Shell"
                                            ),
                                            command[0],
                                        ),
                                    }
                                )
                        emit({"type": "exit", "code": code})
            except Exception as exc:
                message = (
                    api_error(exc)[1]
                    if isinstance(exc, ApiException)
                    else "终端连接中断"
                )
                emit(
                    {
                        "type": "error",
                        "message": terminal_error(message, command[0]),
                    }
                )
            finally:
                emit({"type": "done"})

        logger.info(
            "Pod 终端连接 user=%r pod=%r container=%r shell=%r",
            identity.username,
            f"{namespace}/{name}",
            container,
            shell,
        )
        worker = asyncio.create_task(asyncio.to_thread(bridge))
        last_auth = time.monotonic()
        receive = asyncio.create_task(websocket.receive_text())
        output = asyncio.create_task(outgoing.get())
        try:
            while not stop.is_set():
                done, _ = await asyncio.wait(
                    {receive, output},
                    timeout=1,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if time.monotonic() - last_auth >= 15:
                    await authenticate(token)
                    last_auth = time.monotonic()
                if receive in done:
                    text = receive.result()
                    if len(text) > 32768:
                        raise HTTPException(400, "终端消息过大")
                    value = json.loads(text)
                    if not isinstance(value, dict):
                        raise HTTPException(400, "终端消息无效")
                    if value.get("type") == "auth":
                        updated, _ = await authenticate(value.get("token"))
                        if updated.username != identity.username:
                            raise HTTPException(401, "终端身份不能更换")
                        token = value["token"]
                        last_auth = time.monotonic()
                    elif (
                        value.get("type") == "input"
                        and isinstance(value.get("data"), str)
                        and len(value["data"]) <= 16384
                    ):
                        await authenticate(token)
                        incoming.put_nowait(
                            {"type": "input", "data": value["data"]}
                        )
                    elif value.get("type") == "resize":
                        cols, rows = value.get("cols"), value.get("rows")
                        if (
                            type(cols) is not int
                            or type(rows) is not int
                            or not 20 <= cols <= 500
                            or not 5 <= rows <= 300
                        ):
                            raise HTTPException(400, "终端尺寸无效")
                        incoming.put_nowait(
                            {"type": "resize", "cols": cols, "rows": rows}
                        )
                    else:
                        raise HTTPException(400, "不支持的终端消息")
                    receive = asyncio.create_task(websocket.receive_text())
                if output in done:
                    value = output.result()
                    if value["type"] == "done":
                        break
                    await websocket.send_json(value)
                    output = asyncio.create_task(outgoing.get())
        finally:
            receive.cancel()
            output.cancel()
            await asyncio.gather(receive, output, return_exceptions=True)
    except (HTTPException, ApiException) as exc:
        audit_status = (
            exc.status_code
            if isinstance(exc, HTTPException)
            else exc.status or 502
        )
        message = (
            exc.detail if isinstance(exc, HTTPException) else api_error(exc)[1]
        )
        with suppress(Exception):
            await websocket.send_json(
                {"type": "error", "message": str(message)[:2000]}
            )
        with suppress(Exception):
            await websocket.close(code=1008)
    except (ValueError, TypeError, KeyError, queue.Full, asyncio.TimeoutError):
        audit_status = 400
        with suppress(Exception):
            await websocket.close(code=1008, reason="终端消息无效或连接超时")
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()
        if worker is not None:
            # Wait briefly while the worker observes cancellation.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(worker), 2)
        with suppress(Exception):
            await websocket.close()
        from core.orm import audit

        if not audit_id:
            audit_id = audit.begin_event(
                action="pod.terminal",
                namespace=namespace,
                resource_type="pod",
                resource_name=name,
                source_ip=websocket.client.host if websocket.client else "",
            )
        audit.finish_event(audit_id, audit_status)
