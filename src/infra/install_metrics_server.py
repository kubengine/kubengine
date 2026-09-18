"""部署metrics-server组件"""
import os
from pyinfra.operations import server
from pyinfra.context import host

from _offline_transfer import import_seconds, op_timeout, pull

data = host.data
master_ip = data.master_ip
deploy_src = data.deploy_src
images_path = os.path.join(
    deploy_src, "images", "metrics-server.images.v0.8.0.tar.gz")
helm_charts_dir = os.path.join(deploy_src, "charts", "metrics-server")

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
    name="Load offline metrics-server images",
    commands=command,
    _timeout=timeout,
    _retries=4,
    _retry_delay=10,
)

if "master" in host.groups:
    server.shell(
        name="Install metrics-server",
        commands=" ".join(
            [
                "KUBECONFIG=/etc/kubernetes/admin.conf helm", "install",
                "metrics-server",
                helm_charts_dir,
                "-n", "kube-system",
                "--create-namespace",
                "-f", f"{helm_charts_dir}/values.yaml"
            ]
        )
    )
