"""Tricky: FastAPI auth via Depends() — must detect as authenticated."""
from fastapi import FastAPI, Depends

app = FastAPI()


def get_current_user(token: str):
    return verify_token(token)


@app.get("/api/profile")
def profile(user=Depends(get_current_user)):
    """Correct: auth via Depends() — should NOT flag."""
    return {"user": user}
