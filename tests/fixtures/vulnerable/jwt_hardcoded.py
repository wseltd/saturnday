"""Vulnerable: hardcoded JWT secrets."""
import os
import jwt

# Hardcoded secret assignment
JWT_SECRET = "supersecretkey123"
SECRET_KEY = "changeme"
SIGNING_KEY = "my-jwt-signing-key"

# Fallback default in os.environ.get
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "fallback-secret-value")

# JWT encode with literal secret
def create_token(user_id):
    return jwt.encode({"user_id": user_id}, "hardcoded-secret", algorithm="HS256")
