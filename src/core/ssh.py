"""Asynchronous SSH client wrapper module.

This module provides an asynchronous SSH client wrapper based on asyncssh library,
supporting connection reuse and cluster operations.
"""

import asyncio
import base64
import logging
from typing import Any, Dict, List, Optional, Tuple, Union, cast

import asyncssh

logger = logging.getLogger(__name__)


def _result_error(result: Dict[str, Union[str, int, None]]) -> Optional[str]:
    """把"执行异常"和"退出码非 0"统一成失败原因；成功返回 None。

    ``execute_command`` 只在抛异常时写 ``error``，命令跑失败（退出码非 0）时
    ``error`` 是空的。互信流程里的命令是 shell 脚本，语法错误、写文件失败
    都属于后者，只看 error 会把失败当成功，所以这里统一判一次。
    """
    if result.get('error'):
        return str(result['error'])
    status = result.get('exit_status')
    if status in (0, None):
        return None
    stderr_lines = [
        line.strip() for line in str(result.get('stderr') or '').splitlines() if line.strip()
    ]
    return stderr_lines[-1][:200] if stderr_lines else f"exit_status={status}"


class AsyncSSHClient:
    """Asynchronous SSH client wrapper with connection pooling.

    This class wraps asyncssh library to provide connection pooling and
    cluster management capabilities.
    """

    def __init__(
        self,
        connect_timeout: float = 10,
        operation_timeout: float = 300,
        transfer_timeout: float = 600,
        max_concurrency: int = 10,
    ) -> None:
        """Initialize SSH client with connection pool."""
        # Connection pool: host -> connection mapping
        self._connections: Dict[str, asyncssh.SSHClientConnection] = {}
        self._connection_locks: Dict[str, asyncio.Lock] = {}
        self._pool_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self.connect_timeout = connect_timeout
        self.operation_timeout = operation_timeout
        self.transfer_timeout = transfer_timeout

    async def _host_lock(self, host: str) -> asyncio.Lock:
        async with self._pool_lock:
            return self._connection_locks.setdefault(host, asyncio.Lock())

    async def _get_connection(
        self,
        host: str,
        **kwargs: Any
    ) -> asyncssh.SSHClientConnection:
        """Get connection from pool, create new one if not exists.

        Args:
            host: Target host address
            **kwargs: Connection parameters for asyncssh.connect

        Returns:
            SSH client connection object
        """
        host_lock = await self._host_lock(host)
        async with host_lock:
            conn = self._connections.get(host)
            if conn is not None and not conn.is_closed():
                return conn
            if conn is not None:
                self._connections.pop(host, None)

            connect_timeout = float(kwargs.pop("connect_timeout", self.connect_timeout))
            # Existing installations bootstrap trust from application credentials.
            # Callers can supply a known_hosts path to enable strict host validation.
            kwargs.setdefault("known_hosts", None)
            conn = await asyncio.wait_for(
                asyncssh.connect(host, connect_timeout=connect_timeout, **kwargs),
                timeout=connect_timeout + 1,
            )
            self._connections[host] = conn
            return conn

    async def _discard_connection(self, host: str) -> None:
        conn = self._connections.pop(host, None)
        if conn is None:
            return
        conn.close()
        try:
            await asyncio.wait_for(conn.wait_closed(), timeout=2)
        except (Exception, asyncio.CancelledError):
            pass

    async def close_connection(self, host: str) -> None:
        """Close connection for specified host.

        Args:
            host: Target host address
        """
        host_lock = await self._host_lock(host)
        async with host_lock:
            await self._discard_connection(host)

    async def close_all_connections(self) -> None:
        """Close all connections in the pool."""
        async with self._pool_lock:
            hosts = list(self._connections)
        await asyncio.gather(
            *(self.close_connection(host) for host in hosts),
            return_exceptions=True,
        )

    async def execute_command(
        self,
        host: str,
        command: str,
        operation_timeout: Optional[float] = None,
        **kwargs: Any
    ) -> Dict[str, Union[str, int, None]]:
        """Execute command on single host (using connection pool).

        Args:
            host: Target host address
            command: Command to execute
            **kwargs: Parameters passed to asyncssh.connect
                     (username, password, client_keys, etc.)

        Returns:
            Dictionary containing execution result including stdout, stderr,
            exit_status, and error information
        """
        result: Dict[str, Union[str, int, None]] = {
            'host': host,
            'command': command,
            'stdout': '',
            'stderr': '',
            'exit_status': None,
            'error': None
        }

        timeout = operation_timeout or self.operation_timeout
        try:
            async with self._semaphore:
                conn = await self._get_connection(host, **kwargs)
                process = await asyncio.wait_for(
                    conn.run(command, check=False), timeout=timeout
                )
                result['stdout'] = str(process.stdout) or ''
                result['stderr'] = str(process.stderr) or ''
                result['exit_status'] = process.exit_status
        except asyncio.TimeoutError:
            result['error'] = f"SSH command timed out after {timeout}s"
            await self._discard_connection(host)
        except Exception as e:
            result['error'] = str(e)
            await self._discard_connection(host)

        return result

    async def execute_multiple_commands(
        self,
        hosts_commands: List[Tuple[str, str]],
        **kwargs: Any
    ) -> List[Dict[str, Union[str, int, None]]]:
        """Execute commands asynchronously on multiple hosts.

        Args:
            hosts_commands: List of (host, command) tuples
            **kwargs: Common parameters passed to asyncssh.connect

        Returns:
            List of execution results, each containing command execution result
        """
        tasks = [
            self.execute_command(host, cmd, **kwargs)
            for host, cmd in hosts_commands
        ]

        return await asyncio.gather(*tasks)

    async def is_reachable(
        self,
        hosts: List[str],
        **kwargs: Any
    ) -> Tuple[List[str], List[str]]:
        """Check if hosts are reachable.

        Args:
            hosts: List of host addresses to check
            **kwargs: SSH connection parameters

        Returns:
            Tuple of (reachable_hosts, not_reachable_hosts)
        """
        reachability_options = dict(kwargs)
        reachability_options.setdefault("connect_timeout", min(self.connect_timeout, 3))
        reachability_options.setdefault("operation_timeout", 5)
        result = await self.execute_multiple_commands(
            [(host, 'echo "ping"') for host in hosts],
            **reachability_options,
        )

        reachable_hosts: List[str] = []
        not_reachable_hosts: List[str] = []

        for item in result:
            host = item['host']
            if isinstance(host, str) and item['exit_status'] == 0:
                reachable_hosts.append(host)
            elif isinstance(host, str):
                not_reachable_hosts.append(host)

        return reachable_hosts, not_reachable_hosts

    async def upload_file(
        self,
        host: str,
        local_path: str,
        remote_path: str,
        **kwargs: Any
    ) -> Dict[str, Union[str, None]]:
        """Upload file to remote host (using connection pool).

        Args:
            host: Target host address
            local_path: Local file path
            remote_path: Remote file path
            **kwargs: SSH connection parameters

        Returns:
            Dictionary containing upload result
        """
        result: Dict[str, Union[str, None]] = {
            'host': host,
            'local_path': local_path,
            'remote_path': remote_path,
            'error': None
        }

        timeout = float(kwargs.pop("operation_timeout", self.transfer_timeout))
        try:
            async with self._semaphore:
                conn = await self._get_connection(host, **kwargs)
                async with conn.start_sftp_client() as sftp:
                    await asyncio.wait_for(
                        sftp.put(local_path, remote_path), timeout=timeout
                    )
        except asyncio.TimeoutError:
            result['error'] = f"SFTP upload timed out after {timeout}s"
            await self._discard_connection(host)
        except Exception as e:
            result['error'] = str(e)
            await self._discard_connection(host)

        return result

    async def download_file(
        self,
        host: str,
        remote_path: str,
        local_path: str,
        **kwargs: Any
    ) -> Dict[str, Union[str, None]]:
        """Download file from remote host (using connection pool).

        Args:
            host: Target host address
            remote_path: Remote file path
            local_path: Local file path
            **kwargs: SSH connection parameters

        Returns:
            Dictionary containing download result
        """
        result: Dict[str, Union[str, None]] = {
            'host': host,
            'remote_path': remote_path,
            'local_path': local_path,
            'error': None
        }

        timeout = float(kwargs.pop("operation_timeout", self.transfer_timeout))
        try:
            async with self._semaphore:
                conn = await self._get_connection(host, **kwargs)
                async with conn.start_sftp_client() as sftp:
                    await asyncio.wait_for(
                        sftp.get(remote_path, local_path), timeout=timeout
                    )
        except asyncio.TimeoutError:
            result['error'] = f"SFTP download timed out after {timeout}s"
            await self._discard_connection(host)
        except Exception as e:
            result['error'] = str(e)
            await self._discard_connection(host)

        return result

    async def upload_directory(
        self,
        host: str,
        local_dir: str,
        remote_dir: str,
        **kwargs: Any
    ) -> Dict[str, Union[str, None]]:
        """Upload directory to remote host (recursive, using connection pool).

        Args:
            host: Target host address
            local_dir: Local directory path
            remote_dir: Remote directory path
            **kwargs: SSH connection parameters

        Returns:
            Dictionary containing upload result
        """
        result: Dict[str, Union[str, None]] = {
            'host': host,
            'local_dir': local_dir,
            'remote_dir': remote_dir,
            'error': None
        }

        timeout = float(kwargs.pop("operation_timeout", self.transfer_timeout))
        try:
            async with self._semaphore:
                conn = await self._get_connection(host, **kwargs)
                await asyncio.wait_for(
                    asyncssh.scp(local_dir, (conn, remote_dir), recurse=True),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            result['error'] = f"SCP upload timed out after {timeout}s"
            await self._discard_connection(host)
        except Exception as e:
            result['error'] = str(e)
            await self._discard_connection(host)

        return result

    async def download_directory(
        self,
        host: str,
        remote_dir: str,
        local_dir: str,
        **kwargs: Any
    ) -> Dict[str, Union[str, None]]:
        """Download directory from remote host (recursive, using connection pool).

        Args:
            host: Target host address
            remote_dir: Remote directory path
            local_dir: Local directory path
            **kwargs: SSH connection parameters

        Returns:
            Dictionary containing download result
        """
        result: Dict[str, Union[str, None]] = {
            'host': host,
            'remote_dir': remote_dir,
            'local_dir': local_dir,
            'error': None
        }

        timeout = float(kwargs.pop("operation_timeout", self.transfer_timeout))
        try:
            async with self._semaphore:
                conn = await self._get_connection(host, **kwargs)
                await asyncio.wait_for(
                    asyncssh.scp((conn, remote_dir), local_dir, recurse=True),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            result['error'] = f"SCP download timed out after {timeout}s"
            await self._discard_connection(host)
        except Exception as e:
            result['error'] = str(e)
            await self._discard_connection(host)

        return result

    async def set_hostnames(
        self,
        host_hostname_map: Dict[str, str],
        **kwargs: Any
    ) -> List[Dict[str, Union[str, int, None]]]:
        """Set hostname for cluster nodes.

        Args:
            host_hostname_map: Dictionary mapping host IP to hostname
            **kwargs: SSH connection parameters

        Returns:
            List of setup results for each node
        """
        tasks: List[Any] = []
        for host, hostname in host_hostname_map.items():
            # Build command to set hostname
            cmd = f"""
hostnamectl set-hostname {hostname} && \
if ! grep -q "{host}" /etc/hosts; then \
    echo "{host} {hostname}" >> /etc/hosts; \
else \
    sed -i "s/^{host}.*/{host} {hostname}/" /etc/hosts; \
fi
            """
            tasks.append(self.execute_command(host, cmd.strip(), **kwargs))

        results = await asyncio.gather(*tasks)

        # Type cast to satisfy strict checking
        return [cast(Dict[str, Union[str, int, None]], result) for result in results]

    @staticmethod
    def _scan_host_keys_script(hosts: List[str]) -> str:
        """生成"由**一台**节点统一采集全集群 host key"的脚本。

        为什么不让每台节点各自扫：N 台 × (N-1) 个对端同时 ssh-keyscan 时，
        每个节点会同时收到来自其它所有节点的连接，默认 MaxStartups 下必然
        随机丢连接，结果就是"个别对端总是缺 host key"（实测 812 对里缺 26 对）。
        集中扫一次，每个节点只被扫一次，结果确定。

        脚本输出：
          ``KUBENGINE_SCAN:<base64>``         采集到的 known_hosts 内容
          ``KUBENGINE_SCAN_MISSING:<列表>``   重试后仍没拿到的节点
        """
        if not hosts:
            return "true"

        host_list = " ".join(hosts)
        return f"""
tmp=$(mktemp) || exit 1
: > "$tmp"
scan() {{
    ssh-keyscan -T 10 -p 22 "$@" >> "$tmp" 2>/dev/null
}}
scan {host_list}
# 同一台 sshd 会提供多种 host key（ed25519 / rsa / ecdsa），
# 少收一种就会出现 "No ED25519 host key is known for ... and you have
# requested strict checking."，所以一直补扫到"不再出现新行"为止。
for attempt in 1 2 3 4; do
    before=$(wc -l < "$tmp")
    sleep $(awk 'BEGIN{{srand(); printf "%d", 1 + rand()*3}}')
    scan {host_list}
    after=$(wc -l < "$tmp")
    [ "$after" = "$before" ] && break
done
sort -u "$tmp" > "$tmp.dedup" && mv "$tmp.dedup" "$tmp"
missing=""
for h in {host_list}; do
    [ "$(grep -c "^$h " "$tmp")" -gt 0 ] || missing="$missing $h"
done
echo "KUBENGINE_SCAN_MISSING:$missing"
printf 'KUBENGINE_SCAN:'; base64 -w0 < "$tmp"; echo
rm -f "$tmp"
""".strip()

    @staticmethod
    def _strict_check_script(peers: List[str]) -> str:
        """生成"不带任何 host key 跳过参数"的免密连通自检脚本。

        一次失败可能是被 sshd 限流挡掉，隔 2 秒复验，仍失败才带原因上报。
        原因取 ssh 输出的最后一个非空行。
        """
        if not peers:
            return "true"

        peer_list = " ".join(peers)
        return f"""
STRICT_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5"
fail=""
for h in {peer_list}; do
    if ! ssh $STRICT_OPTS "$h" true >/dev/null 2>&1; then
        fail="$fail $h"
    fi
done
if [ -n "$fail" ]; then
    sleep 2
    for h in $fail; do
        out=$(ssh $STRICT_OPTS "$h" true 2>&1)
        if [ $? -ne 0 ]; then
            reason=$(printf '%s\\n' "$out" | grep -v '^[[:space:]]*$' | tail -1 | cut -c1-120)
            echo "KUBENGINE_SSH_FAIL:$h ${{reason:-exit_nonzero}}"
        fi
    done
fi
""".strip()

    @staticmethod
    def _parse_ssh_fail_lines(stdout: str) -> List[str]:
        """把 ``KUBENGINE_SSH_FAIL:<对端> <原因>`` 行解析成 ``对端 (原因)`` 列表。"""
        failures: List[str] = []
        for line in stdout.splitlines():
            if "KUBENGINE_SSH_FAIL:" not in line:
                continue
            rest = line.split("KUBENGINE_SSH_FAIL:", 1)[1].strip()
            peer, _, reason = rest.partition(" ")
            if peer:
                failures.append(f"{peer} ({reason or '原因未知'})")
        return failures

    @staticmethod
    def _known_hosts_refresh_script(
        self_host: str, hosts: List[str], known_hosts_b64: str
    ) -> str:
        """生成"清旧记录 + 写入全集群当前 host key + 严格自检"的脚本。

        在每台节点上执行，内容来自集中采集（所有节点写同一份）：
          1. 清掉 known_hosts（含 /etc/ssh/ssh_known_hosts）里集群各节点的旧记录，
             节点重装 / 虚机回滚后 host key 会变，旧记录不清掉就会
             "REMOTE HOST IDENTIFICATION HAS CHANGED"；
          2. 把集中采集到的内容（base64，避免任何引号问题）追加进 known_hosts；
          3. 用不带任何 host key 跳过参数的 ssh 自检，把仍然过不去的对端
             连原因一起用标记行输出。

        Args:
            self_host: 当前节点自己的地址（自检时跳过）
            hosts: 集群全部节点地址（清理范围）
            known_hosts_b64: 集中采集到的 known_hosts 内容（base64）

        Returns:
            可直接在远端 sh 里执行的脚本
        """
        peers = [h for h in hosts if h != self_host]
        if not peers:
            return "true"

        clean_lines = "\n".join(
            f"ssh-keygen -R {host} >/dev/null 2>&1 || true\n"
            f"if [ -f /etc/ssh/ssh_known_hosts ]; then "
            f"ssh-keygen -R {host} -f /etc/ssh/ssh_known_hosts >/dev/null 2>&1 || true; fi"
            for host in hosts
        )

        return f"""
mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/known_hosts && chmod 600 ~/.ssh/known_hosts
{clean_lines}
printf '%s' '{known_hosts_b64}' | base64 -d >> ~/.ssh/known_hosts
{AsyncSSHClient._strict_check_script(peers)}
echo "KUBENGINE_KNOWN_HOSTS_DONE"
""".strip()

    async def setup_ssh_mutual_trust(
        self,
        hosts: List[str],
        **kwargs: Any
    ) -> Dict[str, Any]:
        """Configure mutual SSH trust among cluster nodes.

        Args:
            hosts: List of cluster nodes
            **kwargs: SSH connection parameters

        Returns:
            Dictionary containing configuration result information；
            ``known_hosts_failures`` 为 {节点: [仍然无法免确认的节点列表]}，
            只包含真正刷不下去的对端。
        """
        try:
            # 1. 为每个节点准备密钥对（不存在才生成），并把公钥收上来。
            #
            #    注意 -q：不加它时 ssh-keygen 会把 17 行提示（含
            #    "The key's randomart image is:" 这个**带单引号**的行）打到
            #    stdout，混进公钥里；下面的分发命令用单引号包裹内容，
            #    一旦密钥内容里出现单引号，整条命令会被拆坏（实测 sh 报
            #    syntax error），authorized_keys 根本写不进去，而
            #    execute_command 只在抛异常时置 error，于是互信"看起来成功"
            #    但节点之间根本连不上。这里同时做两层防护：
            #      a) ssh-keygen -q 不产生噪音；
            #      b) 取 stdout 最后一个非空行作为公钥。
            generate_key_tasks: List[Any] = []
            for host in hosts:
                cmd = """
mkdir -p ~/.ssh
chmod 700 ~/.ssh
if [ ! -f ~/.ssh/id_rsa ]; then ssh-keygen -q -t rsa -N "" -f ~/.ssh/id_rsa || exit 1; fi
cat ~/.ssh/id_rsa.pub || exit 1
                """
                generate_key_tasks.append(
                    self.execute_command(host, cmd.strip(), **kwargs))

            key_results = await asyncio.gather(*generate_key_tasks)

            # Collect all public keys
            public_keys: Dict[str, str] = {}
            for res in key_results:
                reason = _result_error(res)
                if reason:
                    return {
                        'error': f"节点 {res['host']} 公钥准备失败: {reason}",
                        'details': cast(List[Dict[str, Union[str, int, None]]], key_results)
                    }

                stdout = res['stdout']
                if not isinstance(stdout, str):
                    return {
                        'error': f"Invalid stdout type from {res['host']}: {type(stdout)}",
                        'details': cast(List[Dict[str, Union[str, int, None]]], key_results)
                    }

                key_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
                pub_key = key_lines[-1] if key_lines else ''
                if not pub_key.startswith(('ssh-', 'ecdsa-', 'sk-')):
                    return {
                        'error': f"节点 {res['host']} 未能取到公钥（stdout={stdout.strip()[:120]!r}）",
                        'details': cast(List[Dict[str, Union[str, int, None]]], key_results)
                    }
                public_keys[res['host']] = pub_key

            # 2. Merge all public keys for authorized_keys content
            all_pub_keys = '\n'.join(public_keys.values()) + '\n'
            expected_keys = len(public_keys)

            # 3. Distribute all public keys to each node
            #
            #    内容走 base64，避免任何 shell 引号/转义问题：base64 字符集里
            #    不可能出现单引号，也就不会把命令拆坏。
            payload = base64.b64encode(all_pub_keys.encode()).decode()
            distribute_tasks: List[Any] = []
            for host in hosts:
                # 先写临时文件再 mv，保证并发/中断下不会留下半个 authorized_keys。
                cmd = (
                    "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                    f"printf '%s' '{payload}' | base64 -d > ~/.ssh/authorized_keys.tmp && "
                    "chmod 600 ~/.ssh/authorized_keys.tmp && "
                    "mv -f ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys && "
                    "grep -cE '^(ssh-|ecdsa-|sk-)' ~/.ssh/authorized_keys"
                )
                distribute_tasks.append(
                    self.execute_command(host, cmd, **kwargs))

            distribute_results = await asyncio.gather(*distribute_tasks)

            # Check distribution results：既看异常，也看退出码，还要核对写入条数
            for res in distribute_results:
                reason = _result_error(res)
                if reason:
                    return {
                        'error': f"节点 {res['host']} 写入 authorized_keys 失败: {reason}",
                        'details': cast(List[Dict[str, Union[str, int, None]]], distribute_results)
                    }
                try:
                    written = int(str(res['stdout'] or '0').strip().splitlines()[-1])
                except (ValueError, IndexError):
                    written = -1
                if written < expected_keys:
                    return {
                        'error': (
                            f"节点 {res['host']} 的 authorized_keys 只写入 {written} 条，"
                            f"期望 {expected_keys} 条"
                        ),
                        'details': cast(List[Dict[str, Union[str, int, None]]], distribute_results)
                    }

            # 4. 刷新 known_hosts。
            #
            #    节点重装 / 虚机回滚之后 host key 会变，而 ~/.ssh/known_hosts
            #    往往被保留下来，于是对端会出现
            #    "REMOTE HOST IDENTIFICATION HAS CHANGED" 或停在 yes 确认上；
            #    非交互执行时不带 -o StrictHostKeyChecking=no 就直接失败。
            #
            #    因此这里必须"先清旧记录、再把当前 host key 真正写进去"，
            #    不能用 -o UserKnownHostsFile=/dev/null（那样只校验不落盘）。
            #    采集只做一次（单台扫描），然后同一份内容下发给所有节点，
            #    避免 N×N 扫描把各节点 sshd 打爆导致随机缺 key。
            scanner = hosts[0]
            scan_result = await self.execute_command(
                scanner, self._scan_host_keys_script(hosts), **kwargs
            )

            scan_reason = _result_error(scan_result)
            scan_blob = ''
            scan_missing: List[str] = []
            for line in str(scan_result['stdout'] or '').splitlines():
                if line.startswith("KUBENGINE_SCAN:"):
                    scan_blob = line.split("KUBENGINE_SCAN:", 1)[1].strip()
                elif line.startswith("KUBENGINE_SCAN_MISSING:"):
                    scan_missing = line.split("KUBENGINE_SCAN_MISSING:", 1)[1].split()

            if scan_reason or not scan_blob:
                return {
                    'error': (
                        f"在 {scanner} 上采集集群 host key 失败: "
                        f"{scan_reason or 'ssh-keyscan 没有输出'}"
                    ),
                    'details': cast(List[Dict[str, Union[str, int, None]]], distribute_results)
                }

            refresh_tasks: List[Any] = []
            for src_host in hosts:
                refresh_tasks.append(
                    self.execute_command(
                        src_host,
                        self._known_hosts_refresh_script(src_host, hosts, scan_blob),
                        **kwargs,
                    )
                )

            refresh_results = await asyncio.gather(*refresh_tasks)

            known_hosts_failures: Dict[str, List[str]] = {}
            ssh_failures: Dict[str, List[str]] = {}
            for res in refresh_results:
                host = str(res['host'])
                reason = _result_error(res)
                if reason:
                    known_hosts_failures[host] = [f"<刷新命令失败: {reason}>"]
                    continue
                failures = self._parse_ssh_fail_lines(str(res['stdout'] or ''))
                if failures:
                    ssh_failures[host] = failures

            for host, peers in known_hosts_failures.items():
                logger.warning(
                    "known_hosts refresh failed on %s: %s", host, ", ".join(peers)
                )
            if scan_missing:
                logger.warning(
                    "host key not collected for: %s (这些节点上普通 ssh 会要求确认 yes)",
                    ", ".join(scan_missing),
                )
            for host, peers in ssh_failures.items():
                logger.warning(
                    "strict ssh still failing on %s for peers: %s",
                    host, ", ".join(peers),
                )

            return {
                'error': None,
                'known_hosts_failures': known_hosts_failures,
                'scan_missing': scan_missing,
                'ssh_failures': ssh_failures,
                'details': cast(List[Dict[str, Union[str, int, None]]], distribute_results)
            }

        except Exception as e:
            return {
                'error': f"SSH mutual trust configuration error: {str(e)}",
                'details': None
            }

    async def verify_ssh_trust(
        self,
        hosts: List[str],
        **kwargs: Any
    ) -> Dict[str, List[str]]:
        """校验节点间免密 ssh 是否真的可用（严格模式，接受一次复验）。

        刻意不加 ``-o StrictHostKeyChecking=no``：加上它等于替业务侧的 ssh
        "代答 yes"，会掩盖 known_hosts 没刷新的问题。同时**逐台串行**执行，
        不再把 N×(N-1) 个 ssh 一次性并发出去——那样各节点会被自己的连接风暴
        打爆，出现大量 ``open failed`` / ``Connection closed`` 的假失败。

        Args:
            hosts: 集群节点地址列表
            **kwargs: SSH 连接参数

        Returns:
            ``{节点: ["对端 (原因)", ...]}``，只包含复验后仍失败的组合。
        """
        if len(hosts) < 2:
            return {}

        tasks = []
        for src_host in hosts:
            peers = [h for h in hosts if h != src_host]
            tasks.append(
                self.execute_command(src_host, self._strict_check_script(peers), **kwargs)
            )

        results = await asyncio.gather(*tasks)

        failures: Dict[str, List[str]] = {}
        for res in results:
            host = str(res['host'])
            reason = _result_error(res)
            if reason:
                failures[host] = [f"<校验命令失败: {reason}>"]
                continue
            parsed = self._parse_ssh_fail_lines(str(res['stdout'] or ''))
            if parsed:
                failures[host] = parsed
        return failures


async def _main() -> None:
    """Main function for testing SSH client functionality."""
    # Initialize SSH client
    ssh_client = AsyncSSHClient()

    # 1. Execute command on single host
    print("=== Single Command Execution ===")
    cmd_result = await ssh_client.execute_command(
        'localhost',
        'echo "Hello, AsyncSSH!"',
        username='root',
        # password='your_password',  # For password authentication
        client_keys=['~/.ssh/id_rsa']  # For key authentication
    )

    print(f"Host: {cmd_result['host']}")
    print(f"Command: {cmd_result['command']}")
    if cmd_result['error']:
        print(f"Error: {cmd_result['error']}")
    else:
        print(f"Output: {cmd_result['stdout']}")

    # 2. Execute commands on multiple hosts asynchronously
    print("\n=== Multiple Commands Execution ===")
    hosts_commands: List[Tuple[str, str]] = [
        ('172.31.65.150', 'uname -a'),
        ('localhost', 'ls /'),
        # ('host3.example.com', 'uptime')
    ]

    multi_results = await ssh_client.execute_multiple_commands(
        hosts_commands,
        username='root',
        client_keys=['~/.ssh/id_rsa']
    )

    for res in multi_results:
        print(f"\nHost: {res['host']}")
        print(f"Command: {res['command']}")
        if res['error']:
            print(f"Error: {res['error']}")
        else:
            print(f"Output: {res['stdout']}")

    # 3. Upload file
    print("\n=== File Upload ===")
    upload_result = await ssh_client.upload_file(
        '172.31.65.150',
        '/tmp/111',
        '/tmp/222',
        username='root'
    )

    if upload_result['error']:
        print(f"Upload failed: {upload_result['error']}")
    else:
        print(
            f"File {upload_result['local_path']} uploaded to "
            f"{upload_result['host']}:{upload_result['remote_path']}"
        )

    # 4. Download file
    print("\n=== File Download ===")
    download_result = await ssh_client.download_file(
        '172.31.65.150',
        '/tmp/222',
        '/tmp/333',
        username='root'
    )

    if download_result['error']:
        print(f"Download failed: {download_result['error']}")
    else:
        print(
            f"File {download_result['host']}:{download_result['remote_path']} "
            f"downloaded to {download_result['local_path']}"
        )

    # Close all connections
    await ssh_client.close_all_connections()


if __name__ == "__main__":
    asyncio.run(_main())
