"""Application deployment reconciliation, independent of HTTP request
handling.
"""

import json
import re
import tempfile
from typing import Any, Optional

from core.command import execute_command
from core.config import Application, ConfigDict
from core.http_api_client.helm_resource_check import HelmResourceChecker
from core.logger import get_logger, with_log_context
from core.misc.time import pendulum_sleep
from core.orm.cluster import (
    ClusterSchema,
    ClusterStatus,
    find_cluster_by_id,
    remove_cluster_by_id,
    update_cluster_status,
)
from core.task_runtime import (
    TaskLeaseLost,
    TaskSuperseded,
    application_phase,
    check_application_attempt,
    checkpoint_application,
)

logger = get_logger(__name__)


@with_log_context(task_id="task_id", cluster_id="cluster_id")
def deploy_app(task_id: int, cluster_id: int):
    logger.info("部署集群: %s", cluster_id)
    previous = find_cluster_by_id(cluster_id)
    if previous and previous.status in {
        ClusterStatus.cleaning.value,
        ClusterStatus.anomaly.value,
    }:
        # An older deployment task must not reverse a later cleanup
        # request. Keep the cleanup state intact, outside the deployment
        # failure handler.
        raise RuntimeError(
            "集群正在清理或清理异常，拒绝恢复旧部署任务；保留清理状态并请人工核实"
        )
    try:
        cluster = update_cluster_status(cluster_id, ClusterStatus.creating)
        _notify_cluster(cluster)
        release_name = cluster.helm_name or ""
        if not release_name:
            raise RuntimeError("集群缺少 Helm release 名称")
        existing = _find_helm_release(release_name)
        if existing:
            if existing.get("status") != "deployed":
                raise RuntimeError(
                    "已有 Helm release 未完成部署，请检查后重试；保留资源及管理记录"
                )
            _verify_release_inputs(cluster, existing)
        else:
            # Unique 0600 file, removed on every normal or exceptional
            # exit.
            with tempfile.NamedTemporaryFile(
                prefix="kubengine-values-", suffix=".yaml"
            ) as values_file:
                ConfigDict(cluster.helm_config or {}).save_to_file(
                    values_file.name
                )
                _helm_command(
                    [
                        "install",
                        release_name,
                        (
                            f"oci://{Application.DOMAIN}/charts/"
                            f"{cluster.helm_chart}"
                        ),
                        "--version",
                        cluster.helm_chart_version or "",
                        "-n",
                        "apps",
                        "--create-namespace",
                        "--timeout",
                        "5m",
                        "-f",
                        values_file.name,
                    ]
                )

        checkpoint_application("installed")
        cluster = update_cluster_status(cluster_id, ClusterStatus.checking)
        _notify_cluster(cluster)
        checker = HelmResourceChecker(
            namespace="apps", release_name=release_name
        )
        for attempt in range(2):
            if not checker.check_pods_with_polling()["status"]:
                raise RuntimeError(f"集群 {cluster_id} 的资源未通过健康检查")
            if attempt == 0:
                pendulum_sleep(2, 1)
        check_application_attempt()
        checkpoint_application("verified")
        cluster = update_cluster_status(cluster_id, ClusterStatus.healthy)
        _notify_cluster(cluster)
    except (TaskLeaseLost, TaskSuperseded):
        raise
    except Exception:
        logger.exception("集群 %s 部署失败", cluster_id)
        _mark_cluster_failed(cluster_id, ClusterStatus.unhealthy)
        raise


