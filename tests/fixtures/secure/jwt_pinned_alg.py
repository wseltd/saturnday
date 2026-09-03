"""Secure: JWT decode with pinned algorithms and claim validation."""
import jwt
import os

SECRET = os.environ["JWT_SECRET"]

def verify_token(token):
    """Correct: algorithms pinned, no 'none'."""
    payload = jwt.decode(
        token,
        SECRET,
        algorithms=["HS256"],
        options={"verify_exp": True},
    )
    return payload
