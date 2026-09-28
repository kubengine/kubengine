import ast
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from core.config import Application
from core.http_api_client.harbor_client import HarborClient


@pytest.fixture
def registry_config(monkeypatch):
    registry = SimpleNamespace(
        USERNAME="configured-user", PASSWORD="configured-test-secret"
    )
    monkeypatch.setattr(Application, "REGISTRY", registry)
    return registry


def test_harbor_default_client_uses_configured_credentials(registry_config):
    client = HarborClient()
    assert client.username == registry_config.USERNAME
    assert client.password == registry_config.PASSWORD


def test_harbor_explicit_credentials_override_configuration(registry_config):
    client = HarborClient(
        username="explicit-user", password="explicit-test-secret"
    )
    assert client.username == "explicit-user"
    assert client.password == "explicit-test-secret"


def test_harbor_explicit_empty_password_does_not_fall_back(registry_config):
    client = HarborClient(password="")
    assert client.username == registry_config.USERNAME
    assert client.password == ""


def test_harbor_missing_password_stays_empty(registry_config):
    registry_config.PASSWORD = ""
    assert HarborClient().password == ""


def test_harbor_new_client_observes_updated_credentials(registry_config):
    first = HarborClient()
    registry_config.PASSWORD = "rotated-test-secret"
    second = HarborClient()
    assert first.password == "configured-test-secret"
    assert second.password == "rotated-test-secret"


@pytest.mark.parametrize(
    "password,status",
    [("do-not-display-test-secret", "已配置"), ("", "未配置")],
)
def test_deployment_summary_never_displays_registry_password(
    capsys, password, status
):
    # Do not import cli.k8s or alter gevent behavior in the test
    # process.
    source = Path(__file__).parents[1] / "src/cli/k8s.py"
    tree = ast.parse(source.read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "K8sDeployer"
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_show_deployment_results"
    )
    app = SimpleNamespace(
        REGISTRY=SimpleNamespace(USERNAME="synthetic-user", PASSWORD=password),
        DOMAIN="example.invalid",
        K8S_CONFIG=SimpleNamespace(
            CONTROL_PLANE_ENDPOINT="",
            MASTER_IP="192.0.2.10",
            WORKER_IPS=[],
            SERVICE_CIDR="10.96.0.0/16",
            POD_CIDR="10.97.0.0/16",
            LOADBALANCER_IP_POOLS=["192.0.2.20"],
        ),
        TLS_CONFIG=SimpleNamespace(
            CA_CRT="/synthetic/ca.crt", ROOT_DIR="/synthetic/certs"
        ),
    )
    namespace = {"Application": app, "click": click}
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(body=[method], type_ignores=[])
            ),
            str(source),
            "exec",
        ),
        namespace,
    )
    deployer = SimpleNamespace(
        config=SimpleNamespace(
            get_loadbalancer_ip=lambda: "192.0.2.20",
            deploy_src="/synthetic/offline",
        )
    )
    namespace["_show_deployment_results"](deployer)
    output = capsys.readouterr().out
    if password:
        assert password not in output
    assert status in output
    assert "registry.username/password" in output
    assert "Harbor默认密码" not in output
