"""
Conservative runtime guards for one-time kubeadm bootstrap operations.
"""

import ipaddress
import re
import shlex
from urllib.parse import urlsplit


def join_endpoint(value: str) -> str:
    """Normalize a kubeadm host[:port], preserving any explicit port."""
    if not value or any(character.isspace() for character in value):
        raise ValueError("Invalid Kubernetes control-plane endpoint")
    parsed = urlsplit("//" + value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Control-plane endpoint must be a host or host:port")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Missing control-plane hostname")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid control-plane port")
    if ":" in hostname:
        ipaddress.IPv6Address(hostname)
        hostname = f"[{hostname}]"
    elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", hostname):
        raise ValueError("Invalid control-plane hostname")
    return f"{hostname}:{port or 6443}"


def join_arguments(command: str, endpoint_override: str = "") -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise RuntimeError("Unable to obtain a Kubernetes join command")
    args = shlex.split(command)
    if len(args) < 3 or args[:2] != ["kubeadm", "join"]:
        raise RuntimeError("Unexpected Kubernetes join command")
    args[2] = join_endpoint(endpoint_override or args[2])
    return args


def guarded_kubeadm(command: str, role: str) -> str:
    """Run bootstrap only on a clean node.

    Failures never imply clean state.
    Local markers fence off initialized and partially initialized nodes.
    Existing nodes must pass a read-only API check before a retry is
    treated as a no-op.
    The lock also closes the check/initialize race between concurrent
    invocations.
    """
    if role not in {"master", "worker"}:
        raise ValueError("Unsupported Kubernetes bootstrap role")
    if role == "master":
        existing_check = (
            "\n"
            "        [ -f /etc/kubernetes/admin.conf ] && [ -r /etc/"
            "kubernetes/admin.conf ] || {\n"
            "            echo 'Partial control-plane state: refusing kubeadm "
            "init; inspect and recover explicitly' >&2\n"
            "            exit 1\n"
            "        }\n"
            "        kubectl --kubeconfig=/etc/kubernetes/admin.conf "
            "--request-timeout=15s get --raw=/readyz >/dev/null || {\n"
            "            echo 'Existing control plane could not be verified; "
            "refusing kubeadm init' >&2\n"
            "            exit 1\n"
            "        }\n"
            "        echo 'Existing control plane verified; skipping kubeadm "
            "init'\n"
        )
    else:
        existing_check = (
            "\n"
            "        [ -f /etc/kubernetes/kubelet.conf ] && [ -r /etc/"
            "kubernetes/kubelet.conf ] || {\n"
            "            echo 'Partial node state: refusing kubeadm join; "
            "inspect and recover explicitly' >&2\n"
            "            exit 1\n"
            "        }\n"
            "        node_name=$(hostname | tr '[:upper:]' '[:lower:]')\n"
            "        kubectl --kubeconfig=/etc/kubernetes/kubelet.conf "
            '--request-timeout=15s get node "$node_name" -o name >/dev/null '
            "|| {\n"
            "            echo 'Existing node could not be verified; refusing "
            "kubeadm join' >&2\n"
            "            exit 1\n"
            "        }\n"
            "        echo 'Existing node verified; skipping kubeadm join'\n"
        )
    return (
        "\n"
        "set -eu\n"
        '[ "$(id -u)" = 0 ] || { echo \'Bootstrap requires root to '
        "verify node state safely' >&2; exit 1; }\n"
        "(\n"
        "    flock -n 9 || { echo 'Another kubeadm operation is in "
        "progress' >&2; exit 1; }\n"
        "    for directory in /etc /var /var/lib /etc/kubernetes /etc/"
        "kubernetes/manifests /etc/kubernetes/pki /var/lib/kubelet /var/lib/"
        "etcd; do\n"
        '        if [ -e "$directory" ] || [ -L "$directory" ]; then\n'
        '            [ -d "$directory" ] && [ -r "$directory" ] && [ -x '
        '"$directory" ] || {\n'
        '                echo "Cannot safely inspect $directory; refusing '
        'bootstrap" >&2\n'
        "                exit 1\n"
        "            }\n"
        "        fi\n"
        "    done\n"
        "    existing=0\n"
        "    for marker in /etc/kubernetes/admin.conf /etc/kubernetes/"
        "kubelet.conf /etc/kubernetes/bootstrap-kubelet.conf /etc/kubernetes/"
        "manifests/kube-apiserver.yaml /etc/kubernetes/pki/ca.crt /var/lib/"
        "kubelet/config.yaml /var/lib/etcd/member; do\n"
        '        if [ -e "$marker" ] || [ -L "$marker" ]; then existing=1; '
        "fi\n"
        "    done\n"
        '    if [ "$existing" = 1 ]; then\n'
        f"{existing_check}\n"
        "    else\n"
        f"        {command}\n"
        "    fi\n"
        ") 9>/run/lock/kubengine-kubeadm.lock\n"
    ).strip()
