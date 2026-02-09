"""创建证书"""
from pyinfra.context import host
from pyinfra.operations import server

data = host.data
master_ip = data.master_ip
deploy_src = data.deploy_src
ca_crt_file = data.ca_crt_file
domain = data.domain

# 分发证书
if "master" in host.groups:
    server.files.put(
        name="Copy client cert file",
        dest=f"/usr/share/pki/ca-trust-source/anchors/{domain}.client.crt",
        src=ca_crt_file
    )
if "worker" in host.groups:
    server.shell(
        name="Copy client cert file",
        commands=f"curl -o /usr/share/pki/ca-trust-source/anchors/{domain}.client.crt sftp://{master_ip}{ca_crt_file}"
    )

server.shell(name="Update ca trust", commands="update-ca-trust")
