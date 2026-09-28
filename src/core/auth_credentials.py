"""Private signing material and freshly loaded authentication configuration."""

import base64
import os
from pathlib import Path
import secrets
import stat
import tempfile
from typing import Any

from core.config import Application, ConfigDict


def load_auth_users() -> dict[str, dict[str, Any]]:
    source = ConfigDict.get_instance()._source_path
    config = ConfigDict.load_from_file(source, use_cache=False)
    users = config.get("auth", {}).get("users", {})
    return {name: record for name, record in users.items() if isinstance(record, dict)}


def load_signing_secret() -> str:
    """Publish a complete key atomically; never reuse a TLS private key."""
    key_path = Path(Application.ROOT_DIR) / "config" / "jwt-signing.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".jwt-", dir=key_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, key_path)
            except FileExistsError:
                pass  # Another worker published its key first.
        finally:
            os.unlink(temporary)
    fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise PermissionError("JWT signing key must be a private regular file (mode 0600)")
        secret = stream.read().strip()
    if len(base64.urlsafe_b64decode(secret)) < 32:
        raise ValueError("JWT signing key must contain at least 256 bits")
    return secret
