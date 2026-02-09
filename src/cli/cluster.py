"""Cluster management CLI commands.

This module provides command-line interface for cluster configuration,
including hostname setup, SSH trust configuration, and batch command execution.
"""

import asyncio
import os
from typing import Any, Dict, List, Optional, Tuple, Union

import click

from cli.models import ListType
from core.config import Application
from core.config.config_dict import ConfigDict
from core.ssh import AsyncSSHClient


@click.group(help="Cluster management CLI commands")
def cli() -> None:
    """Main CLI command group.

    Core functionalities:
        1. Batch configure cluster hostnames with persistence
        2. Configure SSH mutual trust between nodes
        3. Node reachability detection and verification
    """
    pass


def _load_cluster_config() -> Optional[Dict[str, Any]]:
    """Load cluster configuration from application config.

    Returns:
        Dictionary with cluster config or None if not found
    """
    try:
        config = ConfigDict.get_instance()

        if (hasattr(config, 'cluster') and config.cluster is not None):
            cluster_config = config.cluster

            # Extract nodes and hostnames if available
            nodes = getattr(cluster_config, 'nodes', [])
            hostnames = getattr(cluster_config, 'hostnames', {})

            if nodes and hostnames:
                return {
                    'hosts': nodes,
                    'hostnames': hostnames
                }

        return None

    except Exception as e:
        click.echo(
            click.style(
                f"Failed to load cluster config: {str(e)}", fg="yellow")
        )
        return None


def _get_cluster_hosts(
    hosts: Optional[List[str]] = None
) -> List[str]:
    """Get cluster hosts from input or config file.

    Args:
        hosts: User-provided hosts list

    Returns:
        List of cluster hosts
    """
    if hosts:
        return hosts

    # Try to load from config
    cluster_config = _load_cluster_config()
    if cluster_config and cluster_config.get('hosts'):
        click.echo(
            click.style("Loaded hosts from configuration file", fg="green")
        )
        return cluster_config['hosts']

    click.echo(
        click.style(
            "No hosts found in configuration. "
            "Please provide --hosts option or configure cluster first.",
            fg="yellow"
        )
    )
    return []


def _get_cluster_hostname_map(
    hostname_map: Optional[str] = None,
    hosts: Optional[List[str]] = None
) -> Dict[str, str]:
    """Get cluster hostname mapping from input or config file.

    Args:
        hostname_map: User-provided hostname mapping string
        hosts: List of hosts for validation

    Returns:
        Dictionary mapping host to hostname
    """
    if hostname_map:
        return _parse_hostname_mapping(hostname_map)

    # Try to load from config
    cluster_config = _load_cluster_config()
    if cluster_config and cluster_config.get('hostnames'):
        click.echo(
            click.style(
                "Loaded hostname mapping from configuration file", fg="green")
        )
        return cluster_config['hostnames']

    click.echo(
        click.style(
            "No hostname mapping found in configuration. "
            "Please provide --hostname-map option.",
            fg="yellow"
        )
    )
    return {}


