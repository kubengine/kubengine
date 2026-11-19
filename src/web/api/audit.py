"""只读分页审计查询，无编辑和删除接口。"""

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from core.orm.audit import query_events
from web.utils.auth import auth_with_renew

router = APIRouter(tags=["操作审计"])


@router.get("/audit-events")
@auth_with_renew()
async def events(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    username: str | None = Query(None, max_length=64),
    actor_id: str | None = Query(None, max_length=64),
    action: str | None = Query(None, max_length=100),
    namespace: str | None = Query(None, max_length=63),
    resource_type: str | None = Query(None, max_length=100),
    resource_name: str | None = Query(None, max_length=255),
    result: str | None = Query(None, max_length=32),
    request_id: str | None = Query(None, max_length=128),
    task_id: str | None = Query(None, max_length=64),
    batch_id: str | None = Query(None, max_length=128),
    since: int | None = Query(None, ge=0),
    until: int | None = Query(None, ge=0),
):
    return await run_in_threadpool(
        query_events,
        page,
        page_size,
        username=username,
        actor_id=actor_id,
        action=action,
        namespace=namespace,
        resource_type=resource_type,
        resource_name=resource_name,
        result=result,
        request_id=request_id,
        task_id=task_id,
        batch_id=batch_id,
        since=since,
        until=until,
    )
