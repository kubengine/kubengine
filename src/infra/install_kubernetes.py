"""安装kubernetes"""
from io import StringIO
import os
from pyinfra.operations import server
from pyinfra.context import host

from _offline_transfer import YUM_OP_SECONDS, import_seconds, op_timeout, pull

data = host.data

repo_name = "kubengine_repo"
master_ip = data.master_ip
deploy_src = data.deploy_src
nameserver = data.nameserver
service_cidr = data.service_cidr
pod_cidr = data.pod_cidr
master_schedule = data.master_schedule
# 安装 kubelet kubectl kubeadm
if "master" not in host.groups:
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
    name="Install kubelet, kubectl and kubeadm",
    packages=["kubelet", "kubectl", "kubeadm"],
    extra_install_args=f"--disablerepo=* --enablerepo={repo_name}",
    _timeout=YUM_OP_SECONDS,
    _retries=1,
    _retry_delay=10,
)
server.yum.repo(
    name="Remove kubengine yum repository",
    src=repo_name,
    present=False
)
server.systemd.service(
    name="Disable and enable kubelet service (keep stopped initially)",
    service="kubelet",
    running=False,
    enabled=True
)

# 配置crictl
server.files.put(
    name="Configure crictl.yaml for containerd",
    dest="/etc/crictl.yaml",
    src=StringIO("""runtime-endpoint: unix:///var/run/containerd/containerd.sock
image-endpoint: unix:///var/run/containerd/containerd.sock
timeout: 10
debug: false
""")
)

# 加载离线镜像
images_file = os.path.join(
    deploy_src, "images", "kubenetes.images.v1.34.0.tar.gz")
images_budget = import_seconds(images_file)
if "master" not in host.groups:
    command, timeout = pull(
        f"sftp://{master_ip}{images_file}", images_file,
        "ctr -n k8s.io i import -", extra_seconds=images_budget)
else:
    command = f"ctr -n k8s.io i import {images_file}"
    timeout = op_timeout(images_file, extra_seconds=images_budget)
server.shell(
    name="Load offline Kubernetes images using ctr",
    commands=command,
    _timeout=timeout,
    _retries=4,
    _retry_delay=10,
)

# 确保 /etc/resolv.conf 中存在 nameserver
for i in nameserver:
    server.files.line(
        name=f"Ensure nameserver {i} in /etc/resolv.conf",
        path="/etc/resolv.conf",
        line=f"nameserver {i}",  # 要检查/添加的目标行
        present=True,  # 确保行存在（不存在则追加）
    )
server.files.line(
    name="Ensure options timeout:2 attempts:1 in /etc/resolv.conf",
    path="/etc/resolv.conf",
    line="options timeout:2 attempts:1",  # 减少查询超时和尝试次数
    present=True,
)
server.files.file(
    name="Create file /proc/sys/net/ipv4/ip_forward",
    path="/proc/sys/net/ipv4/ip_forward"
)
server.shell(
    name="Enable IPv4 ip_forward in /proc/sys/net/ipv4/ip_forward",
    commands="echo 1 > /proc/sys/net/ipv4/ip_forward"
)

if "master" in host.groups:
    # 确定 control-plane-endpoint：HA模式下用VIP，否则用 master_ip
    cpe = data.control_plane_endpoint or master_ip
    server.shell(
        name="Initialize Kubernetes control plane",
        commands=" ".join(["kubeadm", "init",
                           f"--apiserver-advertise-address={master_ip}",
                           f"--control-plane-endpoint={cpe}",
                           "--kubernetes-version=v1.34.0",
                           f"--service-cidr={service_cidr}",
                           f"--pod-network-cidr={pod_cidr}",
                           "--upload-certs",
                           "--ignore-preflight-errors=all"])
    )
    server.files.line(
        name="Ensure KUBECONFIG is set in /etc/profile for master node",
        path="/etc/profile",
        line="export KUBECONFIG=/etc/kubernetes/admin.conf",  # 要检查/添加的目标行
        present=True,  # 确保行存在（不存在则追加）
    )
    if master_schedule:
        server.shell(name="Taint master node to prevent scheduling",
                     commands="KUBECONFIG=/etc/kubernetes/admin.conf kubectl taint nodes --all node-role.kubernetes.io/control-plane:NoSchedule-")
