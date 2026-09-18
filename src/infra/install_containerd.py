"""安装 containerd"""
from io import StringIO
import os
from pyinfra.operations import server
from pyinfra.context import host

from _offline_transfer import op_timeout, pull, pull_to_file

deploy_src = host.data.deploy_src
containerd_dir = os.path.join(deploy_src, "containerd")
containerd_path = os.path.join(
    containerd_dir, "containerd-2.1.3-linux-amd64.tar.gz")
certs_path = os.path.join(containerd_dir, "certs.d.tar.gz")
kata_path = os.path.join(containerd_dir, "kata-static-3.18.0-amd64.tar.gz")
master_ip = host.data.master_ip

target_containerd_dir = "/opt/containerd"

# 移除默认containerd
server.yum.packages(name="Remove default containerd package",
                    packages=["containerd"], present=False)

# 解压containerd
server.files.directory(
    name=f"Create {target_containerd_dir} directory", path=target_containerd_dir)

if "master" not in host.groups:
    command, timeout = pull(
        f"sftp://{master_ip}{containerd_path}", containerd_path,
        f"tar zxf - -C {target_containerd_dir}")
else:
    command = f"tar zxf {containerd_path} -C {target_containerd_dir}"
    timeout = op_timeout(containerd_path)

server.shell(
    name=f"Extract containerd to {target_containerd_dir}",
    commands=command,
    _timeout=timeout,
    _retries=4,
    _retry_delay=10,
)

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
if "master" not in host.groups:
    runc_src = os.path.join(containerd_dir, "runc.amd64")
    runc_pull, runc_timeout = pull_to_file(
        f"sftp://{master_ip}{runc_src}", runc_src, "/usr/local/bin/runc")
    server.shell(
        name="Copy runc binary to /usr/local/bin/runc",
        commands=[
            runc_pull,
            "chmod 755 /usr/local/bin/runc"
        ],
        _timeout=runc_timeout,
        _retries=4,
        _retry_delay=10,
    )


# kata相关
if "master" not in host.groups:
    command, timeout = pull(
        f"sftp://{master_ip}{kata_path}", kata_path, "tar zxf - -C /opt")
else:
    command = f"tar zxf {kata_path} -C /opt"
    timeout = op_timeout(kata_path)
server.shell(
    name="Extract Kata Containers to /opt",
    commands=command,
    _timeout=timeout,
    _retries=4,
    _retry_delay=10,
)
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
if "master" not in host.groups:
    config_pull, config_timeout = pull_to_file(
        f"sftp://{master_ip}{config_path}", config_path,
        "/etc/containerd/config.toml")
    server.shell(
        name="Configure containerd with config.toml",
        commands=[
            "mkdir -p /etc/containerd",
            config_pull,
        ],
        _timeout=config_timeout,
        _retries=4,
        _retry_delay=10,
    )

# proxy
if "master" not in host.groups:
    command, timeout = pull(
        f"sftp://{master_ip}{certs_path}", certs_path,
        "tar zxf - -C /etc/containerd")
else:
    command = f"tar zxf {certs_path} -C /etc/containerd"
    timeout = op_timeout(certs_path)

server.shell(
    name="Extract certs.d to /etc/containerd",
    commands=command,
    _timeout=timeout,
    _retries=4,
    _retry_delay=10,
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
