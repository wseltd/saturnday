"""Secure: structured security event logging."""
import logging

logger = logging.getLogger(__name__)


def login(request):
    user = authenticate(request.json["username"], request.json["password"])
    if not user:
        logger.warning("auth_failure", extra={
            "event_type": "login_failed",
            "actor": request.json["username"],
            "outcome": "failure",
            "reason": "invalid_credentials",
            "source_ip": request.remote_addr,
            "correlation_id": request.request_id,
        })
        return {"error": "Invalid credentials"}, 401
    return {"token": create_token(user)}, 200
