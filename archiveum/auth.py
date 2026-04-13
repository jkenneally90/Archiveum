"""Authentication utilities for Public Mode admin access."""

from __future__ import annotations

import hashlib
import secrets
import string


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2 with SHA256 and a random salt.
    
    Returns a string in format: "salt:hash" where both are hex encoded.
    """
    if not password:
        return ""
    
    # Generate random salt
    salt = secrets.token_hex(32)  # 64 characters
    
    # Hash password with salt
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000  # 100k iterations
    )
    
    return f"{salt}:{key.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored hash.
    
    Args:
        password: The password to verify
        stored_hash: The stored hash in format "salt:hash"
    
    Returns:
        True if password matches, False otherwise
    """
    if not stored_hash or ":" not in stored_hash:
        return False
    
    try:
        salt, expected_hash = stored_hash.split(":", 1)
        
        # Hash the provided password with the same salt
        key = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            salt.encode('utf-8'),
            100000
        )
        
        # Compare using constant-time comparison to prevent timing attacks
        return secrets.compare_digest(key.hex(), expected_hash)
    except Exception:
        return False


def generate_temp_password(length: int = 12) -> str:
    """Generate a secure temporary password.
    
    Returns a random password with letters and digits.
    """
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))
