"""部署 chrony 时间同步（离线纯内网环境）

K8s 集群对节点时钟一致性敏感（影响证书校验、TLS 握手、etcd、Leader Election 等）。
本脚本在离线环境下：
  - master 节点作为 NTP 服务端（无上游时基于本地硬件时钟，local stratum 10）
  - 其他节点（worker / 附加 master）作为客户端，与 master 同步

应在 K8s 核心组件部署前最先执行。
操作系统默认已安装 chrony，无需额外安装。
"""
from io import StringIO
from pyinfra.operations import server
from pyinfra.context import host

data = host.data
master_ip = data.master_ip

# 从 master_ip 推导节点网段（/24），用于 master 端 allow 指令限制客户端来源
_parts = master_ip.split(".")
node_network = f"{_parts[0]}.{_parts[1]}.{_parts[2]}.0/24"

# ---- 生成 chrony.conf ----
if "master" in host.groups:
    # master 作为 NTP 服务端：纯内网无上游，使用本地硬件时钟对外授时
    master_conf = StringIO(
        "# Managed by kubengine\n"
        "driftfile /var/lib/chrony/drift\n"
        "makestep 1.0 3\n"
        "rtcsync\n"
        f"allow {node_network}\n"
        "local stratum 10\n"
        "logdir /var/log/chrony\n"
    )
    server.files.put(
        name="Write master chrony.conf (NTP server)",
        src=master_conf,
        dest="/etc/chrony.conf"
    )
else:
    # 其他节点作为客户端，指向 master 同步
    client_conf = StringIO(
        "# Managed by kubengine\n"
        f"server {master_ip} iburst\n"
        "driftfile /var/lib/chrony/drift\n"
        "makestep 1.0 3\n"
        "rtcsync\n"
        "logdir /var/log/chrony\n"
    )
    server.files.put(
        name="Write client chrony.conf",
        src=client_conf,
        dest="/etc/chrony.conf"
    )

# ---- 重启并启用 chronyd，加载新配置 ----
server.systemd.service(
    name="Restart and enable chronyd",
    service="chronyd",
    enabled=True,
    restarted=True
)

# ---- 立即强制同步时钟 ----
# master 端：直接 makestep；客户端端：重试等待 master 就绪（最多约 60s）
if "master" in host.groups:
    server.shell(
        name="Force master time sync",
        commands="chronyc makestep"
    )
else:
    server.shell(
        name="Force client time sync (retry until master ready)",
        commands=(
            "for i in $(seq 1 12); do "
            "chronyc makestep && break; "
            "sleep 5; "
            "done"
        )
    )
