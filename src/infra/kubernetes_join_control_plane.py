"""附加Master节点加入控制面（高可用模式）"""
import re
from pyinfra.operations import server
from pyinfra.context import host, inventory
from pyinfra.facts.server import Command

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
    ).strip()

    # 从第一个 master 节点获取 certificate-key（仅第一个 additional master 调用 upload-certs，
    # 后续 additional master 复用同一 key，避免重复 upload 覆盖 secret 导致认证失败）
    cert_key = globals().get("_cached_cert_key")
    if not cert_key:
        cert_key_raw = master_host.get_fact(
            Command,
            "kubeadm init phase upload-certs --upload-certs",
            _retries=10,
            _retry_delay=20
        ).strip()
        cert_key_match = re.search(r'certificate key:\s*(\S+)', cert_key_raw)
        cert_key = cert_key_match.group(1) if cert_key_match else ""
        globals()["_cached_cert_key"] = cert_key

    # 解析 token 和 discovery-token-ca-cert-hash
    token_match = re.search(r'--token\s+(\S+)', join_command_raw)
    token = token_match.group(1) if token_match else ""
    hash_match = re.search(r'--discovery-token-ca-cert-hash\s+(\S+)', join_command_raw)
    ca_cert_hash = hash_match.group(1) if hash_match else ""

    # 确定 join 目标地址：HA 模式用 VIP，否则用 master IP
    endpoint = f"{vip}:6443" if vip else re.search(r'join\s+(\S+)', join_command_raw).group(1)

    # 构造 join 命令
    join_command = (
        f"kubeadm join {endpoint} --control-plane "
        f"--certificate-key {cert_key} "
        f"--token {token} "
        f"--discovery-token-ca-cert-hash {ca_cert_hash} "
        f"--ignore-preflight-errors=all"
    )

    # 执行 join
    server.shell(
        name="Join additional master node to control plane",
        commands=join_command
    )

    # 配置 KUBECONFIG
    server.files.line(
        name="Ensure KUBECONFIG is set in /etc/profile for additional master",
        path="/etc/profile",
        line="export KUBECONFIG=/etc/kubernetes/admin.conf",
        present=True
    )
