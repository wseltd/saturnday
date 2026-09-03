"""Vulnerable: JWT decode without pinned algorithms."""
import jwt


def verify_token(token):
    """Bad: no algorithms parameter — attacker can choose algorithm."""
    payload = jwt.decode(token, "secret-key")
    return payload


def verify_token_none_allowed(token):
    """Bad: alg=none explicitly allowed."""
    payload = jwt.decode(
        token, "secret-key", algorithms=["HS256", "none"]
    )
    return payload
