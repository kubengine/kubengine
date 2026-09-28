import base64
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import asyncssh
import pytest
import click
from click.testing import CliRunner

from core.ssh import AsyncSSHClient


def run_local_script(script, tmp_path):
    # Exercise the generated shell against fixtures only. Never touch the user's
    # SSH directory or contact an SSH endpoint.
    script = script.replace("~/.ssh", shlex.quote(str(tmp_path / "ssh")))
    script = script.replace("/etc/ssh/ssh_known_hosts", shlex.quote(str(tmp_path / "system_known_hosts")))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)


def test_authorized_keys_preserves_existing_grants_and_is_idempotent(tmp_path):
    directory = tmp_path / "ssh"
    directory.mkdir()
    target = directory / "authorized_keys"
    original = '# recovery key\nfrom="192.0.2.1" ssh-ed25519 AAAAfixture operator\n'
    target.write_text(original)
    new_key = "ssh-ed25519 AAAAcluster cluster-node\n"
    script = AsyncSSHClient._authorized_keys_merge_script(base64.b64encode(new_key.encode()).decode())
    for _ in range(2):
        result = run_local_script(script, tmp_path)
        assert result.returncode == 0, result.stderr
    assert original in target.read_text()
    assert target.read_text().count(new_key) == 1
    assert target.stat().st_mode & 0o777 == 0o600


def test_authorized_keys_invalid_payload_leaves_original_untouched(tmp_path):
    directory = tmp_path / "ssh"
    directory.mkdir()
    target = directory / "authorized_keys"
    original = "ssh-ed25519 AAAAfixture rescue\n"
    target.write_text(original)
    result = run_local_script(AsyncSSHClient._authorized_keys_merge_script("!invalid!"), tmp_path)
    assert result.returncode != 0
    assert target.read_text() == original


def test_concurrent_authorized_keys_updates_preserve_both_additions(tmp_path):
    keys = ["ssh-ed25519 AAAAfirst first\n", "ssh-ed25519 AAAAsecond second\n"]
    scripts = [AsyncSSHClient._authorized_keys_merge_script(base64.b64encode(key.encode()).decode()) for key in keys]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda script: run_local_script(script, tmp_path), scripts))
    assert all(result.returncode == 0 for result in results)
    content = (tmp_path / "ssh/authorized_keys").read_text()
    assert all(key in content for key in keys)


def test_known_hosts_conflict_preserves_existing_trust(tmp_path):
    directory = tmp_path / "ssh"
    directory.mkdir()
    target = directory / "known_hosts"
    previous = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
    incoming = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
    original = f"192.0.2.10 {previous}\n"
    target.write_text(original)
    payload = base64.b64encode(f"192.0.2.10 {incoming}\n".encode()).decode()
    script = AsyncSSHClient._known_hosts_refresh_script("192.0.2.10", ["192.0.2.10"], payload)
    result = run_local_script(script, tmp_path)
    assert result.returncode != 0
    assert "Host key conflict" in result.stderr
    assert target.read_text() == original


def test_known_hosts_adds_verified_key_without_network_scan(tmp_path):
    public = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
    entry = f"192.0.2.10 {public}\n"
    payload = base64.b64encode(entry.encode()).decode()
    script = AsyncSSHClient._known_hosts_refresh_script("192.0.2.10", ["192.0.2.10"], payload)
    assert "ssh-keyscan" not in script
    assert "ssh-keygen -R" not in script
    for _ in range(2):
        result = run_local_script(script, tmp_path)
        assert result.returncode == 0, result.stderr
    assert (tmp_path / "ssh" / "known_hosts").read_text().count(entry) == 1


@pytest.mark.asyncio
async def test_unknown_host_fails_closed_with_enrollment_guidance(monkeypatch):
    seen = []
    async def reject(host, **kwargs):
        seen.append(kwargs)
        raise asyncssh.HostKeyNotVerifiable("Host key is not trusted")
    monkeypatch.setattr("core.ssh.asyncssh.connect", reject)
    client = AsyncSSHClient()
    result = await client.execute_command("unknown.example", "true")
    assert seen[0]["known_hosts"] is not None
    assert result["exit_status"] is None
    assert "--known-hosts" in result["error"]
    assert "已拒绝连接" in result["error"]


@pytest.mark.asyncio
async def test_disabling_host_verification_is_rejected_before_connect(monkeypatch):
    async def never_connect(*args, **kwargs):
        pytest.fail("must not connect without host verification")
    monkeypatch.setattr("core.ssh.asyncssh.connect", never_connect)
    with pytest.raises(ValueError, match="known_hosts=None"):
        await AsyncSSHClient()._get_connection("example", known_hosts=None)


@pytest.mark.asyncio
async def test_changed_connection_identity_does_not_reuse_connection(monkeypatch):
    connections = []
    async def closed():
        pass
    async def connect(host, **kwargs):
        connection = SimpleNamespace(is_closed=lambda: False, close=lambda: None, wait_closed=closed)
        connections.append(connection)
        return connection
    monkeypatch.setattr("core.ssh.asyncssh.connect", connect)
    client = AsyncSSHClient()
    first = await client._get_connection("node", username="one", known_hosts="one")
    second = await client._get_connection("node", username="two", known_hosts="two")
    assert first is not second
    assert len(connections) == 2
    await client.close_all_connections()


def test_config_cli_passes_explicit_verified_host_file(monkeypatch, tmp_path):
    import cli.cluster as cluster
    trusted = tmp_path / "verified-hosts"
    trusted.write_text("synthetic verified host file")
    seen = []
    async def configure(**kwargs):
        seen.append(kwargs)
    monkeypatch.setattr(cluster, "configure_cluster_workflow", configure)
    result = CliRunner().invoke(cluster.cli, [
        "config", "--hosts", "192.0.2.10", "--hostname-map", "192.0.2.10:node-1",
        "--known-hosts", str(trusted),
    ])
    assert result.exit_code == 0, result.output
    assert seen[0]["known_hosts"] == str(trusted)


@pytest.mark.asyncio
async def test_config_workflow_reports_unverified_nodes_without_mutation(monkeypatch, capsys):
    import cli.cluster as cluster
    class UnverifiedSSH:
        async def is_reachable(self, hosts, **kwargs):
            return [], hosts
        async def close_all_connections(self):
            pass
        async def set_hostnames(self, *args, **kwargs):
            pytest.fail("unverified hosts must not be modified")
    monkeypatch.setattr(cluster, "AsyncSSHClient", UnverifiedSSH)
    with pytest.raises(click.ClickException, match="主机密钥校验"):
        await cluster.configure_cluster_workflow(["192.0.2.10"], False, {"192.0.2.10": "node-1"})
    assert "--known-hosts" in capsys.readouterr().err
