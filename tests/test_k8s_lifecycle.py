import importlib
import logging
import sys

import core.logger as logger_module


def import_k8s_cli_without_file_logging(monkeypatch):
    """导入 CLI 时禁用指向 /opt/kubengine 的全局文件 handler。"""
    monkeypatch.setattr(logger_module, "setup_cli_logging", lambda *args, **kwargs: None)
    sys.modules.pop("cli.k8s", None)
    return importlib.import_module("cli.k8s")


def test_deployment_lifecycle_records_user_cancellation(monkeypatch, caplog) -> None:
    k8s = import_k8s_cli_without_file_logging(monkeypatch)

    class FakeConfig:
        def __init__(self, deploy_src: str) -> None:
            self.deploy_src = deploy_src

        def show_config(self) -> None:
            pass

    monkeypatch.setattr(k8s, "K8sDeploymentConfig", FakeConfig)
    monkeypatch.setattr(k8s.click, "confirm", lambda _prompt: False)

    with caplog.at_level(logging.INFO, logger="cli.k8s"):
        k8s.deploy.callback("/deployment", 0, False)

    records = [record for record in caplog.records if hasattr(record, "event")]
    assert [record.event for record in records] == [  # type: ignore[attr-defined]
        "deployment_start",
        "deployment_end",
    ]
    assert records[0].deployment_id.startswith("dep-")  # type: ignore[attr-defined]
    assert records[-1].status == "cancelled"  # type: ignore[attr-defined]
    assert records[-1].reason == "user_cancelled"  # type: ignore[attr-defined]
    assert records[-1].duration_ms >= 0  # type: ignore[attr-defined]


def test_scale_lifecycle_decorator_records_cancellation(monkeypatch, caplog) -> None:
    k8s = import_k8s_cli_without_file_logging(monkeypatch)

    @k8s.with_deployment_lifecycle("scale")
    def cancel_scale():
        return "cancelled"

    with caplog.at_level(logging.INFO, logger="cli.k8s"):
        assert cancel_scale() is None

    records = [record for record in caplog.records if hasattr(record, "event")]
    assert [record.event for record in records] == [  # type: ignore[attr-defined]
        "deployment_start",
        "deployment_end",
    ]
    assert records[-1].operation_type == "scale"  # type: ignore[attr-defined]
    assert records[-1].status == "cancelled"  # type: ignore[attr-defined]
