"""创建证书"""
from pyinfra.context import host
from pyinfra.operations import server

from _offline_transfer import pull_to_file

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
if "master" not in host.groups:
    cert_pull, cert_timeout = pull_to_file(
        f"sftp://{master_ip}{ca_crt_file}", ca_crt_file,
        f"/usr/share/pki/ca-trust-source/anchors/{domain}.client.crt")
    server.shell(
        name="Copy client cert file",
        commands=cert_pull,
        _timeout=cert_timeout,
        _retries=4,
        _retry_delay=10,
    )

server.shell(name="Update ca trust", commands="update-ca-trust")
