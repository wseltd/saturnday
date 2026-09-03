"""Vulnerable: logout/reset handlers without token invalidation."""


def logout(request):
    """Bad: logout without invalidating tokens."""
    return {"message": "Logged out"}


def reset_password(request):
    """Bad: password reset without invalidating old tokens."""
    new_password = request.json["new_password"]
    user = get_user(request.user_id)
    user.password = hash_password(new_password)
    user.save()
    return {"message": "Password reset"}
