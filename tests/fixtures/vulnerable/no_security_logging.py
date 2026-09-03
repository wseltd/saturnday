"""Vulnerable: auth handlers with bare logger.info only."""
import logging

logger = logging.getLogger(__name__)


def login(request):
    user = authenticate(request.json["username"], request.json["password"])
    if not user:
        logger.info("login failed")
        return {"error": "Invalid credentials"}, 401
    return {"token": create_token(user)}, 200
