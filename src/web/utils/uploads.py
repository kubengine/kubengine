"""Authenticate and bound multipart input before FastAPI starts spooling it."""

import asyncio
from contextlib import contextmanager, ExitStack
import fcntl
import shutil
import tempfile
import time

from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.formparsers import MultiPartParser, MultiPartException

from core.runtime_files import private_runtime_file
from core.upload_limits import upload_limit
from web.utils import auth


@contextmanager
def upload_slot():
    with ExitStack() as stack:
        for index in range(upload_limit("concurrency")):
            lock = stack.enter_context(private_runtime_file(f"upload-slot-{index}.lock"))
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return
        raise HTTPException(status_code=429, detail="上传并发已达上限，请稍后重试")


class SecureUploadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        if self.endpoint.__name__ not in {"upload_chart", "create_image_import_api", "upload_image"}:
            return handler

        async def bounded(request):
            await auth.get_current_user(authorization=request.headers.get("authorization"))
            file_limit = 2 * 1024**2 if self.endpoint.__name__ == "upload_chart" else upload_limit("image_bytes")
            body_limit = file_limit + 64 * 1024
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                raise HTTPException(status_code=400, detail="Content-Length 无效") from None
            if length < 0 or length > body_limit:
                raise HTTPException(status_code=413, detail="上传文件超过大小限制")
            if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
                raise HTTPException(status_code=415, detail="请使用 multipart/form-data 上传")

            deadline = time.monotonic() + upload_limit("timeout_seconds")
            async def stream():
                size = 0
                source = request.stream().__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(source.__anext__(), max(0, deadline - time.monotonic()))
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        raise HTTPException(status_code=408, detail="上传超时") from None
                    size += len(chunk)
                    if size > body_limit:
                        raise HTTPException(status_code=413, detail="上传文件超过大小限制")
                    if shutil.disk_usage(tempfile.gettempdir()).free < upload_limit("reserve_bytes") + len(chunk):
                        raise HTTPException(status_code=507, detail="上传临时空间不足")
                    yield chunk

            with upload_slot():
                parser = MultiPartParser(request.headers, stream(), max_files=1, max_fields=8, max_part_size=64 * 1024)
                try:
                    request._form = await parser.parse()
                    for _, value in request._form.multi_items():
                        if getattr(value, "size", 0) and value.size > file_limit:
                            raise HTTPException(status_code=413, detail="上传文件超过大小限制")
                    return await handler(request)
                except MultiPartException as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from None
                finally:
                    # Starlette only closes these on MultiPartException. Also
                    # cover disconnects, cancellations and our ingress limits.
                    for file in parser._files_to_close_on_error:
                        file.close()
        return bounded
