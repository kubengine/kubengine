"""安装 cni 插件"""
from io import StringIO
import os
from pyinfra.operations import server
from pyinfra.context import host

data = host.data

deploy_src = data.deploy_src
master_ip = data.master_ip
cni_path = os.path.join(deploy_src, "cni-plugins-linux-amd64-v1.7.1.tgz")

target_cni_dir = "/opt/cni/bin"

server.files.directory(
    name=f"Create {target_cni_dir} directory for CNI plugins", path=target_cni_dir)

if "worker" in host.groups:
    command = f"curl sftp://{master_ip}{cni_path} -o - | tar zxf - -C {target_cni_dir}"
else:
    command = f"tar zxf {cni_path} -C {target_cni_dir}"

server.shell(name=f"Extract CNI plugins to {target_cni_dir}", commands=command)

server.files.put(
    name="Create CNI configuration file 10-mynet.conf",  # type: ignore
    src=StringIO("""{
  "name": "mynet",
  "type": "bridge",
  "bridge": "cni0",
  "isGateway": true,
  "ipMasq": true,
  "ipam": { "type": "host-local", "subnet": "10.20.0.0/32", "gateway": "10.20.0.1" },
}
"""),
    dest="/etc/cni/net.d/10-mynet.conf")
