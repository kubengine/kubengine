"""Credential edge cases at logging and command-result boundaries."""

import logging
import shlex
import sys

import pytest

from core.command import execute_command
from core.logger import LogContextFilter, ReadableFormatter, log_lifecycle_event
from core.redaction import redact_text, safe_command


@pytest.mark.parametrize(
    "command",
    [
        shlex.join(["helm", "push", "--password", "leading'private-tail with spaces"]),
        'helm push --password "leading\\"private-tail with spaces"',
        "helm push --password leading\\ private-tail",
        "helm push --password 'leading private-tail",
    ],
)
def test_freeform_logs_hide_entire_shell_quoted_password(command):
    rendered = redact_text(f"Executing {command}")
    assert "private-tail" not in rendered
    assert "leading" not in rendered


@pytest.mark.parametrize("as_shell", [False, True])
def test_safe_command_masks_quoted_values_without_mutating_argv(as_shell):
    secret = "leading'private-tail with spaces"
    arguments = ["helm", "push", "--password", secret]
    rendered = safe_command(shlex.join(arguments) if as_shell else arguments)
    assert "private-tail" not in rendered
    assert "leading" not in rendered
    assert arguments[-1] == secret


def test_escaped_credential_echo_is_removed_from_results_and_logs(caplog):
    secret = "leading'\"private-escaped-tail with spaces"
    with caplog.at_level(logging.DEBUG):
        result = execute_command(
            [
                sys.executable, "-c", "import sys; print(repr(sys.argv[-1]))",
                "--password", secret,
            ]
        )
    assert result.is_success()
    assert "private-escaped-tail" not in result.stdout
    assert "private-escaped-tail" not in caplog.text


def test_filter_masks_known_credentials_in_structured_error_and_exception():
    secret = "private-extra-field-marker"
    try:
        raise ValueError(f"remote rejected {secret}")
    except ValueError:
        record = logging.LogRecord(
            "redaction.review", logging.ERROR, __file__, 1,
            "operation failed", (), sys.exc_info(),
        )
    record.command = shlex.join(["helm", "push", "--password", secret])
    record.error = f"remote rejected {secret}"
    LogContextFilter().filter(record)
    rendered = ReadableFormatter("%(message)s %(error)s").format(record)
    assert secret not in rendered
    assert secret not in record.error
    assert record.exc_info is None or secret not in str(record.exc_info[1])


def test_generated_private_key_is_preserved_in_result_but_never_logged(caplog):
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        "ISOLATED_PRIVATE_KEY_BODY_MARKER\n"
        "-----END PRIVATE KEY-----"
    )
    with caplog.at_level(logging.DEBUG):
        result = execute_command(
            [sys.executable, "-c", "import os; print(os.environ['REVIEW_GENERATED_OUTPUT'])"],
            env={"REVIEW_GENERATED_OUTPUT": private_key},
        )
    assert result.is_success()
    assert result.stdout == private_key
    assert "ISOLATED_PRIVATE_KEY_BODY_MARKER" not in caplog.text
    assert all("ISOLATED_PRIVATE_KEY_BODY_MARKER" not in str(record.__dict__) for record in caplog.records)


def test_generated_join_output_survives_while_logs_redact_new_credentials(caplog):
    join = "kubeadm join test.invalid --token newly-generated-token --certificate-key newly-generated-key"
    with caplog.at_level(logging.DEBUG):
        result = execute_command(
            [sys.executable, "-c", "import os; print(os.environ['REVIEW_GENERATED_OUTPUT'])"],
            env={"REVIEW_GENERATED_OUTPUT": join},
        )
    assert result.stdout == join
    assert "newly-generated-token" not in caplog.text
    assert "newly-generated-key" not in caplog.text


def test_lifecycle_masks_credentials_in_nested_structured_errors(caplog):
    secret = "private-nested-error-marker"
    with caplog.at_level(logging.INFO):
        log_lifecycle_event(
            logging.getLogger("redaction.lifecycle"), "command_end",
            command=shlex.join(["helm", "push", "--password", secret]),
            error={"message": secret, "details": [secret]},
        )
    assert secret not in caplog.text
    assert secret not in str(caplog.records[-1].error)
