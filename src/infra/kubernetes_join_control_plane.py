"""附加Master节点加入控制面（高可用模式）"""
import re
import shlex
from pyinfra.operations import server
from pyinfra.context import host, inventory
from pyinfra.facts.server import Command
from _kubernetes_bootstrap import guarded_kubeadm, join_arguments

# 仅 additional_master 组的节点执行此操作
if "additional_master" in host.groups:
    data = host.data
    vip = data.control_plane_endpoint or ""

    # 从第一个 master 节点获取 join 命令（含 token 和 hash）
    master_host = inventory.get_group("master")[0]
    join_command_raw = master_host.get_fact(
        Command,
        "kubeadm token create --print-join-command",
        _retries=10,
        _retry_delay=20
    )
    join_args = join_arguments(join_command_raw, vip)

    # 从第一个 master 节点获取 certificate-key（仅第一个 additional master 调用 upload-certs，
    # 后续 additional master 复用同一 key，避免重复 upload 覆盖 secret 导致认证失败）
    cert_key = globals().get("_cached_cert_key")
    if not cert_key:
        cert_key_raw = master_host.get_fact(
            Command,
            "kubeadm init phase upload-certs --upload-certs",
            _retries=10,
            _retry_delay=20
        )
        if not isinstance(cert_key_raw, str):
            raise RuntimeError("Unable to obtain the control-plane certificate key")
        cert_key_match = re.search(r'certificate key:\s*(\S+)', cert_key_raw)
        cert_key = cert_key_match.group(1) if cert_key_match else ""
        if not re.fullmatch(r"[a-fA-F0-9]{64}", cert_key):
            raise RuntimeError("Invalid control-plane certificate key")
        globals()["_cached_cert_key"] = cert_key

    join_args.extend(["--control-plane", "--certificate-key", cert_key])
    join_command = shlex.join(join_args)

    # 执行 join
    server.shell(
        name="Join additional master node to control plane",
        commands=guarded_kubeadm(join_command, "master")
    )

    # 配置 KUBECONFIG
    server.files.line(
        name="Ensure KUBECONFIG is set in /etc/profile for additional master",
        path="/etc/profile",
        line="export KUBECONFIG=/etc/kubernetes/admin.conf",
        present=True
    )
