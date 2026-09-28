import logging
import shlex
import sys

import pytest

from core.command import CommandError, execute_command
from core.logger import log_lifecycle_event
from core.redaction import safe_command


def test_argument_list_does_not_execute_shell_syntax(tmp_path):
    marker = tmp_path / "injected"
    payload = f"$(touch {marker}); echo injected"
    result = execute_command(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", payload]
    )
    assert result.is_success()
    assert result.stdout == payload
    assert not marker.exists()


def test_argument_list_environment_preserves_context():
    result = execute_command(
        [
            sys.executable,
            "-c",
            (
                "import os; print(os.environ['KUBENGINE_TEST_VALUE']); "
                "print(bool(os.environ.get('PATH')))"
            ),
        ],
        env={"KUBENGINE_TEST_VALUE": "test-value"},
    )
    assert result.stdout == "test-value\nTrue"


@pytest.mark.parametrize("as_shell", [False, True])
def test_credentials_are_removed_from_logs_errors_and_results(
    caplog, as_shell
):
    secret = "private'credential with spaces"
    args = [
        sys.executable,
        "-c",
        (
            "import sys; print(sys.argv[-1]); print(sys.argv[-1], "
            "file=sys.stderr); sys.exit(7)"
        ),
        "--password",
        secret,
    ]
    cmd = shlex.join(args) if as_shell else args
    with caplog.at_level(logging.DEBUG), pytest.raises(CommandError) as caught:
        execute_command(cmd, fail_action="raise")
    assert secret not in str(caught.value)
    assert secret not in caught.value.command
    assert secret not in caught.value.result.stdout
    assert secret not in caught.value.result.stderr
    assert "private" not in caplog.text
    assert "credential" not in caplog.text
    assert all(secret not in str(record.__dict__) for record in caplog.records)


def test_generated_join_command_is_not_destroyed_in_result(caplog):
    join = (
        "kubeadm join example --token generated-token "
        "--certificate-key generated-key"
    )
    with caplog.at_level(logging.DEBUG):
        # Read the output from stdin-free code without placing the
        # credential in argv.
        result = execute_command(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ['JOIN_RESULT'])",
            ],
            env={"JOIN_RESULT": join},
        )
    assert result.stdout == join
    assert "generated-token" not in caplog.text
    assert "generated-key" not in caplog.text


def test_ssh_lifecycle_redacts_command_and_error(caplog):
    with caplog.at_level(logging.INFO):
        log_lifecycle_event(
            logging.getLogger("review.ssh"),
            "ssh_command_end",
            command="ctr push -u user:secret-example image",
            error="secret-example failed",
        )
    assert "secret-example" not in caplog.text
    assert caplog.records[-1].error == "[REDACTED] failed"
    assert "secret-example" not in caplog.records[-1].command


def test_malformed_credential_command_is_not_logged():
    assert "secret" not in safe_command("helm --password 'secret")


def test_interruption_terminates_surviving_process_group(monkeypatch):
    import signal
    from unittest.mock import MagicMock, Mock

    from core import command

    process = Mock(pid=12345)
    process.wait.side_effect = [KeyboardInterrupt(), 0]
    process.poll.return_value = 0
    monkeypatch.setattr(
        command.subprocess, "Popen", MagicMock(return_value=process)
    )
    signals = []
    monkeypatch.setattr(
        command.os, "killpg", lambda pid, sig: signals.append((pid, sig))
    )
    with pytest.raises(KeyboardInterrupt):
        execute_command(["example-operation"])
    assert (12345, signal.SIGTERM) in signals
    # Descendants may survive even though the group leader has exited.
    assert (12345, signal.SIGKILL) in signals
