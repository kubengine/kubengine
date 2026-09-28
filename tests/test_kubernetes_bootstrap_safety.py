import ast
import asyncio
import fcntl
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from infra._kubernetes_bootstrap import guarded_kubeadm, join_arguments, join_endpoint


@pytest.fixture
def node_fixture(tmp_path):
    for name in ("etc", "var/lib", "run/lock", "bin"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    kubectl = tmp_path / "bin/kubectl"
    kubectl.write_text('#!/bin/sh\nprintf "api-check\\n" >> "$FAKE_TRACE"\nexit "${FAKE_API_EXIT:-0}"\n')
    kubectl.chmod(0o755)
    identity = tmp_path / "bin/id"
    identity.write_text('#!/bin/sh\necho "${FAKE_UID:-0}"\n')
    identity.chmod(0o755)
    return tmp_path


def run_guard(root, role, **extra_env):
    trace = root / "trace"
    script = guarded_kubeadm(f"printf 'bootstrap\\n' >> {shlex.quote(str(trace))}", role)
    for prefix in ("/etc", "/var", "/run"):
        script = script.replace(prefix, str(root) + prefix)
    env = {**os.environ, "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"], "FAKE_TRACE": str(trace), **extra_env}
    result = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, timeout=5)
    return result, trace.read_text() if trace.exists() else ""


@pytest.mark.parametrize("role", ["master", "worker"])
def test_clean_nodes_bootstrap(node_fixture, role):
    result, trace = run_guard(node_fixture, role)
    assert result.returncode == 0, result.stderr
    assert trace == "bootstrap\n"


def test_application_and_registry_certificates_do_not_block_new_cluster(node_fixture):
    for name in (
        "opt/kubengine/config/certs/ca/ca.crt",
        "etc/containerd/certs.d/registry/ca.crt",
        "usr/share/pki/ca-trust-source/anchors/example.client.crt",
    ):
        certificate = node_fixture / name
        certificate.parent.mkdir(parents=True, exist_ok=True)
        certificate.write_text("synthetic application certificate")
    result, trace = run_guard(node_fixture, "master")
    assert result.returncode == 0, result.stderr
    assert trace == "bootstrap\n"


@pytest.mark.parametrize("value,expected", [
    ("192.0.2.10", "192.0.2.10:6443"),
    ("192.0.2.10:7443", "192.0.2.10:7443"),
    ("api.example.invalid:6443", "api.example.invalid:6443"),
    ("[2001:db8::1]:7443", "[2001:db8::1]:7443"),
])
def test_join_endpoint_preserves_explicit_port(value, expected):
    assert join_endpoint(value) == expected


@pytest.mark.parametrize("value", ["", "https://example.invalid:6443", "192.0.2.1:6443:6443", "node;touch", "node $(id)", "user@node", "node:0"])
def test_invalid_join_endpoint_is_rejected(value):
    with pytest.raises(ValueError):
        join_endpoint(value)


def test_join_override_is_quoted_as_arguments_and_keeps_custom_port():
    command = "kubeadm join 192.0.2.1:6443 --token 'synthetic;token' --discovery-token-ca-cert-hash synthetic"
    args = join_arguments(command, "api.example.invalid:7443")
    assert args[2] == "api.example.invalid:7443"
    assert shlex.split(shlex.join(args)) == args
    assert args[4] == "synthetic;token"


@pytest.mark.parametrize("role,config", [("master", "admin.conf"), ("worker", "kubelet.conf")])
def test_existing_nodes_are_verified_and_not_bootstrapped(node_fixture, role, config):
    directory = node_fixture / "etc/kubernetes"
    directory.mkdir()
    (directory / config).write_text("synthetic config")
    result, trace = run_guard(node_fixture, role)
    assert result.returncode == 0, result.stderr
    assert trace == "api-check\n"


@pytest.mark.parametrize("role,config", [("master", "admin.conf"), ("worker", "kubelet.conf")])
def test_api_failures_never_fall_back_to_bootstrap(node_fixture, role, config):
    directory = node_fixture / "etc/kubernetes"
    directory.mkdir()
    (directory / config).write_text("synthetic config")
    result, trace = run_guard(node_fixture, role, FAKE_API_EXIT="1")
    assert result.returncode != 0
    assert trace == "api-check\n"
    assert "refusing kubeadm" in result.stderr


