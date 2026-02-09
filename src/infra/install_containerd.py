"""安装 containerd"""
from io import StringIO
import os
from pyinfra.operations import server
from pyinfra.context import host

deploy_src = host.data.deploy_src
containerd_dir = os.path.join(deploy_src, "containerd")
containerd_path = os.path.join(
    containerd_dir, "containerd-2.1.3-linux-amd64.tar.gz")
kata_path = os.path.join(containerd_dir, "kata-static-3.18.0-amd64.tar.gz")
master_ip = host.data.master_ip

target_containerd_dir = "/opt/containerd"

# 移除默认containerd
server.yum.packages(name="Remove default containerd package",
                    packages=["containerd"], present=False)

# 解压containerd
server.files.directory(
    name=f"Create {target_containerd_dir} directory", path=target_containerd_dir)

if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{containerd_path} -o - | tar zxf - -C {target_containerd_dir}"
else:
    command = f"tar zxf {containerd_path} -C {target_containerd_dir}"

server.shell(
    name=f"Extract containerd to {target_containerd_dir}", commands=command)

# 创建软链
server.files.link(
    name="Create symlink for containerd-shim-runc-v2",
    path="/usr/local/bin/containerd-shim-runc-v2",
    target=f"{target_containerd_dir}/bin/containerd-shim-runc-v2")
server.files.link(
    name="Create symlink for ctr",
    path="/usr/local/bin/ctr",
    target=f"{target_containerd_dir}/bin/ctr")

# runc
if "master" in host.groups:
    server.files.put(
        name="Copy runc binary to /usr/local/bin/runc",
        dest="/usr/local/bin/runc",
        src=os.path.join(containerd_dir, "runc.amd64"),
        mode="755"
    )
if "worker" in host.groups:
    server.shell(
        name="Copy runc binary to /usr/local/bin/runc",
        commands=[
            f"curl -o /usr/local/bin/runc sftp://{master_ip}{containerd_dir}/runc.amd64",
            "chmod 755 /usr/local/bin/runc"
        ]
    )


# kata相关
if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{kata_path} -o - | tar zxf - -C /opt"
else:
    command = f"tar zxf {kata_path} -C /opt"
server.shell(
    name="Extract Kata Containers to /opt", commands=command)
server.files.link(
    name="Create symlink for containerd-shim-kata-v2",
    path="/usr/local/bin/containerd-shim-kata-v2",
    target="/opt/kata/bin/containerd-shim-kata-v2")
server.files.link(
    name="Create symlink for kata-runtime",
    path="/usr/local/bin/kata-runtime",
    target="/opt/kata/bin/kata-runtime")
server.files.link(
    name="Create symlink for Kata configuration.toml",
    path="/etc/kata-containers/configuration.toml",
    target="/opt/kata/share/defaults/kata-containers/configuration.toml")
# config
config_path = os.path.join(containerd_dir, "config.toml")
if "master" in host.groups:
    server.files.put(
        name="Configure containerd with config.toml",
        dest="/etc/containerd/config.toml",
        src=config_path)
if "worker" in host.groups:
    server.shell(
        name="Configure containerd with config.toml",
        commands=[
            f"mkdir -p /etc/containerd",
            "curl -o /etc/containerd/config.toml sftp://{master_ip}{config_path}"
        ]
    )

# systemd 管理 containerd
server.files.put(
    name="Create containerd systemd service file",
    dest="/usr/lib/systemd/system/containerd.service",
    src=StringIO(f"""[Unit]
Description=containerd container runtime
Documentation=https://containerd.io
After=network.target

[Service]
ExecStartPre=/sbin/modprobe overlay
ExecStart={target_containerd_dir}/bin/containerd
Delegate=yes
KillMode=process

[Install]
WantedBy=multi-user.target"""))
server.systemd.daemon_reload(
    name="Reload systemd daemon for containerd")
server.systemd.service(
    name="Enable containerd service",
    service="containerd",
    enabled=True)
