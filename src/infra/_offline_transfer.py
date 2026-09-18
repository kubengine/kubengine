"""离线文件传输的公共参数与超时预算。

worker / 附加 master 的离线文件都是通过 ``curl sftp://<master>...`` 从
master 上拉的。这些命令的健壮性参数和超时预算统一放在这里，避免每个
部署脚本各写一份、各调一套。

参数按 curl 8.x 的实测语义定（本仓库环境为 Kylin V10 + curl 8.4 + libssh）：

- ``--speed-limit`` / ``--speed-time`` 看的是**最近 N 秒**的速度，不是整段
  平均速度，所以对"先快后停"的大文件传输同样能判出停顿：低于 1KB/s 持续
  60 秒即中断；
- ``--max-time`` 是整条命令的总时长上限，会把"慢但在走"的传输一起掐掉，
  所以必须按文件大小算，不能写死一个 600；
- 管道传输（``-o - | tar`` / ``-o - | ctr i import -``）不能用 ``--retry``：
  重试会把每一次尝试的数据都写进同一个 stdout，tar / ctr 拿到的是拼接后
  的坏数据。重试交给 pyinfra 的操作级参数 ``_retries``，整条命令重来。

文件大小直接按 master 本地的路径算：kubengine 就跑在 master 上，
deploy_src 在 master 和各节点上是同一个绝对路径。
"""

import os

#: SSH 连接阶段超时（秒）
CONNECT_TIMEOUT = 15
#: 低于这个速度（字节/秒）持续 STALL_SECONDS 秒即判为停顿并中断。
#: 取 10KB/s：内网正常单连接是几 MB/s，"虚机被定住/存储卡住"时是 0，
#: 而低于 10KB/s 持续一分钟不可能是正常传输。调这个值就能改判停灵敏度，
#: 但不能低于真实链路速度，否则慢链路会被误杀。
STALL_LIMIT_BPS = 10240
STALL_SECONDS = 60
#: 估算时长时假定的最慢可接受速度（字节/秒）
FLOOR_BPS = 512 * 1024
#: 再小的文件也给这么多秒传输时间（只是失控保险，真正的判停靠上面的低速门限）
MIN_TRANSFER_SECONDS = 900
#: ctr import / 解压至少给这么多秒
MIN_IMPORT_SECONDS = 600
#: curl 自己超时后，coreutils timeout 再多等这么久再 SIGKILL
KILL_GRACE_SECONDS = 30
#: pyinfra 的 _timeout 在远端兜底之上再留一层余量
OP_MARGIN_SECONDS = 120
#: yum/dnf 走 sftp:// 离线源时的单条命令上限。dnf 自己也有 timeout/minrate，
#: 这里只是不让它无限期挂住（本地源安装 kubelet 这类通常在几分钟内）
YUM_OP_SECONDS = 1800

#: 所有 curl 传输共用（-s 去掉进度条，-S 保留错误信息）
CURL_OPTS = (
    f"--connect-timeout {CONNECT_TIMEOUT} "
    f"--speed-limit {STALL_LIMIT_BPS} --speed-time {STALL_SECONDS} -sS"
)


def _size(local_path):
    try:
        return os.path.getsize(local_path)
    except OSError:
        return 0


def transfer_seconds(local_path):
    """传输阶段的总时长上限：按文件大小、以 FLOOR_BPS 折算。"""
    return max(MIN_TRANSFER_SECONDS, _size(local_path) // FLOOR_BPS + 60)


def import_seconds(local_path):
    """给 ctr i import / tar 解压留的时长。"""
    return max(MIN_IMPORT_SECONDS, _size(local_path) // FLOOR_BPS)


def op_timeout(local_path, extra_seconds=0):
    """pyinfra 操作级超时：本地分支（master 自己解压/导入）也用它。"""
    return transfer_seconds(local_path) + extra_seconds + OP_MARGIN_SECONDS


def _guarded(url_command, local_path, extra_seconds):
    budget = transfer_seconds(local_path)
    command = (
        f"timeout --kill-after=5 {budget + KILL_GRACE_SECONDS} "
        f"curl --max-time {budget} {CURL_OPTS} {url_command}"
    )
    return command, budget + extra_seconds + OP_MARGIN_SECONDS + KILL_GRACE_SECONDS


def pull(url, local_path, consumer, extra_seconds=0):
    """从 master 拉文件并直接交给消费端，返回 (命令, pyinfra 超时)。

    ``consumer`` 形如 ``tar zxf - -C /opt/cni/bin`` 或
    ``ctr -n k8s.io i import -``。
    """
    command, timeout = _guarded(f"{url} -o -", local_path, extra_seconds)
    return f"{command} | {consumer}", timeout


def pull_to_file(url, local_path, dest, extra_seconds=0):
    """从 master 拉单个文件到指定路径，返回 (命令, pyinfra 超时)。"""
    return _guarded(f"{url} -o {dest}", local_path, extra_seconds)
