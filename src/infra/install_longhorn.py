"""安装longhorn"""
from io import StringIO
import os
from pyinfra.operations import server, python
from pyinfra.context import host
from core.misc.ca import k8s_create_tls

data = host.data

repo_name = "kubengine_repo"
master_ip = data.master_ip
deploy_src = data.deploy_src
domain = data.domain
loadbalancer_ip = data.loadbalancer_ip
images_path = os.path.join(
    deploy_src, "images", "longhorn.images.v1.9.1.tar.gz")
helm_charts_dir = os.path.join(deploy_src, "charts", "longhorn")

# 安装 open-iscsi
if "worker" in host.groups:
    baseurl = f"sftp://{master_ip}{deploy_src}/repo"
else:
    baseurl = f"file:///{deploy_src}/repo"
server.yum.repo(
    name="Add kubengine yum repository",
    src=repo_name,
    baseurl=baseurl,
    gpgcheck=False
)
server.yum.packages(
    name="Install open-iscsi",
    packages=["open-iscsi"],
    extra_install_args=f"--disablerepo=* --enablerepo={repo_name}"
)
server.yum.repo(
    name="Remove kubengine yum repository",
    src=repo_name,
    present=False
)
server.shell(name="Configure iscsi initiator name",
             commands='echo "InitiatorName=$(/sbin/iscsi-iname)" > /etc/iscsi/initiatorname.iscsi')
server.systemd.service(
    name="Start and enable iscsid service",
    service="iscsid",
    enabled=True
)

# 加载 uio_pci_generic 模块
server.modprobe(name="Load uio_pci_generic module", module="uio_pci_generic")
server.files.put(
    name="Create /etc/modules-load.d/uio_pci_generic.conf file for uio_pci_generic",
    src=StringIO("uio_pci_generic"),
    dest="/etc/modules-load.d/uio_pci_generic.conf"
)
# 加载 vfio_pci 模块
server.modprobe(name="Load vfio_pci module", module="vfio_pci")
server.files.put(
    name="Create /etc/modules-load.d/vfio_pci.conf file for vfio_pci module",
    src=StringIO("vfio_pci"),
    dest="/etc/modules-load.d/vfio_pci.conf"
)
# 加载 iscsi_tcp 模块
server.modprobe(name="Load iscsi_tcp module", module="iscsi_tcp")
server.files.put(
    name="Create /etc/modules-load.d/iscsi_tcp.conf file for iscsi_tcp module",
    src=StringIO("iscsi_tcp"),
    dest="/etc/modules-load.d/iscsi_tcp.conf"
)

# 加载离线镜像
if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{images_path} -o - | ctr -n k8s.io i import -"
else:
    command = f"ctr -n k8s.io i import {images_path}"
server.shell(name="Load offline longhorn images", commands=command)

if "master" in host.groups:
    python.call(name="Create TLS cert for longhorn-system namespace", function=k8s_create_tls,
                namespace="longhorn-system", tls_name="longhorn-tls")
    server.shell(
        name="Install longhorn",
        commands=" ".join(
            ["KUBECONFIG=/etc/kubernetes/admin.conf helm", "install",
             "longhorn",
             helm_charts_dir,
             "-n", "longhorn-system",
             "--create-namespace",
             "-f", f"{helm_charts_dir}/values.yaml"])
    )

server.files.line(
    name=f"Add longhorn.{domain} to /etc/hosts",
    path="/etc/hosts",
    line=f"{loadbalancer_ip} longhorn.{domain}",
    present=True,
)
