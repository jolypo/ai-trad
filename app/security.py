from __future__ import annotations

import base64
import hashlib
import hmac
import os

_ITERATIONS = 310_000
_ALGO = 'sha256'


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(_ALGO, password.encode('utf-8'), salt, _ITERATIONS)
    return f'pbkdf2_{_ALGO}${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = encoded.split('$', 3)
        if scheme != f'pbkdf2_{_ALGO}':
            return False
        salt = base64.b64decode(salt_b64.encode())
        expected = base64.b64decode(digest_b64.encode())
        actual = hashlib.pbkdf2_hmac(_ALGO, password.encode('utf-8'), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False
