"""安装harbor"""
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
    deploy_src, "images", "harbor.images.v2.14.0.tar.gz")
helm_charts_dir = os.path.join(deploy_src, "charts", "harbor")

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
    name="Load offline harbor images",
    commands=command,
    _timeout=timeout,
    _retries=4,
    _retry_delay=10,
)


if "master" in host.groups:
    python.call(name="Create TLS cert for harbor-system namespace", function=k8s_create_tls,
                namespace="harbor-system", tls_name="harbor-tls")
    values_template_file = os.path.join(helm_charts_dir, "values.yaml.j2")
    values_file = os.path.join(helm_charts_dir, "values.yaml")
    server.files.template(name="Gen harbor helm chart values file",
                          src=values_template_file,
                          dest=values_file,
                          domain=domain)
    server.shell(name="Install harbor", commands=" ".join(["KUBECONFIG=/etc/kubernetes/admin.conf helm", "install",
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