async def configure_cluster_workflow(
    hosts: List[str],
    verify_ssh: bool,
    host_hostname_map: Dict[str, str],
    **ssh_kwargs: Any
) -> None:
    """Execute cluster configuration workflow.

    Args:
        hosts: Target cluster node IP list
        verify_ssh: Whether to verify SSH trust results
        host_hostname_map: IP to hostname mapping dictionary
        **ssh_kwargs: SSH connection parameters
    """
    ssh_client = AsyncSSHClient()

    try:
        # Step 1: Check node reachability
        click.echo(click.style(
            "\n=== Checking Node Reachability ===", fg="blue"))
        reachable_hosts, unreachable_hosts = await ssh_client.is_reachable(
            hosts, **ssh_kwargs
        )

        if unreachable_hosts:
            click.echo(
                click.style(
                    f"Warning: Unreachable nodes: {', '.join(unreachable_hosts)}",
                    fg="yellow"
                )
            )

            if not click.confirm(
                "Continue with reachable nodes only?",
                default=False
            ):
                click.echo("Configuration aborted by user")
                return

            # Filter to reachable nodes only
            hosts = reachable_hosts
            host_hostname_map = {
                host: host_hostname_map[host]
                for host in reachable_hosts
                if host in host_hostname_map
            }

        # Step 2: Set hostnames
        click.echo(click.style(
            "\n=== Setting Cluster Hostnames ===", fg="blue"))
        hostname_results = await ssh_client.set_hostnames(
            host_hostname_map, **ssh_kwargs
        )

        for result in hostname_results:
            if result.get('error'):
                click.echo(
                    click.style(
                        f"Host {result['host']} hostname setup failed: {result['error']}",
                        fg="red"
                    )
                )
            else:
                hostname = host_hostname_map.get(
                    str(result['host']), 'unknown')
                click.echo(
                    click.style(
                        f"Host {result['host']} hostname set to {hostname}",
                        fg="green"
                    )
                )

        # Step 3: Configure SSH mutual trust
        click.echo(click.style("\n=== Configuring SSH Trust ===", fg="blue"))
        trust_result = await ssh_client.setup_ssh_mutual_trust(
            hosts, **ssh_kwargs
        )

        if trust_result.get('error'):
            click.echo(
                click.style(
                    f"SSH trust configuration failed: {trust_result['error']}",
                    fg="red"
                )
            )
        else:
            click.echo(
                click.style(
                    "SSH mutual trust configured successfully!",
                    fg="green"
                )
            )

        # Step 4: Verify SSH trust (optional)
        if verify_ssh and not trust_result.get('error'):
            await _verify_ssh_trust(ssh_client, hosts, **ssh_kwargs)

        # Step 5: Save cluster configuration
        await _save_cluster_config(hosts, host_hostname_map)

    except Exception as e:
        click.echo(
            click.style(f"Configuration workflow failed: {str(e)}", fg="red"),
            err=True
        )
    finally:
        await ssh_client.close_all_connections()
        click.echo(click.style(
            "\n=== All SSH connections closed ===", fg="blue"))


async def _verify_ssh_trust(
    ssh_client: AsyncSSHClient,
    hosts: List[str],
    **ssh_kwargs: Any
) -> None:
    """Verify SSH trust between cluster nodes.

    Args:
        ssh_client: SSH client instance
        hosts: List of hosts to verify
        **ssh_kwargs: SSH connection parameters
    """
    click.echo(click.style("\n=== Verifying SSH Trust ===", fg="blue"))

    # Build verification tasks
    verify_tasks: List[Tuple[str, str]] = []
    for src_host in hosts:
        for dest_host in hosts:
            if src_host != dest_host:
                cmd = (
                    f'ssh -o StrictHostKeyChecking=no '
                    f'-o ConnectTimeout=5 {dest_host} "echo success"'
                )
                verify_tasks.append((src_host, cmd))

    # Execute verification tasks
    verify_results = await ssh_client.execute_multiple_commands(
        verify_tasks, **ssh_kwargs
    )

    # Process verification results
    for result in verify_results:
        command_parts = str(result['command']).split()
        dest_host = command_parts[3] if len(command_parts) > 3 else 'unknown'

        if (result.get('exit_status') == 0 and 'success' in str(result.get('stdout', ''))):
            click.echo(
                click.style(
                    f"SSH trust verified: {result['host']} → {dest_host}",
                    fg="green"
                )
            )
        else:
            error_info = result.get('error') or result.get(
                'stderr', 'Unknown error')
            click.echo(
                click.style(
                    f"SSH trust failed: {result['host']} → {dest_host}: {error_info}",
                    fg="red"
                )
            )


