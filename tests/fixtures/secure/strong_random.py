"""Secure: cryptographic random for auth contexts."""
import secrets
import uuid

def generate_reset_token():
    """Correct: secrets module for reset token."""
    return secrets.token_urlsafe(32)

def create_invite_code():
    """Correct: secrets.token_hex for invite code."""
    return secrets.token_hex(8)

def generate_session_id():
    """Correct: uuid4 is cryptographically random."""
    return str(uuid.uuid4())

# Non-auth random usage (should NOT flag)
import random
def shuffle_playlist(songs):
    """OK: random for non-security context."""
    random.shuffle(songs)
    return songs
