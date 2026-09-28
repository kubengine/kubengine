"""Validated limits shared by ingress, archive storage and
maintenance.
"""

from core.config import ConfigDict

DEFAULTS = {
    "image_bytes": 10 * 1024**3,
    "archive_bytes": 50 * 1024**3,
    "reserve_bytes": 1024**3,
    "concurrency": 2,
    "timeout_seconds": 3600,
    "retention_seconds": 7 * 86400,
}


def upload_limit(name: str) -> int:
    config = ConfigDict.get_instance().get("security", {}).get("uploads", {})
    value = config.get(name, DEFAULTS[name])
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"security.uploads.{name} must be a positive integer")
    return value
