"""部署 cert-manager 组件

为集群提供 cert-manager.io/v1 CRD（Certificate / Issuer），
供 Flink Kubernetes Operator 等依赖证书准入的组件使用。

离线制品要求（需提前放入 offline-deploy）：
  - 镜像: {deploy_src}/images/cert-manager.images.v1.16.3.tar.gz
  - Chart: {deploy_src}/charts/cert-manager/  （解压后的 helm chart 目录）
"""
import os
from pyinfra.operations import server
from pyinfra.context import host

data = host.data
deploy_src = data.deploy_src

helm_charts_dir = os.path.join(deploy_src, "charts", "cert-manager")

if "master" in host.groups:
    server.shell(
        name="Install cert-manager",
        commands=" ".join(
            [
                "KUBECONFIG=/etc/kubernetes/admin.conf helm", "install",
                "cert-manager",
                helm_charts_dir,
                "-n", "cert-manager",
                "--create-namespace",
                "--set", "installCRDs=true",
                "--set", "global.leaderElection.namespace=cert-manager",
                # 关闭安装后自检 Job（Helm post-install hook）。
                # 该 Job 仅做 webhook 健康校验，非功能组件；
                # 离线环境中常因 webhook 就绪晚于 Job 首次执行而反复重试，
                # 导致 helm install 阻塞不返回。
                "--set", "startupapicheck.enabled=false",
                "-f", f"{helm_charts_dir}/values.yaml",
                # 兜底超时，避免异常情况下无限挂起
                "--timeout", "5m"
            ]
        )
    )
