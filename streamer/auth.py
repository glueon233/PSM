"""Password hashing with Argon2id (argon2-cffi) and token helpers."""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# Argon2id (argon2-cffi default type is Argon2id) with conservative params
_ph = PasswordHasher(time_cost=2, memory_cost=32768, parallelism=2)


def hash_password(password):
    return _ph.hash(password)


def verify_password(encoded, password):
    if not encoded:
        return False
    try:
        return _ph.verify(encoded, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def new_token(bytes_len=32):
    return secrets.token_urlsafe(bytes_len)


def token_hash(token):
    """Store only a hash of issued tokens so a DB leak does not reveal them."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_token(provided, stored_hash):
    if not stored_hash:
        return False
    return hmac.compare_digest(token_hash(provided), stored_hash)
