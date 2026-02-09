"""Asynchronous SSH client wrapper module.

This module provides an asynchronous SSH client wrapper based on asyncssh library,
supporting connection reuse and cluster operations.
"""

import asyncio
from typing import Any, Dict, List, Tuple, Union, cast

import asyncssh


class AsyncSSHClient:
    """Asynchronous SSH client wrapper with connection pooling.

    This class wraps asyncssh library to provide connection pooling and
    cluster management capabilities.
    """

    def __init__(self) -> None:
        """Initialize SSH client with connection pool."""
        # Connection pool: host -> connection mapping
        self._connections: Dict[str, asyncssh.SSHClientConnection] = {}
        self._lock = asyncio.Lock()  # Ensure thread-safe connection pool operations

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
        async with self._lock:
            if host in self._connections:
                # Check if connection is still active
                # Note: asyncssh doesn't have is_active() method,
                # we'll let it fail if connection is broken
                return self._connections[host]
                # If connection is closed, remove and create new connection
                # del self._connections[host]

            # Create new connection and add to pool
            conn = await asyncssh.connect(host, known_hosts=None, **kwargs)
            self._connections[host] = conn
            return conn

    async def close_connection(self, host: str) -> None:
        """Close connection for specified host.

        Args:
            host: Target host address
        """
        async with self._lock:
            if host in self._connections:
                try:
                    self._connections[host].close()
                except Exception:
                    pass
                del self._connections[host]

    async def close_all_connections(self) -> None:
        """Close all connections in the pool."""
        async with self._lock:
            for _, conn in self._connections.items():
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()

    async def execute_command(
        self,
        host: str,
        command: str,
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

        try:
            conn = await self._get_connection(host, **kwargs)
            process = await conn.run(command, check=False)
            result['stdout'] = str(process.stdout) or ''
            result['stderr'] = str(process.stderr) or ''
            result['exit_status'] = process.exit_status
        except Exception as e:
            result['error'] = str(e)

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
        result = await self.execute_multiple_commands(
            [(host, 'echo "ping"') for host in hosts],
            connect_timeout=1,
            **kwargs
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

        try:
            conn = await self._get_connection(host, **kwargs)
            async with conn.start_sftp_client() as sftp:
                await sftp.put(local_path, remote_path)
        except Exception as e:
            result['error'] = str(e)

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

        try:
            conn = await self._get_connection(host, **kwargs)
            async with conn.start_sftp_client() as sftp:
                await sftp.get(remote_path, local_path)
        except Exception as e:
            result['error'] = str(e)

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

        try:
            conn = await self._get_connection(host, **kwargs)
            # type: ignore
            await asyncssh.scp(local_dir, (conn, remote_dir), recurse=True)
        except Exception as e:
            result['error'] = str(e)

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

        try:
            conn = await self._get_connection(host, **kwargs)
            # type: ignore
            await asyncssh.scp((conn, remote_dir), local_dir, recurse=True)
        except Exception as e:
            result['error'] = str(e)

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

    async def setup_ssh_mutual_trust(
        self,
        hosts: List[str],
        **kwargs: Any
    ) -> Dict[str, Union[List[Dict[str, Union[str, int, None]]], None, str]]:
        """Configure mutual SSH trust among cluster nodes.

        Args:
            hosts: List of cluster nodes
            **kwargs: SSH connection parameters

        Returns:
            Dictionary containing configuration result information
        """
        try:
            # 1. Generate SSH key pairs for each node (if not exists)
            generate_key_tasks: List[Any] = []
            for host in hosts:
                cmd = """
if [ ! -f ~/.ssh/id_rsa ]; then \
    ssh-keygen -t rsa -N "" -f ~/.ssh/id_rsa; \
fi && \
cat ~/.ssh/id_rsa.pub
                """
                generate_key_tasks.append(
                    self.execute_command(host, cmd.strip(), **kwargs))

            key_results = await asyncio.gather(*generate_key_tasks)

            # Collect all public keys
            public_keys: Dict[str, str] = {}
            for res in key_results:
                if res['error']:
                    error_msg = f"Failed to generate key on {res['host']}: {res['error']}"
                    return {
                        'error': error_msg,
                        'details': cast(List[Dict[str, Union[str, int, None]]], key_results)
                    }

                stdout = res['stdout']
                if isinstance(stdout, str):
                    public_keys[res['host']] = stdout.strip()
                else:
                    return {
                        'error': f"Invalid stdout type from {res['host']}: {type(stdout)}",
                        'details': cast(List[Dict[str, Union[str, int, None]]], key_results)
                    }

            # 2. Merge all public keys for authorized_keys content
            all_pub_keys = '\n'.join(public_keys.values())

            # 3. Distribute all public keys to each node
            distribute_tasks: List[Any] = []
            for host in hosts:
                # Ensure .ssh directory exists with correct permissions
                cmd = f"""
mkdir -p ~/.ssh && \
chmod 700 ~/.ssh && \
echo "{all_pub_keys}" > ~/.ssh/authorized_keys && \
chmod 600 ~/.ssh/authorized_keys
                """
                distribute_tasks.append(
                    self.execute_command(host, cmd.strip(), **kwargs))

            distribute_results = await asyncio.gather(*distribute_tasks)

            # Check distribution results
            for res in distribute_results:
                if res['error']:
                    error_msg = f"Failed to configure trust on {res['host']}: {res['error']}"
                    return {
                        'error': error_msg,
                        'details': cast(List[Dict[str, Union[str, int, None]]], distribute_results)
                    }

            return {
                'error': None,
                'details': cast(List[Dict[str, Union[str, int, None]]], distribute_results)
            }

        except Exception as e:
            return {
                'error': f"SSH mutual trust configuration error: {str(e)}",
                'details': None
            }


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
