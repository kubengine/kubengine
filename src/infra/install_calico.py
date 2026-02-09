"""安装calico网络插件"""
import os
from pyinfra.operations import server
from pyinfra.context import host


deploy_src: str = host.data.deploy_src
master_ip: str = host.data.master_ip
manifests_dir: str = host.data.manifest_dir

images_path: str = os.path.join(
    deploy_src, "images", "calico.images.v3.27.0.tar.gz")

if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{images_path} -o - | ctr -n k8s.io i import -"
else:
    command = f"ctr -n k8s.io i import {images_path}"

server.shell(name="Load offline Calico images", commands=command)


# Create manifests directory
server.files.directory(name="Create manifests directory", path=manifests_dir)

# Generate Calico manifests from template
calico_manifests_template_file: str = os.path.join(
    deploy_src, "templates", "calico.yaml.j2")
calico_manifests_file = os.path.join(manifests_dir, "calico.yaml")

server.files.template(
    name="Gen calico manifests file",
    src=calico_manifests_template_file,
    dest=calico_manifests_file,
    mode="755",
    user="root",
    group="root",
    pod_network_cidr=host.data.pod_cidr
)

# Apply Calico manifests
server.shell(
    name="Install calico",
    commands=f"KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {manifests_dir}/calico.yaml"
)