@with_log_context(task_id="task_id", cluster_id="cluster_id")
def clean_up_cluster(task_id: int, cluster_id: int):
    logger.info("清理集群资源: %s", cluster_id)
    try:
        cluster = find_cluster_by_id(cluster_id)
        if cluster is None:
            return  # A previously completed cleanup is safe to recover.
        if not cluster.helm_name:
            raise RuntimeError(
                "集群缺少 Helm release 名称，无法确认资源已清理"
            )
        existing = _find_helm_release(cluster.helm_name)
        confirmed = application_phase() == "cleanup_confirmed"
        if confirmed:
            if existing is not None:
                raise RuntimeError(
                    "清理检查点之后出现同名 release，保留记录并请人工核实"
                )
        elif existing is None:
            if cluster.status != ClusterStatus.pending.value:
                raise RuntimeError(
                    "Helm release 记录不存在，但不能确认 Kubernetes 资源已清理；"
                    "保留管理记录，请人工核实残留资源后处理"
                )
        else:
            if existing.get("status") == "uninstalled":
                raise RuntimeError(
                    "Helm release 已标记卸载，但不能确认资源已清理；"
                    "保留管理记录，请人工核实残留资源后处理"
                )
            # Persist before invoking Helm: it may purge release history
            # even when waiting for deletion fails, or the process may
            # crash.
            cluster = update_cluster_status(cluster_id, ClusterStatus.cleaning)
            _notify_cluster(cluster)
            _helm_command(
                [
                    "uninstall",
                    cluster.helm_name,
                    "-n",
                    "apps",
                    "--wait",
                    "--cascade",
                    "foreground",
                    "--timeout",
                    "5m",
                ]
            )
        checkpoint_application("cleanup_confirmed")
        remove_cluster_by_id(cluster_id)
        _notify({"action": "refresh_clusters"})
    except (TaskLeaseLost, TaskSuperseded):
        raise
    except Exception:
        logger.exception("集群 %s 清理失败，保留管理记录", cluster_id)
        _mark_cluster_failed(cluster_id, ClusterStatus.anomaly)
        raise


def _helm_command(arguments: list[str]):
    check_application_attempt()
    result = execute_command(
        ["helm", *arguments],
        timeout=360,
        env={"KUBECONFIG": "/etc/kubernetes/admin.conf"},
    )
    check_application_attempt()
    if result.is_failure():
        raise RuntimeError(f"Helm 操作失败: {result.get_error_lines()}")
    return result


def _find_helm_release(release_name: str) -> Optional[dict[str, Any]]:
    # --ignore-not-found can swallow history-query errors in Helm.
    # Inspect structured discovery instead, without treating command
    # failure as absence.
    result = _helm_command(
        [
            "list",
            "--all",
            "-n",
            "apps",
            "--filter",
            f"^{re.escape(release_name)}$",
            "--output",
            "json",
        ]
    )
    releases = json.loads("\n".join(result.get_output_lines()))
    if (
        not isinstance(releases, list)
        or len(releases) > 1
        or any(
            not isinstance(item, dict)
            or item.get("name") != release_name
            or not isinstance(item.get("status"), str)
            for item in releases
        )
    ):
        raise RuntimeError("Helm release 查询返回了无效数据")
    return releases[0] if releases else None


def _notify(message: dict[str, Any]) -> None:
    try:
        from core.orm.notifications import publish_cluster_change

        publish_cluster_change()
    except Exception:
        logger.exception("集群通知发送失败")


def _notify_cluster(cluster: ClusterSchema) -> None:
    try:
        _notify(
            {
                "action": "update_cluster",
                "data": cluster.model_dump(mode="json"),
            }
        )
    except Exception:
        logger.exception("集群通知序列化失败")


def _mark_cluster_failed(cluster_id: int, status: ClusterStatus) -> None:
    try:
        _notify_cluster(update_cluster_status(cluster_id, status))
    except Exception:
        logger.exception("无法更新集群 %s 的失败状态", cluster_id)


def _verify_release_inputs(cluster: ClusterSchema, release: dict):
    expected_chart = f"{cluster.helm_chart}-{cluster.helm_chart_version}"
    if release.get("chart") != expected_chart:
        raise RuntimeError("已有 Helm release 的 Chart 或版本不匹配，拒绝接管")
    result = _helm_command(
        [
            "get",
            "values",
            cluster.helm_name or "",
            "-n",
            "apps",
            "--output",
            "json",
        ]
    )
    values = json.loads("\n".join(result.get_output_lines()))
    if values is None:
        values = {}
    if not isinstance(values, dict) or json.dumps(
        values, sort_keys=True, allow_nan=False
    ) != json.dumps(
        cluster.helm_config or {}, sort_keys=True, allow_nan=False
    ):
        raise RuntimeError("已有 Helm release 的配置与任务不匹配，拒绝接管")
