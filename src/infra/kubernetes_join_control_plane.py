"""附加Master节点加入控制面（高可用模式）"""
from pyinfra.operations import server
from pyinfra.context import host, inventory
from pyinfra.facts.server import Command

# 仅 additional_master 组的节点执行此操作
if "additional_master" not in host.groups:
    exit(0)

data = host.data
vip = data.control_plane_endpoint or ""

# 从第一个 master 节点获取 join 命令（含 --control-plane）
master_host = inventory.get_group("master")[0]
join_command_raw = master_host.get_fact(
    Command,
    "kubeadm token create --print-join-command",
    _retries=10,
    _retry_delay=20
)
join_command = join_command_raw.strip()

# 将 join 的目标地址改为 VIP（如果配置了VIP）
if vip:
    import re
    # 替换如 172.31.96.11:6443 为 172.31.96.100:6443
    join_command = re.sub(
        r'--discovery-token-ca-cert-hash\s+\S+',
        '',  # 去掉自动获取的 hash（后续加回来）
        join_command
    )
    # 重新从 master 获取完整的 hash
    join_hash = master_host.get_fact(
        Command,
        "kubeadm token create --print-join-command",
        _retries=10,
        _retry_delay=20
    ).strip()
    hash_match = re.search(r'--discovery-token-ca-cert-hash\s+(\S+)', join_hash)
    if hash_match:
        join_command = f"kubeadm join {vip}:6443 --control-plane --certificate-key $(kubeadm init phase upload-certs --upload-certs 2>/dev/null | tail -1) --token {join_command.split('--token ')[1].split()[0]} --discovery-token-ca-cert-hash {hash_match.group(1)} --ignore-preflight-errors=all"
else:
    join_command = f"{join_command} --control-plane --ignore-preflight-errors=all"

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
