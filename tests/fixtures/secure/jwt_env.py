"""Secure: JWT secrets from environment, no fallback."""
import os
import jwt

# Correct: no fallback default
JWT_SECRET = os.environ["JWT_SECRET"]

# Correct: None fallback (not a string)
SIGNING_KEY = os.environ.get("SIGNING_KEY")

# Correct: secret from variable, not literal
def create_token(user_id):
    return jwt.encode({"user_id": user_id}, JWT_SECRET, algorithm="HS256")

# Non-secret string assignment (should NOT flag)
APP_NAME = "my-application"
DATABASE_URL = "postgresql://localhost/mydb"
