"""Custom Click parameter types for CLI commands.

This module defines custom parameter types for Click command-line interface,
including list parsing and validation utilities.
"""

from typing import Any, List, Optional
import click


class ListParamType(click.ParamType):
    """Custom Click parameter type for parsing comma-separated strings into lists.

    This type converts comma-separated input strings into Python lists,
    automatically handling whitespace trimming and empty values.
    """

    name = "list"

    def __init__(self, separator: str = ",", trim: bool = True) -> None:
        """Initialize list parameter type.

        Args:
            separator: Character used to separate list items (default: comma)
            trim: Whether to trim whitespace from items (default: True)
        """
        self.separator = separator
        self.trim = trim
        super().__init__()

    def convert(
        self,
        value: Any,
        param: Optional[click.Parameter],
        ctx: Optional[click.Context]
    ) -> List[str]:
        """Convert input value to list format.

        Args:
            value: Input parameter value
            param: Click parameter object
            ctx: Click context object

        Returns:
            List of strings with optional whitespace trimming

        Raises:
            click.BadParameter: If value type is invalid
        """
        # If already a list, return as-is
        if isinstance(value, list):
            return [str(item) for item in value]  # type: ignore

        # Handle None value
        if value is None:
            return []

        # Convert to string if not already
        if not isinstance(value, str):
            raise click.BadParameter(
                f"Expected string or list, got {type(value).__name__}",
                param=param
            )

        # Handle empty string
        if not value.strip():
            return []

        # Split by separator and optionally trim whitespace
        if self.trim:
            return [item.strip() for item in value.split(self.separator) if item.strip()]
        else:
            return [item for item in value.split(self.separator) if item]


class HostListType(ListParamType):
    """Specialized parameter type for host lists with validation."""

    name = "host_list"

    def __init__(self) -> None:
        """Initialize host list parameter type."""
        super().__init__(separator=",", trim=True)

    def convert(
        self,
        value: Any,
        param: Optional[click.Parameter],
        ctx: Optional[click.Context]
    ) -> List[str]:
        """Convert input value to validated host list.

        Args:
            value: Input parameter value
            param: Click parameter object
            ctx: Click context object

        Returns:
            List of validated host addresses

        Raises:
            click.BadParameter: If host format is invalid
        """
        host_list = super().convert(value, param, ctx)

        # Validate each host format
        for host in host_list:
            if not self._is_valid_host(host):
                raise click.BadParameter(
                    f"Invalid host format: '{host}'. "
                    "Expected IP address or hostname",
                    param=param
                )

        return host_list

    def _is_valid_host(self, host: str) -> bool:
        """Validate if host string is a valid IP or hostname.

        Args:
            host: Host string to validate

        Returns:
            True if host format is valid
        """
        # Simple validation for demonstration
        # In production, you might want more sophisticated validation
        if not host:
            return False

        # Check for valid characters
        valid_chars = (
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789"
            ".-_"
        )

        return all(char in valid_chars for char in host)


class KeyValueMapType(click.ParamType):
    """Parameter type for parsing key:value mappings from comma-separated strings.

    Example: "key1:value1,key2:value2" -> {"key1": "value1", "key2": "value2"}
    """

    name = "key_value_map"

    def convert(
        self,
        value: Any,
        param: Optional[click.Parameter],
        ctx: Optional[click.Context]
    ) -> dict[str, str]:
        """Convert input string to key-value mapping.

        Args:
            value: Input parameter value
            param: Click parameter object
            ctx: Click context object

        Returns:
            Dictionary mapping keys to values

        Raises:
            click.BadParameter: If format is invalid
        """
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}  # type: ignore

        if value is None:
            return {}

        if not isinstance(value, str):
            raise click.BadParameter(
                f"Expected string or dict, got {type(value).__name__}",
                param=param
            )

        if not value.strip():
            return {}

        result: dict[str, str] = {}

        try:
            for pair in value.split(","):
                if ":" not in pair:
                    raise click.BadParameter(
                        f"Invalid key:value pair: '{pair}'. "
                        "Expected format 'key:value'",
                        param=param
                    )

                key, val = pair.split(":", 1)
                result[key.strip()] = val.strip()

            return result

        except ValueError as e:
            raise click.BadParameter(
                f"Failed to parse key-value mapping: {str(e)}",
                param=param
            )


# Global parameter type instances
LIST = ListParamType()
HOST_LIST = HostListType()
KEY_VALUE_MAP = KeyValueMapType()


# Backward compatibility aliases
ListType = LIST  # Deprecated: use LIST instead
