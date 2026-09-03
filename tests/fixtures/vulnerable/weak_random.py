"""Vulnerable: non-cryptographic random in auth contexts."""
import random
import time
import uuid

def generate_reset_token():
    """Bad: using random for password reset token."""
    return str(random.randint(100000, 999999))

def create_invite_code():
    """Bad: random.choice for invite code."""
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choice(chars) for _ in range(8))

def generate_session_id():
    """Bad: uuid1 is MAC/time-based, not random."""
    return str(uuid.uuid1())

def make_room_code():
    """Bad: seeded with timestamp."""
    random.seed(int(time.time()))
    return random.randint(1000, 9999)
