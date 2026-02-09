"""Password hashing and verification utilities.

This module provides secure password hashing and verification functions
using bcrypt for password storage and authentication.
"""

import bcrypt


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a hashed password.

    Args:
        plain_password: The plain text password to verify
        hashed_password: The bcrypt hashed password to verify against

    Returns:
        True if password matches, False otherwise

    Raises:
        ValueError: If passwords are invalid format
    """
    if not plain_password or not hashed_password:
        return False

    try:
        # Verify password using bcrypt
        plain_bytes = plain_password.encode("utf-8")
        hashed_bytes = hashed_password.encode("utf-8")

        return bcrypt.checkpw(plain_bytes, hashed_bytes)
    except (ValueError, UnicodeEncodeError) as e:
        raise ValueError(f"Invalid password format: {str(e)}")


def generate_password_hash(password: str) -> str:
    """Generate a secure hash for a plain password.

    Args:
        password: The plain text password to hash

    Returns:
        Bcrypt hash string suitable for database storage

    Raises:
        ValueError: If password is invalid or too weak
    """
    if not password:
        raise ValueError("Password cannot be empty")

    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")

    try:
        # Generate salt and hash password
        salt = bcrypt.gensalt()
        password_bytes = password.encode("utf-8")
        hashed_bytes = bcrypt.hashpw(password_bytes, salt)

        # Return as string for database storage
        return hashed_bytes.decode("utf-8")

    except (ValueError, UnicodeEncodeError) as e:
        raise ValueError(f"Failed to hash password: {str(e)}")


def check_password_strength(password: str) -> tuple[bool, list[str]]:
    """Check password strength against common security requirements.

    Args:
        password: The password to check

    Returns:
        Tuple of (is_strong, weakness_messages)
        where is_strong is True if password meets all requirements
    """
    weaknesses: list[str] = []

    # Length check
    if len(password) < 8:
        weaknesses.append("Password must be at least 8 characters long")

    # Character variety checks
    if not any(c.islower() for c in password):
        weaknesses.append(
            "Password must contain at least one lowercase letter")

    if not any(c.isupper() for c in password):
        weaknesses.append(
            "Password must contain at least one uppercase letter")

    if not any(c.isdigit() for c in password):
        weaknesses.append("Password must contain at least one digit")

    if not any(c in "!@#$%^&*()_+-=[]{}|;:'\",.<>?/" for c in password):
        weaknesses.append(
            "Password must contain at least one special character")

    # Common password patterns
    common_patterns = ["password", "123456", "qwerty", "admin", "welcome"]
    if password.lower() in common_patterns:
        weaknesses.append("Password is too common and easily guessable")

    is_strong = len(weaknesses) == 0
    return is_strong, weaknesses


def generate_secure_random_password(length: int = 12) -> str:
    """Generate a cryptographically secure random password.

    Args:
        length: Desired password length (default 12, max 128)

    Returns:
        Secure random password string

    Raises:
        ValueError: If length is invalid
    """
    if length < 8 or length > 128:
        raise ValueError(
            "Password length must be between 8 and 128 characters")

    # Define character sets
    lowercase = "abcdefghijklmnopqrstuvwxyz"
    uppercase = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    digits = "0123456789"
    special = "!@#$%^&*()_+-=[]{}|;:'\",.<>?/"

    # Ensure at least one character from each set
    import secrets
    all_chars = lowercase + uppercase + digits + special

    password = [
        secrets.choice(lowercase),
        secrets.choice(uppercase),
        secrets.choice(digits),
        secrets.choice(special)
    ]

    # Fill remaining length with random characters
    for _ in range(length - 4):
        password.append(secrets.choice(all_chars))

    # Shuffle the result
    secrets.SystemRandom().shuffle(password)

    return "".join(password)


def is_password_expired(
    hashed_password: str,
    max_age_days: int = 90
) -> tuple[bool, int]:
    """Check if password needs to be changed based on hash timestamp.

    Note: This requires the bcrypt hash to include timestamp information.
    This is a simplified version - in production, you might want to
    store password change timestamps separately.

    Args:
        hashed_password: The bcrypt hashed password
        max_age_days: Maximum age in days before requiring change

    Returns:
        Tuple of (is_expired, days_remaining)
    """
    # This is a placeholder implementation
    # In production, you would extract actual timestamp from hash or DB
    # For now, assume password needs regular changes
    return False, max_age_days


if __name__ == "__main__":
    # Test password utilities
    test_password = "MySecureP@ss123!"

    print("=== Password Utility Tests ===")

    # Test password strength
    is_strong, issues = check_password_strength(test_password)
    print(f"Password '{test_password}':")
    print(f"Strong: {is_strong}")
    if issues:
        print("Issues:")
        for issue in issues:
            print(f"  - {issue}")

    # Test hashing
    print(f"\nOriginal: {test_password}")
    hashed = generate_password_hash(test_password)
    print(f"Hashed: {hashed}")

    # Test verification
    is_valid = verify_password(test_password, hashed)
    print(f"Verification: {'Success' if is_valid else 'Failed'}")

    # Test invalid password
    is_invalid = verify_password("wrongpassword", hashed)
    print(
        f"Wrong password verification: {'Success' if is_invalid else 'Failed (expected)'}")

    # Generate random password
    random_pwd = generate_secure_random_password(16)
    print(f"\nGenerated secure password: {random_pwd}")

    # Check generated password strength
    is_random_strong, random_issues = check_password_strength(random_pwd)
    print(f"Generated password strength: {is_random_strong}")
