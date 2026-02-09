"""Properties file parsing and manipulation utilities.

This module provides utilities for parsing properties files and converting
dot-separated key-value strings into nested dictionaries with intelligent
type conversion.
"""

from typing import Any, Dict, List, Union


def convert_property_value(raw_value: str) -> Union[int, float, bool, None, str]:
    """Intelligently convert property value to appropriate Python type.

    Conversion rules:
    - "true"/"false" (case-insensitive) → bool
    - "null"/"none" (case-insensitive) → None
    - Pure digits → int
    - Numbers with decimal point → float
    - Others → str (empty string returns "")

    Args:
        raw_value: The raw string value from properties file.

    Returns:
        Converted value with appropriate type.
    """
    # Remove leading and trailing whitespace
    value = raw_value.strip()

    # Handle empty values
    if not value:
        return ""

    # Handle boolean values
    lower_value = value.lower()
    if lower_value in ("true", "false"):
        return lower_value == "true"

    # Handle None values
    if lower_value in ("null", "none"):
        return None

    # Handle numeric values (try int first, then float)
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            # Non-numeric, return original string
            return value


def parse_properties_to_dict(properties_data: str) -> Dict[str, Any]:
    """Convert properties format string to nested dictionary.

    Args:
        properties_data: Properties format string (one key-value pair per line).

    Returns:
        Nested dictionary structure.
    """
    result: Dict[str, Any] = {}
    lines = properties_data.splitlines()

    for line_number, line in enumerate(lines, 1):
        # Remove leading and trailing whitespace
        stripped_line = line.strip()

        # Skip empty lines and comment lines (starting with # or !)
        if not stripped_line or stripped_line.startswith(("#", "!")):
            continue

        # Split key and value (split on first = to handle = in values)
        if "=" not in stripped_line:
            print(f"Warning: Line {line_number} has no '=', skipping → {line}")
            continue

        key_part, value_part = stripped_line.split("=", 1)

        # Process key: split into hierarchical levels
        key_levels: List[str] = [
            level.strip() for level in key_part.split(".")
            if level.strip()
        ]

        if not key_levels:
            print(
                f"Warning: Line {line_number} has empty key, skipping → {line}")
            continue

        # Convert value type
        converted_value = convert_property_value(value_part)

        # Build nested dictionary: navigate to parent of final key
        current_dict = result
        for level in key_levels[:-1]:  # All levels except the last
            # Create empty dict if level doesn't exist or isn't a dict
            if level not in current_dict or not isinstance(current_dict[level], dict):
                current_dict[level] = {}
            # Move to next level
            current_dict = current_dict[level]

        # Assign value to final key
        final_key = key_levels[-1]
        current_dict[final_key] = converted_value

    return result


def convert_dot_notation_to_dict(dot_string: str) -> Dict[str, Any]:
    """Convert dot-separated key-value string to nested dictionary.

    Args:
        dot_string: Dot-separated key-value string in format "key.key.key=value".

    Returns:
        Nested Python dictionary object.

    Raises:
        ValueError: When input string format is invalid (no = or multiple =).
    """
    # Split key-value pair (ensure exactly one =)
    parts = dot_string.split("=")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid input format, expected 'key=value': {dot_string}")

    keys_str, value = parts[0].strip(), parts[1].strip()

    # Split hierarchical keys
    key_levels = keys_str.split(".")
    if not key_levels or key_levels[-1] == "":
        raise ValueError(
            f"Invalid key format, cannot end with dot: {dot_string}")

    # Build nested dictionary (from innermost to outermost)
    result: Dict[str, Any] = {}
    current_level = result

    for index, key in enumerate(key_levels):
        if not key:  # Handle empty keys (like "a..b=value")
            raise ValueError(f"Key contains empty level: {dot_string}")

        # Last level gets the value, others get empty dict
        if index == len(key_levels) - 1:
            # Process the value
            current_level[key] = convert_property_value(value)
        else:
            current_level[key] = {}
            current_level = current_level[key]

    return result


# ------------------- Usage Examples -------------------
if __name__ == "__main__":
    # Parse target string
    input_string = "sentinel.resources.requests.memory=8Gi"
    python_object = convert_dot_notation_to_dict(input_string)

    # Print result (nested dictionary)
    print("Converted Python object:")
    print(python_object)
    # Output: {'sentinel': {'resources': {'requests': {'memory': '8Gi'}}}}

    # Access specific value
    memory_value = python_object["sentinel"]["resources"]["requests"]["memory"]
    print(f"\nAccess memory value: {memory_value}")
    # Output: 8Gi
