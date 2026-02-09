"""安装harbor"""
import os
from pyinfra.operations import server, python
from pyinfra.context import host
from core.misc.ca import k8s_create_tls

data = host.data
master_ip = data.master_ip
deploy_src = data.deploy_src
domain = data.domain
loadbalancer_ip = data.loadbalancer_ip
images_path = os.path.join(
    deploy_src, "images", "harbor.images.v2.14.0.tar.gz")
helm_charts_dir = os.path.join(deploy_src, "charts", "harbor")

# 加载离线镜像
if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{images_path} -o - | ctr -n k8s.io i import -"
else:
    command = f"ctr -n k8s.io i import {images_path}"
server.shell(name="Load offline harbor images", commands=command)


if "master" in host.groups:
    python.call(name="Create TLS cert for harbor-system namespace", function=k8s_create_tls,
                namespace="harbor-system", tls_name="harbor-tls")
    server.shell(name="Install harbor", commands=" ".join(["KUBECONFIG=/etc/kubernetes/admin.conf  helm", "install",
                                                           "harbor",
                                                           helm_charts_dir,
                                                           "-n", "harbor-system",
                                                           "--create-namespace",
                                                           "-f", f"{helm_charts_dir}/values.yaml"]))

server.files.line(
    name=f"Add harbor.{domain} to /etc/hosts",
    path="/etc/hosts",
    line=f"{loadbalancer_ip} {domain}",
    present=True,
)

# 导入harbor离线 images / helm-charts
# if "master" in host.group_data:
#     pass