@pytest.mark.parametrize("role", ["master", "worker"])
def test_partial_node_state_requires_explicit_recovery(node_fixture, role):
    directory = node_fixture / "etc/kubernetes/pki"
    directory.mkdir(parents=True)
    (directory / "ca.crt").write_text("synthetic marker")
    result, trace = run_guard(node_fixture, role)
    assert result.returncode != 0
    assert trace == ""
    assert "Partial" in result.stderr


def test_unprivileged_inspection_fails_closed(node_fixture):
    result, trace = run_guard(node_fixture, "master", FAKE_UID="1000")
    assert result.returncode != 0
    assert trace == ""


def test_dangling_state_symlink_is_not_treated_as_clean(node_fixture):
    directory = node_fixture / "etc/kubernetes"
    directory.mkdir()
    (directory / "admin.conf").symlink_to(directory / "missing")
    result, trace = run_guard(node_fixture, "master")
    assert result.returncode != 0
    assert trace == ""


def test_concurrent_bootstrap_is_rejected(node_fixture):
    with (node_fixture / "run/lock/kubengine-kubeadm.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, trace = run_guard(node_fixture, "master")
    assert result.returncode != 0
    assert "in progress" in result.stderr
    assert trace == ""


def deployment_methods():
    # Extract only the two safety methods. Importing cli.k8s would monkey-patch
    # networking for the entire suite, so these tests never import its CLI.
    source = Path(__file__).parents[1] / "src/cli/k8s.py"
    tree = ast.parse(source.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "K8sDeployer")
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {"_validate_bootstrap_state", "deploy"}]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *methods], type_ignores=[])
    namespace = {"Path": Path, "K8sDeploymentError": RuntimeError, "logger": SimpleNamespace(error=lambda *a, **kw: None)}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("probe_error", [None, PermissionError("synthetic access denied")])
def test_changed_or_missing_state_cannot_rebootstrap_existing_control_plane(monkeypatch, probe_error):
    namespace = deployment_methods()
    def inspect(_path):
        if probe_error:
            raise probe_error
        return object()
    monkeypatch.setattr(Path, "lstat", inspect)
    deployer = SimpleNamespace(config=SimpleNamespace(get_config_hash=lambda: "new"), deployment_state=SimpleNamespace(should_force_redeploy=lambda value: True))
    with pytest.raises(RuntimeError):
        namespace["_validate_bootstrap_state"](deployer)


def test_matching_config_allows_existing_deployment_resume(monkeypatch):
    namespace = deployment_methods()
    monkeypatch.setattr(Path, "lstat", lambda p: pytest.fail("matching state must retain the resume path"))
    deployer = SimpleNamespace(config=SimpleNamespace(get_config_hash=lambda: "same"), deployment_state=SimpleNamespace(should_force_redeploy=lambda value: False))
    namespace["_validate_bootstrap_state"](deployer)


def test_fresh_deployment_without_markers_is_allowed(monkeypatch):
    namespace = deployment_methods()
    def absent(path):
        raise FileNotFoundError(path)
    monkeypatch.setattr(Path, "lstat", absent)
    deployer = SimpleNamespace(config=SimpleNamespace(get_config_hash=lambda: "fresh"), deployment_state=SimpleNamespace(should_force_redeploy=lambda value: True))
    namespace["_validate_bootstrap_state"](deployer)


def test_guard_runs_before_certificates_or_environment_mutations():
    namespace = deployment_methods()
    def refuse():
        raise RuntimeError("existing control plane")
    deployer = SimpleNamespace(
        _validate_bootstrap_state=refuse,
        validate_environment=lambda: pytest.fail("must stop before contacting nodes"),
        prepare_certificates=lambda: pytest.fail("must not rotate certificates"),
        _error=lambda message: None,
    )
    assert asyncio.run(namespace["deploy"](deployer)) is False
