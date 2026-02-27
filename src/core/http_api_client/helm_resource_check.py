"""
Helm 资源检查模块

提供与 Kubernetes API 交互的类，用于检查 Helm Release 关联的 Pod 资源状态。
支持轮询等待 Pod 就绪、健康状态检查等功能。
"""

import os
import time
from typing import Any, Dict, List, Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from core.logger import get_logger

logger = get_logger(__name__)

# 配置常量
TARGET_NAMESPACE = "apps"
HELM_RELEASE_NAME = "d723b3d7361cf4145910778c04369cfca"

# 轮询相关配置
POLL_INTERVAL_SECONDS = 5  # 每次轮询的间隔时间（秒）
MAX_POLL_TIMES = 60  # 最大轮询次数（总超时时间 = POLL_INTERVAL_SECONDS * MAX_POLL_TIMES）


class HelmResourceChecker:
    """
    Helm 资源检查类

    使用官方 Kubernetes Python SDK 检查 Helm Release 关联的资源状态。

    Attributes:
        namespace: 目标命名空间
        release_name: Helm Release 名称
        helm_label_selector: Helm 标签选择器
        apps_api: Kubernetes Apps V1 API 客户端
        core_api: Kubernetes Core V1 API 客户端
    """

    def __init__(self, namespace: str, release_name: str, kubeconfig_path: Optional[str] = None) -> None:
        """
        初始化 Helm 资源检查器

        Args:
            namespace: 目标命名空间
            release_name: Helm Release 名称
            kubeconfig_path: kubeconfig 文件路径
        """
        self.namespace = namespace
        self.release_name = release_name
        self.helm_label_selector = (
            f"app.kubernetes.io/instance={release_name},"
            f"app.kubernetes.io/managed-by=Helm"
        )

        # 加载 Kubernetes 配置
        try:
            config.load_incluster_config()  # 集群内优先
            logger.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            try:
                if kubeconfig_path:
                    config.load_kube_config(config_file=kubeconfig_path)
                else:
                    # 尝试默认路径
                    default_kubeconfig = "/etc/kubernetes/admin.conf"
                    if os.path.exists(default_kubeconfig):
                        config.load_kube_config(config_file=default_kubeconfig)
                    else:
                        # 尝试用户默认 kubeconfig
                        config.load_kube_config()
                logger.info("Loaded kubeconfig Kubernetes configuration")
            except Exception as e:
                logger.error(
                    f"Failed to load Kubernetes configuration: {e}")
                raise

        # 初始化 API 客户端
        self.apps_api = client.AppsV1Api()
        self.core_api = client.CoreV1Api()

    def _check_single_pod_health(self, pod: Any) -> Dict[str, Any]:
        """
        检查单个 Pod 的健康状态

        Args:
            pod: Pod 对象

        Returns:
            包含 Pod 健康状态信息的字典：
                - pod_name: Pod 名称
                - phase: Pod 阶段
                - ready: 是否就绪
                - restart_count: 容器重启次数
                - is_normal: 是否正常
                - is_abnormal: 是否异常
                - is_uncertain: 状态是否未明确
        """
        pod_name = pod.metadata.name
        pod_phase = pod.status.phase

        # 判断 Pod 是否就绪（Ready 探针通过）
        pod_ready = any(cond.type == "Ready" and cond.status == "True"
                        for cond in pod.status.conditions or [])  # type: ignore

        # 统计容器重启次数
        restart_count = sum(
            container.restart_count
            for container in pod.status.container_statuses or [])  # type: ignore

        # 定义「明确结果状态」
        is_normal = (
            pod_phase in ["Running",
                          "Completed"] and pod_ready and restart_count < 5
        )
        is_abnormal = (
            pod_phase == "Failed" or restart_count >= 5
        )
        is_uncertain = not (is_normal or is_abnormal)

        return {
            "pod_name": pod_name,
            "phase": pod_phase,
            "ready": pod_ready,
            "restart_count": restart_count,
            "is_normal": is_normal,
            "is_abnormal": is_abnormal,
            "is_uncertain": is_uncertain,
        }

    def check_pods_with_polling(self) -> Dict[str, Any]:
        """
        带轮询等待的 Pod 检查

        持续轮询 Pod 状态直到：
        - 所有 Pod 都达到明确状态（正常或异常）
        - 达到最大轮询次数

        Returns:
            包含检查结果的字典：
                - status: 整体状态（True/False）
                - details: 详细信息列表
        """
        poll_times = 0
        overall_pod_result: dict[str, Any] = {"status": True, "details": []}

        logger.info(
            f"开始轮询 Pod 状态（间隔 {POLL_INTERVAL_SECONDS} 秒，"
            f"最大 {MAX_POLL_TIMES} 次）..."
        )

        while poll_times < MAX_POLL_TIMES:
            poll_times += 1
            uncertain_pod_names: List[str] = []
            current_pod_result: dict[str, Any] = {
                "status": True, "details": []}

            # 1. 查询当前 Pod 列表
            try:
                pods = self.core_api.list_namespaced_pod(  # type: ignore
                    namespace=self.namespace,
                    label_selector=self.helm_label_selector,
                )
            except ApiException as e:
                error_msg = f"轮询第 {poll_times} 次失败：{e.reason}({e.status})"
                logger.error(error_msg)
                return {"status": False, "details": [error_msg]}

            if not pods.items:
                current_pod_result["details"].append("无 Pod 资源")
                break

            # 2. 检查每个 Pod 的状态
            for pod in pods.items:  # type: ignore
                pod_health = self._check_single_pod_health(pod)

                if pod_health["is_normal"]:
                    current_pod_result["details"].append(
                        f"Pod {pod_health['pod_name']} 状态正常"
                    )
                    logger.debug(f"Pod {pod_health['pod_name']} 状态正常")
                elif pod_health["is_abnormal"]:
                    current_pod_result["status"] = False
                    error_detail = (
                        f"Pod {pod_health['pod_name']} 异常："
                        f"状态 {pod_health['phase']}，"
                        f"就绪 {pod_health['ready']}，"
                        f"重启 {pod_health['restart_count']} 次"
                    )
                    current_pod_result["details"].append(error_detail)
                    logger.warning(error_detail)
                else:
                    current_pod_result["status"] = False
                    uncertain_pod_names.append(pod_health["pod_name"])
                    detail_msg = (
                        f"轮询第 {poll_times} 次："
                        f"Pod {pod_health['pod_name']} 状态未明确（创建中/探针未就绪），"
                        f"当前状态 {pod_health['phase']}，就绪 {pod_health['ready']}"
                    )
                    current_pod_result["details"].append(detail_msg)
                    logger.debug(detail_msg)

            # 3. 判断是否需要继续轮询：无未明确状态的 Pod，直接终止循环
            if not uncertain_pod_names:
                overall_pod_result = current_pod_result
                logger.info("所有 Pod 均达到明确状态")
                break

            # 4. 未达到明确状态，休眠后继续轮询（最后一次轮询不休眠）
            if poll_times < MAX_POLL_TIMES:
                logger.info(
                    f"仍有 {len(uncertain_pod_names)} 个 Pod 状态未明确，"
                    f"{POLL_INTERVAL_SECONDS} 秒后继续轮询..."
                )
                time.sleep(POLL_INTERVAL_SECONDS)
            else:
                # 5. 达到最大轮询次数，终止并返回最终结果
                timeout_msg = (
                    f"已达到最大轮询次数 {MAX_POLL_TIMES}，"
                    f"停止等待，未明确状态的 Pod 视为异常"
                )
                current_pod_result["details"].append(timeout_msg)
                logger.warning(timeout_msg)
                overall_pod_result = current_pod_result

        return overall_pod_result


# 执行完整轮询检查（工作负载 + Pod）
if __name__ == "__main__":
    checker = HelmResourceChecker(
        namespace=TARGET_NAMESPACE,
        release_name=HELM_RELEASE_NAME,
    )

    # 调用完整轮询检查方法
    pod_status = checker.check_pods_with_polling()

    # 打印最终结果
    print("\n" + "=" * 60)
    print(f"Helm Release: {HELM_RELEASE_NAME}")
    print(f"命名空间: {TARGET_NAMESPACE}")
    print(f"整体状态: {'正常' if pod_status['status'] else '异常'}")
    print("详细信息:")
    for detail in pod_status["details"]:
        print(f"  - {detail}")
    print("=" * 60)
