"""安装dashboard组件"""
from io import StringIO
import os
from pyinfra.operations import server, python
from pyinfra.context import host
from core.misc.ca import k8s_create_tls

data = host.data
master_ip = data.master_ip
deploy_src = data.deploy_src
domain = data.domain
loadbalancer_ip = data.loadbalancer_ip
images_path = os.path.join(
    deploy_src, "images", "dashboard.images.1.7.0.tar.gz")
helm_charts_dir = os.path.join(deploy_src, "charts", "kubernetes-dashboard")

# 加载离线镜像
if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{images_path} -o - | ctr -n k8s.io i import -"
else:
    command = f"ctr -n k8s.io i import {images_path}"

server.shell(name="Load offline dashboard images", commands=command)


if "master" in host.groups:
    python.call(name="Create TLS cert for dashboard-system namespace", function=k8s_create_tls,
                namespace="dashboard-system", tls_name="dashboard-tls")
    server.shell(name="Install dashboard", commands=" ".join(["KUBECONFIG=/etc/kubernetes/admin.conf helm", "install",
                                                              "dashboard",
                                                             helm_charts_dir,
                                                             "-n", "dashboard-system",
                                                              "--create-namespace",
                                                              "-f", f"{helm_charts_dir}/values.yaml"]))
    manifests_dir = data.manifest_dir
    server.files.directory(
        name="Create manifests directory", path=manifests_dir)
    manifests_file = os.path.join(manifests_dir, "dashboard-admin-user.yaml")
    server.files.put(name="Create dashboard RBAC config file", dest=manifests_file, src=StringIO("""apiVersion: v1
kind: ServiceAccount
metadata:
  name: admin-user
  namespace: dashboard-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: admin-user-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
  - kind: ServiceAccount
    name: admin-user
    namespace: dashboard-system
---
apiVersion: v1
kind: Secret
metadata:
  name: admin-token
  namespace: dashboard-system
  annotations:
    kubernetes.io/service-account.name: admin-user
type: kubernetes.io/service-account-token
"""))
    server.shell(name="Create dashboard RBAC config",
                 commands=f"KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {manifests_file}")

    token_file = os.path.join(data.root_dir, "config", "admin-user.token")
    server.shell(name="Save dashboard token to file",
                 commands=f"KUBECONFIG=/etc/kubernetes/admin.conf kubectl -n dashboard-system get secret admin-token -o jsonpath='{{.data.token}}'|base64 -d > {token_file}")

server.files.line(
    name=f"Add dashboard.{domain} to /etc/hosts",
    path="/etc/hosts",
    line=f"{loadbalancer_ip} dashboard.{domain}",
    present=True,
)