async def _save_cluster_config(
    hosts: List[str],
    host_hostname_map: Dict[str, str]
) -> None:
    """Save cluster configuration to global config.

    Args:
        hosts: List of cluster node IPs
        host_hostname_map: Hostname mapping
    """
    try:
        config = ConfigDict.get_instance()

        # Initialize cluster config if not exists
        if not hasattr(config, 'cluster') or config.cluster is None:
            config.cluster = ConfigDict({})

        # Save cluster information
        config.cluster.nodes = hosts
        config.cluster.hostnames = host_hostname_map

        # Save to file
        config_path = os.path.join(
            Application.ROOT_DIR, "config", "application.yaml"
        )
        config.save_to_file(config_path)

        click.echo(
            click.style(
                f"\nCluster configuration saved to {config_path}",
                fg="green"
            )
        )

    except Exception as e:
        click.echo(
            click.style(f"Failed to save cluster config: {str(e)}", fg="red"),
            err=True
        )


@cli.command(name="configure-cluster")
@click.option(
    "--hosts",
    type=ListType,
    help="Cluster node IP list, comma-separated (e.g., 172.31.65.150,localhost)"
)
@click.option(
    "--hostname-map",
    help="IP to hostname mapping, comma-separated (e.g., 172.31.65.150:node-1,localhost:node-2)"
)
@click.option(
    "--username",
    default="root",
    help="SSH username (default: root)"
)
@click.option(
    "--password",
    help="SSH password (password auth takes priority over key auth)"
)
@click.option(
    "--key-file",
    default="~/.ssh/id_rsa",
    help="SSH private key file path"
)
@click.option(
    "--skip-verify",
    is_flag=True,
    default=False,
    help="Skip SSH trust verification"
)
def configure_cluster_command(
    hosts: Optional[List[str]],
    hostname_map: Optional[str],
    username: str,
    password: str,
    key_file: str,
    skip_verify: bool
) -> None:
    """Configure cluster with hostnames and SSH trust.

    If --hosts and --hostname-map are not provided, the command will attempt
    to load cluster configuration from the application config file.

    Examples:
        $ python cluster.py configure-cluster \\
            --hosts 172.31.65.150,localhost \\
            --hostname-map 172.31.65.150:node-1,localhost:node-2 \\
            --username root \\
            --key-file ~/.ssh/id_rsa

        # Load from config file:
        $ python cluster.py configure-cluster --username root --key-file ~/.ssh/id_rsa
    """
    try:
        # Get hosts from input or config
        loaded_hosts = _get_cluster_hosts(hosts)
        if not loaded_hosts:
            return

        # Get hostname mapping from input or config
        loaded_hostname_map = _get_cluster_hostname_map(
            hostname_map, loaded_hosts)
        if not loaded_hostname_map:
            return

        # Validate all hosts have hostname mapping
        _validate_host_hostname_mapping(loaded_hosts, loaded_hostname_map)

        # Build SSH connection parameters
        ssh_kwargs = _build_ssh_params(username, password, key_file)

        # Execute configuration workflow
        asyncio.run(
            configure_cluster_workflow(
                hosts=loaded_hosts,
                verify_ssh=not skip_verify,
                host_hostname_map=loaded_hostname_map,
                **ssh_kwargs
            )
        )

    except ValueError as e:
        click.echo(
            click.style(f"Configuration error: {str(e)}", fg="red"), err=True
        )


@cli.command(name="show-cluster-config")
def show_cluster_config() -> None:
    """Display current cluster configuration from config file."""
    cluster_config = _load_cluster_config()

    if not cluster_config:
        click.echo(
            click.style("No cluster configuration found", fg="yellow")
        )
        return

    click.echo(click.style("=== Cluster Configuration ===", fg="blue", bold=True))
    click.echo()

    hosts = cluster_config.get('hosts', [])
    if hosts:
        click.echo(click.style("Nodes:", fg="cyan"))
        for host in hosts:
            click.echo(f"  • {host}")

    hostnames = cluster_config.get('hostnames', {})
    if hostnames:
        click.echo(click.style("\nHostname Mapping:", fg="cyan"))
        for ip, hostname in hostnames.items():
            click.echo(f"  • {ip} → {hostname}")

    click.echo()


