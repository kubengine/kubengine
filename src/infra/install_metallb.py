"""安装metallb"""
from io import StringIO
import os
from pyinfra.operations import server
from pyinfra.context import host

data = host.data
master_ip = data.master_ip
deploy_src = data.deploy_src
loadbalancer_ippools = data.loadbalancer_ippools
images_file = os.path.join(
    deploy_src, "images", "metallb.images.v0.15.2.tar.gz")
helm_charts_dir = os.path.join(
    deploy_src, "/root/offline-deploy/charts/metallb")

# 加载离线镜像
if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{images_file} -o - | ctr -n k8s.io i import -"
else:
    command = f"ctr -n k8s.io i import {images_file}"
server.shell(name="Load offline metallb images", commands=command)

if "master" in host.groups:
    commands = [
        "KUBECONFIG=/etc/kubernetes/admin.conf helm", "install",
        "metallb",
        helm_charts_dir,
        "-n", "metallb-system",
        "--create-namespace",
        "-f", f"{helm_charts_dir}/values.yaml"
    ]
    server.shell(name="Install metallb", commands=" ".join(commands))
    server.shell(name='Sleep 30 seconds on remote host', commands="sleep 30", )
    # 配置ip池资源
    manifests_dir = data.manifest_dir
    server.files.directory(
        name="Create manifests directory", path=manifests_dir)
    ippool_manifests_file = os.path.join(manifests_dir, "metallb-ippool.yaml")
    ippool_manifests_template_file = os.path.join(
        deploy_src, "templates", "metallb-ippool.yaml.j2")
    server.files.template(
        name="Gen metallb-ippool.yaml manifests file",
        src=ippool_manifests_template_file,
        dest=ippool_manifests_file,
        loadbalancer_ippool=loadbalancer_ippools
    )
    server.shell(
        name="Config LoadBalancer ippool",
        commands=f"KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {ippool_manifests_file}"
    )

    # empty-l2-advertisement
    server.files.put(
        name="Create empty-l2-advertisement.yaml",
        dest=f"{manifests_dir}/empty-l2-advertisement.yaml",
        src=StringIO("""apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: empty-l2-advertisement
  namespace: metallb-system
spec:
  ipAddressPools:
  - loadbalancer-pool
  # 不指定任何节点，意味着不在任何节点上为该IP池启用Layer2广播
  nodeSelectors: []""")
    )
    server.shell(
        name="Empty l2 advertisement",
        commands=f"KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {manifests_dir}/empty-l2-advertisement.yaml"
    )
