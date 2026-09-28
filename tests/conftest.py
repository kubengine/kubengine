"""Isolate tests and child processes from installed cluster data."""

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

_test_root = tempfile.TemporaryDirectory(prefix="kubengine-tests-")
_root = Path(_test_root.name)
_ca_key = _root / "certs" / "ca" / "ca.key"
_ca_key.parent.mkdir(parents=True)
_ca_key.write_text("a3ViZW5naW5lLWlzb2xhdGVkLXRlc3Qta2V5")
_config_path = _root / "application.yaml"
_config_path.write_text(
    yaml.safe_dump(
        {
            "root_dir": str(_root),
            "domain": "kubengine.test",
            "tls": {"root_dir": str(_root / "certs")},
            "auth": {
                "users": {
                    "admin": {
                        "password_hash": "disabled-test-password",
                        "ak": "test-ak",
                        "sk_hash": "disabled-test-secret",
                    }
                }
            },
            "registry": {"username": "test-user", "password": "test-password"},
        }
    )
)
os.environ["KUBEENGINE_CONFIG"] = str(_config_path)


# These modules deliberately exercise the standalone gevent CLI runtime.
# Importing pyinfra_cli patches threading/socket/subprocess globally,
# which must
# not affect the asyncio HTTP and native-thread tests in the main pytest
# process.
_GEVENT_MODULES = {"test_executor_wrapper.py", "test_k8s_lifecycle.py"}


def pytest_addoption(parser):
    parser.addoption(
        "--gevent-test-worker",
        action="store_true",
        default=False,
        help="Run a selected gevent CLI test in its isolated worker process.",
    )


class _IsolatedGeventTest(pytest.Item):
    def runtest(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--gevent-test-worker",
                "-q",
                "-o",
                "faulthandler_timeout=15",
                f"{self.path}::{self.name}",
            ],
            cwd=self.config.rootpath,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            pytest.fail(
                f"Isolated gevent test failed (exit {result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}",
                pytrace=False,
            )

    def reportinfo(self):
        return self.path, self._source_line, self.name


class _IsolatedGeventModule(pytest.Module):
    def collect(self):
        # Discover names without importing the monkey-patching modules.
        # Each
        # selected test is still run by ordinary pytest, with its real
        # fixtures,
        # in the worker. Per-test nodes preserve selection and failure
        # reporting.
        tree = ast.parse(self.path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test_"):
                item = _IsolatedGeventTest.from_parent(self, name=node.name)
                item._source_line = node.lineno - 1
                yield item


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makemodule(module_path, parent):
    if module_path.name in _GEVENT_MODULES and not parent.config.getoption(
        "--gevent-test-worker"
    ):
        return _IsolatedGeventModule.from_parent(parent, path=module_path)
    return None
