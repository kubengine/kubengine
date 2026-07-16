"""kubenetes添加节点"""
from pyinfra.operations import server
from pyinfra.context import host, inventory
from pyinfra.facts.server import Command

data = host.data
vip = data.control_plane_endpoint or ""

if "worker" in host.groups:
    master_host = inventory.get_group("master")[0]
    join_command = master_host.get_fact(
        Command,
        "kubeadm token create --print-join-command",
        _retries=10,
        _retry_delay=20
    )

    # 如果配置了VIP，将 join 目标替换为 VIP
    if vip:
        import re
        join_command = re.sub(
            r'https?://[^:]+:\d+',
            f'https://{vip}:6443',
            join_command
        )

    # Execute join command to add worker node to the cluster
    server.shell(
        name="Join worker node to Kubernetes cluster",
        commands=join_command
    )
