"""部署metrics-server组件"""
import os
from pyinfra.operations import server
from pyinfra.context import host

data = host.data
master_ip = data.master_ip
deploy_src = data.deploy_src
images_path = os.path.join(
    deploy_src, "images", "metrics-server.images.v0.8.0.tar.gz")
helm_charts_dir = os.path.join(deploy_src, "charts", "metrics-server")

# 加载离线镜像
if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{images_path} -o - | ctr -n k8s.io i import -"
else:
    command = f"ctr -n k8s.io i import {images_path}"
server.shell(name="Load offline metrics-server images", commands=command)

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