async def execute_command_workflow(
    hosts: List[str],
    command: str,
    **ssh_kwargs: Any
) -> None:
    """Execute command on cluster nodes.

    Args:
        hosts: Target cluster node IP list
        command: Command to execute
        **ssh_kwargs: SSH connection parameters
    """
    ssh_client = AsyncSSHClient()

    try:
        # Check node reachability
        click.echo(click.style(
            "\n=== Checking Node Reachability ===", fg="blue"))
        reachable_hosts, unreachable_hosts = await ssh_client.is_reachable(
            hosts, **ssh_kwargs
        )

        if unreachable_hosts:
            click.echo(
                click.style(
                    f"Skipping unreachable nodes: {', '.join(unreachable_hosts)}",
                    fg="yellow"
                )
            )

        if not reachable_hosts:
            click.echo(
                click.style("No reachable nodes found!", fg="red"),
                err=True
            )
            return

        # Build command execution tasks
        click.echo(
            click.style(
                f"\n=== Executing command on {len(reachable_hosts)} nodes: {command} ===",
                fg="blue"
            )
        )
        cmd_tasks = [(host, command) for host in reachable_hosts]

        # Execute commands
        results = await ssh_client.execute_multiple_commands(
            cmd_tasks, **ssh_kwargs
        )

        # Display results
        click.echo(click.style(
            "\n=== Command Execution Results ===", fg="blue"))
        for result in results:
            _display_command_result(result)

    except Exception as e:
        click.echo(
            click.style(f"Command execution failed: {str(e)}", fg="red"),
            err=True
        )
    finally:
        await ssh_client.close_all_connections()
        click.echo(click.style(
            "\n=== All SSH connections closed ===", fg="blue"))


def _display_command_result(result: Dict[str, Any]) -> None:
    """Display command execution result in formatted way.

    Args:
        result: Command execution result dictionary
    """
    host = result['host']
    click.echo(click.style(f"\n【Node {host}】", fg="cyan", bold=True))

    if result.get('error'):
        click.echo(click.style("  Status: Failed", fg="red"))
        click.echo(click.style(f"  Error: {result['error']}", fg="red"))
    else:
        exit_status = result.get('exit_status', -1)
        status = "Success" if exit_status == 0 else f"Exit code: {exit_status}"
        status_color = "green" if exit_status == 0 else "yellow"

        click.echo(click.style(f"  Status: {status}", fg=status_color))

        stdout = result.get('stdout', '').strip()
        if stdout:
            click.echo(click.style("  Stdout:", fg="blue"))
            click.echo(f"    {stdout}")

        stderr = result.get('stderr', '').strip()
        if stderr:
            click.echo(click.style("  Stderr:", fg="red"))
            click.echo(f"    {stderr}")


@cli.command(name="execute-cmd")
@click.option(
    "--hosts",
    type=ListType,
    help="Cluster node IP list, comma-separated (if not provided, loads from config)"
)
@click.option(
    "--cmd",
    required=True,
    help="Command to execute (e.g., 'df -h', 'systemctl status docker')"
)
@click.option(
    "--username",
    default="root",
    help="SSH username (default: root)"
)
@click.option(
    "--password",
    help="SSH password (takes priority over key auth)"
)
@click.option(
    "--key-file",
    default="~/.ssh/id_rsa",
    help="SSH private key file path"
)
def execute_command(
    hosts: Optional[List[str]],
    cmd: str,
    username: str,
    password: str,
    key_file: str
) -> None:
    """Execute command on cluster nodes.

    If --hosts is not provided, the command will attempt to load
    cluster configuration from the application config file.

    Examples:
        1. Check disk usage on configured nodes:
            $ python cluster.py execute-cmd --cmd "df -h" --username root

        2. Execute on specific nodes:
            $ python cluster.py execute-cmd \\
                --hosts 172.31.65.150,localhost \\
                --cmd "df -h" \\
                --username root \\
                --key-file ~/.ssh/id_rsa

        3. Restart Docker service:
            $ python cluster.py execute-cmd \\
                --cmd "systemctl restart docker" \\
                --username root \\
                --password your-password
    """
    try:
        # Get hosts from input or config
        loaded_hosts = _get_cluster_hosts(hosts)
        if not loaded_hosts:
            return

        ssh_kwargs = _build_ssh_params(username, password, key_file)

        asyncio.run(
            execute_command_workflow(
                hosts=loaded_hosts,
                command=cmd,
                **ssh_kwargs
            )
        )

    except ValueError as e:
        click.echo(
            click.style(f"Configuration error: {str(e)}", fg="red"), err=True
        )


