"""Versioned, atomic CLI checkpoints and process-wide deployment exclusion."""

from contextlib import contextmanager
from datetime import datetime
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
from uuid import uuid4

from core.config import Application
from core.private_files import private_directory, private_file
from core.runtime_files import private_runtime_file


class DeploymentStateError(RuntimeError):
    pass


@contextmanager
def deployment_lock():
    with private_runtime_file("k8s-deployment.lock") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeploymentStateError("已有部署、扩容或状态重置正在执行，请等待其结束") from None
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def exclusive_deployment(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        with deployment_lock():
            return function(*args, **kwargs)
    return wrapper


class DeploymentState:
    VERSION = 2

    def __init__(self, state_file=None):
        self.state_file = Path(state_file or Path(Application.ROOT_DIR) / "config/.k8s_deployment_state.json")
        self.state = self._load_state()

    @staticmethod
    def _empty():
        return {"schema_version": 2, "completed_files": [], "failed_files": [], "skip_files": [],
                "file_hashes": {}, "config_hash": None, "last_execution_time": None, "steps": {}}

    def _load_state(self):
        try:
            with private_directory(self.state_file.parent) as directory:
                with private_file(directory, self.state_file.name) as fd:
                    with os.fdopen(os.dup(fd), encoding="utf-8") as file:
                        data = json.load(file)
        except FileNotFoundError:
            return self._empty()
        except (OSError, ValueError) as exc:
            raise DeploymentStateError(f"无法读取部署状态，拒绝按首次部署处理：{self.state_file}") from exc
        if not isinstance(data, dict) or data.get("schema_version", 1) not in (1, 2):
            raise DeploymentStateError("部署状态格式或版本不受支持")
        if not {"completed_files", "failed_files", "file_hashes", "config_hash"} <= data.keys():
            raise DeploymentStateError("部署状态缺少必需字段，拒绝按首次部署处理")
        result = {**self._empty(), **data}
        for key in ("completed_files", "failed_files", "skip_files"):
            if not isinstance(result[key], list) or not all(isinstance(value, str) for value in result[key]):
                raise DeploymentStateError(f"部署状态字段 {key} 无效")
        if (not isinstance(result["file_hashes"], dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in result["file_hashes"].items())
                or not isinstance(result["steps"], dict)
                or (result["config_hash"] is not None and not isinstance(result["config_hash"], str))):
            raise DeploymentStateError("部署状态检查点无效")
        for name, step in result["steps"].items():
            if (not isinstance(name, str) or not isinstance(step, dict)
                    or step.get("status") not in {"running", "success", "failed"}
                    or not isinstance(step.get("attempt"), int) or step["attempt"] < 1
                    or not isinstance(step.get("owner"), str)):
                raise DeploymentStateError("部署步骤状态无效")
        result["schema_version"] = self.VERSION
        return result

    def _mutate(self, change):
        # State-level locking also makes separate readers' updates merge safely.
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with private_directory(self.state_file.parent) as directory:
            with private_file(directory, self.state_file.name + ".lock", os.O_RDWR | os.O_CREAT) as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = self._load_state()
                result = change(state)
                state["last_execution_time"] = datetime.now().isoformat()
                name = f".{self.state_file.name}.{uuid4().hex}.tmp"
                try:
                    with private_file(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
                        with os.fdopen(os.dup(fd), "w", encoding="utf-8") as file:
                            json.dump(state, file, indent=2, ensure_ascii=False)
                            file.flush()
                            os.fsync(file.fileno())
                    os.replace(name, self.state_file.name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(name, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                self.state = state
                return result

    def reload(self):
        self.state = self._load_state()

    def is_file_completed(self, name):
        return name in self.state["completed_files"] and name not in self.state["failed_files"]

    def is_file_failed(self, name):
        return name in self.state["failed_files"]

    def is_file_skipped(self, name):
        return name in self.state["skip_files"]

    def mark_file_running(self, name, digest):
        def change(state):
            previous = state["steps"].get(name, {})
            step = {"status": "running", "owner": uuid4().hex,
                    "attempt": previous.get("attempt", 0) + 1, "input_digest": digest}
            state["steps"][name] = step
            state["completed_files"] = [f for f in state["completed_files"] if f != name]
            return step["owner"]
        return self._mutate(change)

    def _finish(self, name, status, digest=None, owner=None):
        def change(state):
            step = state["steps"].get(name)
            if owner is not None and (not step or step["owner"] != owner or step["status"] != "running"):
                raise DeploymentStateError("部署检查点已由另一执行接管，拒绝旧状态写入")
            for key in ("completed_files", "failed_files"):
                state[key] = [f for f in state[key] if f != name]
            state["completed_files" if status == "success" else "failed_files"].append(name)
            if digest is not None and status == "success":
                state["file_hashes"][name] = digest
            if step:
                step["status"] = status
        self._mutate(change)

    def mark_file_completed(self, name, file_hash=None, owner=None):
        self._finish(name, "success", file_hash, owner)

    def mark_file_failed(self, name, owner=None):
        self._finish(name, "failed", owner=owner)

    def set_config_hash(self, value):
        self._mutate(lambda state: state.update(config_hash=value))

    def get_file_hash(self, name):
        return self.state["file_hashes"].get(name)

    def set_file_hash(self, name, value):
        self._mutate(lambda state: state["file_hashes"].update({name: value}))

    def reset_state(self):
        def reset(state):
            skipped = state["skip_files"]
            state.clear()
            state.update(self._empty(), skip_files=skipped)
        self._mutate(reset)

    def should_force_redeploy(self, config_hash):
        return self.state["config_hash"] != config_hash
