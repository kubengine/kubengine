"""kubenetes添加节点"""
from pyinfra.operations import server
from pyinfra.context import host, inventory
from pyinfra.facts.server import Command

if "worker" in host.groups:
    master_host = inventory.get_group("master")
    join_command = master_host[0].get_fact(
        Command,
        "kubeadm token create --print-join-command",
        _retries=10,
        _retry_delay=20
    )
    # Execute join command to add worker node to the cluster
    server.shell(
        name="Join worker node to Kubernetes cluster",
        commands=join_command
    )
