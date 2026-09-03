"""Vulnerable: JWT created without expiration."""
import jwt


def create_token(user_id):
    """Bad: no exp claim."""
    return jwt.encode({"user_id": user_id}, SECRET_KEY)
