"""Redact command credentials before they enter logs or error
responses.
"""

import json
import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
_FLAGS = {
    "--password",
    "--creds",
    "--credentials",
    "--user",
    "-u",
    "--token",
    "--certificate-key",
    "--secret",
    "--ssh-password",
    "--registry-password",
    "--access-token",
    "--client-secret",
}
_SECRET_NAME = re.compile(
    r"(?:password|passwd|secret|token|credential|private_key)", re.I
)
_URL_AUTH = re.compile(
    r"(?P<prefix>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<secret>[^\s/@]+:[^\s/@]+)@"
)
_FLAG_PREFIX = re.compile(
    r"(?P<flag>(?<![\w-])(?:"
    + "|".join(
        re.escape(flag) for flag in sorted(_FLAGS, key=len, reverse=True)
    )
    + r"))(?P<sep>=|\s+)"
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S
)


def command_secrets(command: str | Sequence[str]) -> tuple[str, ...]:
    """
    Find credential values, including quoted flags and credential URLs.
    """
    try:
        args = (
            shlex.split(command) if isinstance(command, str) else list(command)
        )
    except ValueError:
        # Do not guess shell token boundaries when quoting is
        # incomplete.
        return ()
    values: set[str] = set()
    expect_secret = False
    for arg in args:
        if expect_secret:
            values.add(arg)
            if ":" in arg:
                values.add(arg.split(":", 1)[1])
            expect_secret = False
            continue
        flag, sep, value = arg.partition("=")
        if flag in _FLAGS:
            if sep:
                values.add(value)
                if ":" in value:
                    values.add(value.split(":", 1)[1])
            else:
                expect_secret = True
        elif sep and _SECRET_NAME.search(flag):
            values.add(value)
        for match in _URL_AUTH.finditer(arg):
            values.add(match["secret"])
            values.add(match["secret"].split(":", 1)[1])
    return tuple(
        sorted(
            (value for value in values if value and value != REDACTED),
            key=len,
            reverse=True,
        )
    )


def redact_known_values(value: str, secrets: Sequence[str] = ()) -> str:
    """Mask known values without changing unrelated command output."""
    variants: set[str] = set()
    for secret in secrets:
        if secret and secret != REDACTED:
            # CLIs and exception formatters commonly echo credentials
            # using shell, repr or JSON escaping rather than the literal
            # input.
            variants.update(
                (
                    secret,
                    shlex.quote(secret),
                    repr(secret)[1:-1],
                    json.dumps(secret, ensure_ascii=False)[1:-1],
                    json.dumps(secret)[1:-1],
                )
            )
    for secret in sorted(variants, key=len, reverse=True):
        value = value.replace(secret, REDACTED)
    return value


def _redact_flag_values(value: str) -> str:
    """
    Consume complete shell words, including adjacent and escaped quotes.

    This scans credential arguments only; it never evaluates shell
    syntax. An unfinished quote is conservatively hidden through the end
    of the message.
    """
    parts: list[str] = []
    cursor = 0
    for match in _FLAG_PREFIX.finditer(value):
        if match.start() < cursor:
            continue
        end = match.end()
        quote = ""
        while end < len(value):
            char = value[end]
            if not quote and (char.isspace() or char in ";|&"):
                break
            if char == "\\" and quote != "'":
                end = min(len(value), end + 2)
                continue
            if char == quote:
                quote = ""
            elif not quote and char in "'\"":
                quote = char
            end += 1
        if end == match.end():
            continue
        parts.extend((value[cursor : match.end()], REDACTED))
        cursor = end
    parts.append(value[cursor:])
    return "".join(parts)


def redact_text(value: str, secrets: Sequence[str] = ()) -> str:
    """Mask known values as well as common credential-bearing text."""
    value = redact_known_values(value, secrets)
    value = _PRIVATE_KEY.sub(REDACTED, value)
    value = _URL_AUTH.sub(
        lambda match: match["prefix"] + REDACTED + "@", value
    )
    value = _redact_flag_values(value)
    return _BEARER.sub("Bearer " + REDACTED, value)


def redact_value(value: Any, secrets: Sequence[str] = ()) -> Any:
    """Sanitize JSON-like structured log fields before constructing a
    record.
    """
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            key: redact_value(item, secrets) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets) for item in value)
    if isinstance(value, BaseException):
        return redact_text(str(value), secrets)
    return value


def safe_command(command: str | Sequence[str]) -> str:
    """Return a log representation, never the credential-bearing
    argv.
    """
    if isinstance(command, str):
        try:
            args = shlex.split(command)
        except ValueError:
            return "[command omitted: invalid shell quoting]"
    else:
        args = list(command)
    secrets = command_secrets(args)
    return redact_text(
        shlex.join([redact_known_values(arg, secrets) for arg in args])
    )