@cli.command(name="disable-firewalld")
@click.option(
    "--hosts",
    type=ListType,
    help="Cluster node IP list, comma-separated (if not provided, loads from config)"
)
@click.option(
    "--username",
    default="root",
    help="SSH username (default: root)"
)
@click.option(
    "--password",
    help="SSH password"
)
@click.option(
    "--key-file",
    default="~/.ssh/id_rsa",
    help="SSH private key file path"
)
def disable_firewalld(
    hosts: Optional[List[str]],
    username: str,
    password: str,
    key_file: str
) -> None:
    """Disable and stop firewall service on cluster nodes.

    If --hosts is not provided, the command will attempt to load
    cluster configuration from the application config file.

    Example:
        $ python cluster.py disable-firewalld --username root

        # Or on specific nodes:
        $ python cluster.py disable-firewalld \\
            --hosts 172.31.65.150,localhost \\
            --username root
    """
    try:
        # Get hosts from input or config
        loaded_hosts = _get_cluster_hosts(hosts)
        if not loaded_hosts:
            return

        ssh_kwargs = _build_ssh_params(username, password, key_file)
        cmd = "systemctl stop firewalld && systemctl disable firewalld"

        asyncio.run(
            execute_command_workflow(
                hosts=loaded_hosts,
                command=cmd,
                **ssh_kwargs
            )
        )

    except ValueError as e:
        click.echo(
            click.style(f"Configuration error: {str(e)}", fg="red"), err=True
        )


def _parse_hostname_mapping(hostname_map: str) -> Dict[str, str]:
    """Parse hostname mapping from comma-separated string.

    Args:
        hostname_map: Comma-separated IP:hostname pairs

    Returns:
        Dictionary mapping IP to hostname

    Raises:
        ValueError: If format is invalid
    """
    host_hostname_dict: Dict[str, str] = {}

    try:
        for item in hostname_map.split(","):
            item = item.strip()
            if ":" not in item:
                raise ValueError(
                    f"Invalid format: {item} (expected IP:hostname)"
                )

            ip, hostname = item.split(":", 1)
            host_hostname_dict[ip.strip()] = hostname.strip()

        return host_hostname_dict

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Failed to parse hostname mapping: {str(e)}")


def _validate_host_hostname_mapping(
    hosts: List[str],
    host_hostname_map: Dict[str, str]
) -> None:
    """Validate all hosts have corresponding hostname mapping.

    Args:
        hosts: List of host IPs
        host_hostname_map: Hostname mapping dictionary

    Raises:
        ValueError: If any host lacks hostname mapping
    """
    for host in hosts:
        if host not in host_hostname_map:
            raise ValueError(f"Host {host} missing hostname mapping")


def _build_ssh_params(
    username: str,
    password: str,
    key_file: str
) -> Dict[str, Any]:
    """Build SSH connection parameters.

    Args:
        username: SSH username
        password: SSH password
        key_file: SSH private key file path

    Returns:
        SSH connection parameters dictionary
    """
    ssh_kwargs: Dict[str, Union[str, List[str]]] = {"username": username}

    if password:
        ssh_kwargs["password"] = password
    else:
        ssh_kwargs["client_keys"] = [os.path.expanduser(key_file)]

    return ssh_kwargs


if __name__ == '__main__':
    """Entry point for CLI commands."""
    cli()
