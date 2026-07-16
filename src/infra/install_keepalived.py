"""部署Keepalived提供控制面VIP（高可用模式）"""
from io import StringIO
from pyinfra.operations import server, files
from pyinfra.context import host

data = host.data

# 仅 master 和 additional_master 组部署 keepalived
is_master = "master" in host.groups or "additional_master" in host.groups
if not is_master:
    exit(0)

vip = data.control_plane_endpoint or ""
if not vip:
    # 没有配置VIP，跳过（单 master 模式）
    exit(0)

master_ip = data.master_ip
deploy_src = data.deploy_src

# 通过 kubengine 离线 repo 安装 keepalived
repo_name = "kubengine_repo"
if "master" not in host.groups:
    baseurl = f"sftp://{master_ip}{deploy_src}/repo"
else:
    baseurl = f"file:///{deploy_src}/repo"
server.yum.repo(
    name="Add kubengine yum repository",
    src=repo_name,
    baseurl=baseurl,
    gpgcheck=False
)
server.yum.packages(
    name="Install keepalived",
    packages=["keepalived"],
    extra_install_args=f"--disablerepo=* --enablerepo={repo_name}"
)
server.yum.repo(
    name="Remove kubengine yum repository",
    src=repo_name,
    present=False
)

# 获取网络接口：优先使用配置值，否则自动检测默认路由网卡
interface = data.master_interface or ""
if not interface:
    import subprocess
    result = subprocess.run(
        ["ip", "route", "get", "1.1.1.1"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0:
        interface = result.stdout.split(" dev ")[1].split()[0]
    else:
        interface = "ens31"

# 确定优先级：第一个 master = MASTER(100)，其余 = BACKUP(95-90-...)
# host.name 为 pyinfra 中当前主机的 IP
master_ips = [data.master_ip] + list(data.additional_master_ips or [])
current_ip = host.name

priority = 90
state = "BACKUP"
for idx, ip in enumerate(master_ips):
    if ip == current_ip:
        if idx == 0:
            priority = 100
            state = "MASTER"
        else:
            priority = max(95 - (idx - 1) * 5, 50)
        break

keepalived_conf = f"""! Configuration File for keepalived
global_defs {{
    router_id LVS_K8S
    script_user root
    enable_script_security
}}

vrrp_instance VI_K8S_APISERVER {{
    state {state}
    interface {interface}
    virtual_router_id 51
    priority {priority}
    advert_int 2
    authentication {{
        auth_type PASS
        auth_pass 42f8a7c3
    }}
    virtual_ipaddress {{
        {vip}/24
    }}
}}
"""

files.put(
    name="Write keepalived configuration",
    dest="/etc/keepalived/keepalived.conf",
    src=StringIO(keepalived_conf)
)

server.systemd.service(
    name="Enable and start keepalived service",
    service="keepalived",
    running=True,
    enabled=True,
    restart=True
)
