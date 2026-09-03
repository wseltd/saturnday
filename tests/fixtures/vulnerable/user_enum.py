"""Vulnerable: differentiated login errors reveal account existence."""


def login(username, password):
    user = find_user(username)
    if not user:
        return {"error": "User not found"}, 404
    if not check_password(user, password):
        return {"error": "Wrong password"}, 401
    return {"token": create_token(user)}, 200
