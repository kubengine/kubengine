import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from core.config.application import Application
from core.logger import (
    GlobalLoggerManager,
    MultiProcessFileHandler,
    MultiProcessTimedRotatingFileHandler,
)

_WORKER_SCRIPT = r"""
import logging
import sys
import time
from pathlib import Path

from core.logger import MultiProcessFileHandler, MultiProcessTimedRotatingFileHandler

log_file = sys.argv[1]
worker_id = int(sys.argv[2])
ready_file = Path(sys.argv[3])
start_file = Path(sys.argv[4])
record_count = int(sys.argv[5])
rotate = sys.argv[6] == "true"

if rotate:
    handler = MultiProcessTimedRotatingFileHandler(
        log_file,
        when="S",
        interval=1,
        backupCount=5,
        encoding="utf-8",
    )
    # 所有进程都认为已到轮转时间，用于复现重复轮转竞争。
    handler.rolloverAt = 1
else:
    handler = MultiProcessFileHandler(log_file, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(message)s"))

process_logger = logging.getLogger(f"test.multiprocess.{worker_id}")
process_logger.handlers = [handler]
process_logger.setLevel(logging.INFO)
process_logger.propagate = False

ready_file.touch()
deadline = time.monotonic() + 10
while not start_file.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("等待多进程日志测试启动超时")
    time.sleep(0.01)

try:
    for sequence in range(record_count):
        process_logger.info("worker=%s sequence=%s", worker_id, sequence)
finally:
    handler.close()
    process_logger.handlers.clear()
"""


def _run_log_workers(
    tmp_path: Path,
    log_file: Path,
    process_count: int,
    record_count: int,
    rotate: bool,
) -> None:
    """用未加载测试插件的独立解释器验证真实多进程写入。"""
    source_path = Path(__file__).parents[1] / "src"
    environment = os.environ.copy()
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_path), existing_python_path) if part
    )
    start_file = tmp_path / "start"
    ready_files = [
        tmp_path / f"ready-{worker_id}" for worker_id in range(process_count)
    ]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER_SCRIPT,
                str(log_file),
                str(worker_id),
                str(ready_files[worker_id]),
                str(start_file),
                str(record_count),
                str(rotate).lower(),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        for worker_id in range(process_count)
    ]

    try:
        deadline = time.monotonic() + 15
        while not all(path.exists() for path in ready_files):
            failed_processes = [
                process for process in processes if process.poll() is not None
            ]
            if failed_processes or time.monotonic() >= deadline:
                details = [process.communicate() for process in failed_processes]
                raise AssertionError(f"日志工作进程未就绪: {details}")
            time.sleep(0.01)

        start_file.touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert (
                process.returncode == 0
            ), f"日志工作进程退出异常，stdout={stdout!r}, stderr={stderr!r}"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_concurrent_processes_write_and_rotate_without_losing_records(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "multi-process.log"
    log_file.write_text("seed\n", encoding="utf-8")
    process_count = 4
    record_count = 25
    _run_log_workers(
        tmp_path,
        log_file,
        process_count,
        record_count,
        rotate=True,
    )

    log_files = [
        path
        for path in tmp_path.glob("multi-process.log*")
        if path.name != "multi-process.log.lock"
    ]
    lines = [
        line
        for path in log_files
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    expected = {
        f"worker={worker_id} sequence={sequence}"
        for worker_id in range(process_count)
        for sequence in range(record_count)
    }

    assert set(lines) == expected | {"seed"}
    assert len(lines) == len(expected) + 1
    assert len([path for path in log_files if path != log_file]) == 1
    assert (tmp_path / "multi-process.log.lock").exists()


def test_concurrent_processes_write_safely_when_rotation_is_disabled(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "multi-process-plain.log"
    process_count = 4
    record_count = 25
    _run_log_workers(
        tmp_path,
        log_file,
        process_count,
        record_count,
        rotate=False,
    )

    lines = log_file.read_text(encoding="utf-8").splitlines()
    expected = {
        f"worker={worker_id} sequence={sequence}"
        for worker_id in range(process_count)
        for sequence in range(record_count)
    }

    assert set(lines) == expected
    assert len(lines) == len(expected)
    assert (tmp_path / "multi-process-plain.log.lock").exists()


@pytest.mark.parametrize(
    ("rotate_enabled", "expected_type"),
    [
        (True, MultiProcessTimedRotatingFileHandler),
        (False, MultiProcessFileHandler),
    ],
)
def test_logger_manager_always_uses_multiprocess_file_handler(
    tmp_path: Path,
    monkeypatch: Any,
    rotate_enabled: bool,
    expected_type: type[logging.FileHandler],
) -> None:
    monkeypatch.setattr(
        Application.LOGGER_CONFIG,
        "ROTATE_ENABLE",
        rotate_enabled,
    )
    target_logger = logging.Logger(f"test.handler.{rotate_enabled}")

    GlobalLoggerManager()._setup_file_handler(
        target_logger,
        logging.Formatter("%(message)s"),
        str(tmp_path / f"handler-{rotate_enabled}.log"),
    )

    try:
        assert len(target_logger.handlers) == 1
        assert isinstance(target_logger.handlers[0], expected_type)
    finally:
        target_logger.handlers[0].close()
        target_logger.handlers.clear()
