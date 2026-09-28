"""Content fingerprints for each infrastructure step and its real inputs."""

import hashlib
import json
from pathlib import Path


INPUTS = {
    "install_chrony.py": ["repo"],
    "install_cni.py": ["cni-plugins-linux-amd64-v1.7.1.tgz"],
    "install_containerd.py": ["containerd"],
    "install_keepalived.py": ["repo"],
    "install_kubernetes.py": ["repo", "images/kubenetes.images.v1.34.0.tar.gz"],
    "kubernetes_join_control_plane.py": [],
    "kubernetes_join_node.py": [],
    "install_calico.py": ["images/calico.images.v3.27.0.tar.gz", "templates/calico.yaml.j2"],
    "install_helm.py": ["helm"],
    "install_metallb.py": ["images/metallb.images.v0.15.2.tar.gz", "charts/metallb", "templates/metallb-ippool.yaml.j2"],
    "install_ingress_nginx.py": ["images/ingress-nginx.images.v1.13.3.tar.gz", "charts/ingress-nginx", "templates/ingress-nginx-values.yaml.j2"],
    "issue_cert.py": [],
    "install_longhorn.py": ["repo", "images/longhorn.images.v1.9.1.tar.gz", "charts/longhorn"],
    "install_harbor.py": ["images/harbor.images.v2.14.0.tar.gz", "charts/harbor", "harbor"],
    "install_metrics_server.py": ["images/metrics-server.images.v0.8.0.tar.gz", "charts/metrics-server"],
    "install_dashboard.py": ["images/dashboard.images.1.7.0.tar.gz", "charts/kubernetes-dashboard"],
    "install_kuboard.py": ["images/kuboard.images.v4.tar.gz", "charts/kuboard"],
    "install_cert_manager.py": ["images/cert-manager.images.v1.16.3.tar.gz", "charts/cert-manager"],
}


class DeploymentInputs:
    def __init__(self):
        self.cache = {}

    def _digest(self, path):
        before = path.stat()
        key = (str(path), before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if key not in self.cache:
            digest = hashlib.sha256()
            with path.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    digest.update(chunk)
            after = path.stat()
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != key[1:]:
                raise RuntimeError(f"部署输入在读取期间变化：{path}")
            self.cache[key] = digest.hexdigest()
        return self.cache[key]

    def fingerprint(self, script, deploy_src, config, extra_files=()):
        script, root = Path(script), Path(deploy_src)
        if script.name not in INPUTS:
            raise ValueError(f"组件未声明输入依赖：{script.name}")
        files = [script, *sorted(script.parent.glob("_*.py")), script.parent / "executor_wrapper.py", *map(Path, extra_files)]
        files.extend(root / entry for entry in INPUTS[script.name])
        entries = []
        for path in files:
            if not path.exists():
                entries.append((str(path), "missing"))
                continue
            children = sorted(path.rglob("*")) if path.is_dir() else [path]
            for child in children:
                if child.is_dir():
                    continue
                # A rendered file next to its .j2 source is output, not input.
                if Path(str(child) + ".j2").is_file():
                    continue
                if not child.is_file():
                    raise ValueError(f"部署输入不是普通文件：{child}")
                entries.append((str(child), self._digest(child)))
        value = {"schema": 2, "config": config, "inputs": entries}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
