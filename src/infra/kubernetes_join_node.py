"""kubenetes添加节点"""

import shlex

from _kubernetes_bootstrap import guarded_kubeadm, join_arguments
from pyinfra.context import host, inventory
from pyinfra.facts.server import Command
from pyinfra.operations import server

data = host.data
vip = data.control_plane_endpoint or ""

if "worker" in host.groups:
    master_host = inventory.get_group("master")[0]
    join_command = master_host.get_fact(
        Command,
        "kubeadm token create --print-join-command",
        _retries=10,
        _retry_delay=20,
    )

    join_args = join_arguments(join_command, vip)

    # Execute join command to add worker node to the cluster
    server.shell(
        name="Join worker node to Kubernetes cluster",
        commands=guarded_kubeadm(shlex.join(join_args), "worker"),
    )
