
"""自定义network command相关处理"""

from core.command import execute_command


def local_ips() -> list[str]:
    """获取本地所有ipv4地址

    Returns:
        list[str]: ip集合
    """
    cmd = "ip -4 addr show |grep inet |awk '{print $2}'|cut -d/ -f1"
    res = execute_command(cmd)
    if res.is_failure():
        return []
    return res.get_output_lines()
