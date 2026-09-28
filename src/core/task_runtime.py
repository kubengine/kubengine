"""Shared attempt identity and lease heartbeat for durable task executors."""

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import threading

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class TaskLease:
    owner: str
    attempt: int


class TaskLeaseLost(RuntimeError):
    """A stale attempt must stop external steps and database writes."""


class TaskSuperseded(RuntimeError):
    """A later user operation replaced this resource's desired operation."""


@contextmanager
def lease_heartbeat(renew, *, interval=30.0):
    done, lost = threading.Event(), threading.Event()

    def heartbeat():
        while not done.wait(interval):
            try:
                if renew():
                    continue
            except Exception:
                logger.exception("任务续租失败")
            lost.set()
            return

    context = copy_context()
    thread = threading.Thread(target=lambda: context.run(heartbeat), name="task-heartbeat", daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        done.set()
        thread.join(timeout=2)


@dataclass(frozen=True)
class ApplicationAttempt:
    task_id: int
    lease: TaskLease
    cluster_id: int
    resource_uid: str
    resource_version: int
    lost: threading.Event


current_attempt: ContextVar[ApplicationAttempt | None] = ContextVar("application_attempt", default=None)


def check_application_attempt(db=None, cluster_id=None):
    """Validate task and resource ownership in the caller's write transaction."""
    attempt = current_attempt.get()
    if attempt is None:
        return
    from core.orm.engine import get_db
    from core.orm.task import owned_task_query
    from core.orm.cluster import Cluster
    if db is None:
        with get_db() as session:
            return check_application_attempt(session, cluster_id)
    if attempt.lost.is_set() or owned_task_query(db, attempt.task_id, attempt.lease).first() is None:
        raise TaskLeaseLost("任务租约已失效，停止旧执行")
    if cluster_id is not None and cluster_id != attempt.cluster_id:
        raise TaskSuperseded("任务不拥有该资源")
    cluster = db.get(Cluster, attempt.cluster_id)
    if (cluster is None or cluster.resource_uid != attempt.resource_uid
            or cluster.operation_version != attempt.resource_version):
        raise TaskSuperseded("资源已有更新的操作，拒绝恢复旧部署任务")


def checkpoint_application(phase: str):
    from core.orm.engine import get_db
    from core.orm.task import owned_task_query
    from sqlalchemy import text
    attempt = current_attempt.get()
    if attempt is None:
        raise RuntimeError("应用操作必须由持久化任务执行器调用")
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        check_application_attempt(db)
        owned_task_query(db, attempt.task_id, attempt.lease).update({"phase": phase}, synchronize_session=False)
        db.commit()


def application_phase():
    from core.orm.engine import get_db
    from core.orm.task import owned_task_query
    attempt = current_attempt.get()
    if attempt is None:
        return None
    with get_db() as db:
        check_application_attempt(db)
        return owned_task_query(db, attempt.task_id, attempt.lease).one().phase
