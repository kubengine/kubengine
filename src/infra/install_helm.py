"""安装helm"""
import os
from pyinfra.operations import server
from pyinfra.context import host

data = host.data
deploy_src = data.deploy_src
helm_file = os.path.join(deploy_src, "helm")

if "master" in host.groups:
    server.files.put(
        name="Copy helm binary to /usr/local/bin/helm",
        dest="/usr/local/bin/helm",
        src=helm_file,
        mode="755",
        user="root",
        group="root",
    )
