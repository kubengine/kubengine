"""
Continuously recover durable application work with a bounded executor.
"""

from concurrent.futures import ThreadPoolExecutor

from core.logger import get_logger
from core.orm.task import execute_task_function, find_unfinished_tasks

logger = get_logger(__name__)


class AppTaskWorker:
    def __init__(self, concurrency=4, *, owner=None, lease_seconds=90):
        self.concurrency = concurrency
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.executor = ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="app-task"
        )
        self.futures = {}
        self.cursor = 0

    def poll(self):
        for task_id, future in list(self.futures.items()):
            if future.done():
                del self.futures[task_id]
                if future.exception() is not None:
                    logger.error(
                        "应用任务 %s 执行结束并报告错误：%s",
                        task_id,
                        future.exception(),
                    )
        if len(self.futures) >= self.concurrency:
            return
        tasks = find_unfinished_tasks()
        tasks.sort(
            key=lambda task: (task.task_id <= self.cursor, task.task_id)
        )
        for task in tasks:
            if task.task_id not in self.futures:
                self.cursor = task.task_id
                self.futures[task.task_id] = self.executor.submit(
                    execute_task_function,
                    task.task_id,
                    task.task_func_path,
                    task.params,
                    recover_running=True,
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                )
            if len(self.futures) >= self.concurrency:
                break

    def close(self):
        self.executor.shutdown(wait=True)
