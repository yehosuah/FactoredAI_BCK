"""Opaque expiring sessions and salted scrypt credentials for the test simulator."""

import hashlib
import hmac
import secrets


def password_hash(password):
    salt = secrets.token_hex(16)
    hashed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return "scrypt:" + salt + ":" + hashed.hex()


def password_matches(password, stored):
    try:
        algorithm, salt, expected = stored.split(":")
        if algorithm != "scrypt":
            return False
        result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(result.hex(), expected)
    except (ValueError, TypeError):
        return False


# One valid hash per process, with the same work factor as provisioned credentials.
# Its random password is discarded; matching it never authenticates an unknown user.
DUMMY_PASSWORD_HASH = password_hash(secrets.token_urlsafe(32))


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def next_state(state, action):
    transitions = {
        ("ACTIVE", "pause"): "PAUSED",
        ("PAUSED", "reactivate"): "ACTIVE",
        ("PENDING_ACTIVATION", "activate"): "ACTIVE",
    }
    if action == "block" and state in ("ACTIVE", "PAUSED", "PENDING_ACTIVATION", "INELIGIBLE"):
        return "BLOCKED"
    if (state, action) in transitions:
        return transitions[(state, action)]
    raise ValueError("ineligible_card_state")
