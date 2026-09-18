"""安装KubeBoard"""
import os
from pyinfra.operations import server, python
from pyinfra.context import host
from core.misc.ca import k8s_create_tls

from _offline_transfer import import_seconds, op_timeout, pull

data = host.data
master_ip = data.master_ip
deploy_src = data.deploy_src
domain = data.domain
loadbalancer_ip = data.loadbalancer_ip
images_path = os.path.join(
    deploy_src, "images", "kuboard.images.v4.tar.gz")
helm_charts_dir = os.path.join(deploy_src, "charts", "kuboard")

images_budget = import_seconds(images_path)

# 加载离线镜像
if "master" not in host.groups:
    command, timeout = pull(
        f"sftp://{master_ip}{images_path}", images_path,
        "ctr -n k8s.io i import -", extra_seconds=images_budget)
else:
    command = f"ctr -n k8s.io i import {images_path}"
    timeout = op_timeout(images_path, extra_seconds=images_budget)
server.shell(
    name="Load offline kuboard images",
    commands=command,
    _timeout=timeout,
    _retries=4,
    _retry_delay=10,
)


if "master" in host.groups:
    # Ingress TLS 密钥与 dashboard/harbor/longhorn 安装时一致：
    # 安装前在目标命名空间预创建 TLS Secret（kuboard-tls），chart 仅引用不创建
    python.call(name="Create TLS cert for kuboard-system namespace", function=k8s_create_tls,
                namespace="kuboard-system", tls_name="kuboard-tls")
    values_template_file = os.path.join(helm_charts_dir, "values.yaml.j2")
    values_file = os.path.join(helm_charts_dir, "values.yaml")
    server.files.template(name="Gen kuboard helm chart values file",
                          src=values_template_file,
                          dest=values_file,
                          domain=domain)
    server.shell(name="Install kuboard", commands=" ".join(["KUBECONFIG=/etc/kubernetes/admin.conf helm", "install",
                                                            "kuboard",
                                                            helm_charts_dir,
                                                            "-n", "kuboard-system",
                                                            "--create-namespace",
                                                            "-f", f"{helm_charts_dir}/values.yaml",
                                                            # 兜底超时，避免镜像导入/存储卷创建慢时无限挂起
                                                            "--timeout", "10m"]))

server.files.line(
    name=f"Add kuboard.{domain} to /etc/hosts",
    path="/etc/hosts",
    line=f"{loadbalancer_ip} kuboard.{domain}",
    present=True,
)
