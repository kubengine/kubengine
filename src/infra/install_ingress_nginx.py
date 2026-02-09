"""安装ingress-nginx"""
import os
from pyinfra.operations import server
from pyinfra.context import host

data = host.data
master_ip = data.master_ip
deploy_src = data.deploy_src
images_file = os.path.join(
    deploy_src, "images", "ingress-nginx.images.v1.13.3.tar.gz")
helm_charts_dir = os.path.join(
    deploy_src, "/root/offline-deploy/charts/ingress-nginx")
loadbalancer_ip = data.loadbalancer_ip

# 加载离线镜像
if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{images_file} -o - | ctr -n k8s.io i import -"
else:
    command = f"ctr -n k8s.io i import {images_file}"
server.shell(name="Load offline ingress nginx images", commands=command)

if "master" in host.groups:
    manifests_dir = data.manifest_dir
    server.files.directory(name="Create manifests directory",
                           path=manifests_dir)
    values_manifests_file = os.path.join(
        manifests_dir, "ingress-nginx-values.yaml")
    values_template_file = os.path.join(
        deploy_src, "templates", "ingress-nginx-values.yaml.j2")
    server.files.template(name="Gen ingress nginx helm chart values file",
                          src=values_template_file,
                          dest=values_manifests_file,
                          loadbalancer_ip=loadbalancer_ip)

    commands = ["KUBECONFIG=/etc/kubernetes/admin.conf helm", "install",
                "ingress-nginx",
                helm_charts_dir,
                "-n", "ingress-nginx-system",
                "--create-namespace",
                "-f", values_manifests_file]
    server.shell(name="Install ingress nginx", commands=" ".join(commands))
