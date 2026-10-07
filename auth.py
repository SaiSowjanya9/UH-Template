"""Team sign-in for the hosted deployment, backed by Supabase Auth.

Passwords are checked by Supabase and never stored here. The browser receives only a
signed Flask session cookie, so no access token is exposed to page scripts, and the app
talks to Postgres with its own service key regardless of who is signed in.

Create team members in the Supabase dashboard (Authentication -> Users) with
"Auto Confirm User" enabled, and leave public sign-ups disabled.
"""
import os
import re
import time

import requests

TIMEOUT = (5, 15)
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_ATTEMPTS = 10
WINDOW = 15 * 60
# Supabase rejects anything shorter, and a stricter rule here would only mislead.
MIN_PASSWORD = 6


class AuthError(ValueError):
    pass


def settings():
    """Read the sign-in configuration, or None when the app runs without login."""
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = os.getenv("SUPABASE_ANON_KEY") or ""
    if not url or not key:
        return None
    return {"url": url, "key": key}


class Throttle:
    """Slow down password guessing. In-process, which suits the single-worker deployment."""

    def __init__(self, limit=MAX_ATTEMPTS, window=WINDOW):
        self.limit, self.window = limit, window
        self.attempts = {}

    def check(self, key):
        now = time.time()
        recent = [stamp for stamp in self.attempts.get(key, []) if now - stamp < self.window]
        self.attempts[key] = recent
        if len(recent) >= self.limit:
            raise AuthError("Too many sign-in attempts. Wait a few minutes and try again.")

    def record(self, key):
        self.attempts.setdefault(key, []).append(time.time())

    def clear(self, key):
        self.attempts.pop(key, None)


def sign_in(email, password, config=None, session=None):
    """Exchange an email and password for the signed-in user's identity."""
    config = config or settings()
    if not config:
        raise AuthError("Sign-in is not configured on this server.")
    email = (email or "").strip()
    if not EMAIL.fullmatch(email) or len(email) > 320:
        raise AuthError("Enter the email address and password for your account.")
    if not password or len(password) < MIN_PASSWORD or len(password) > 200:
        raise AuthError("Enter the email address and password for your account.")
    client = session or requests
    try:
        response = client.post(f"{config['url']}/auth/v1/token", params={"grant_type": "password"},
                               json={"email": email, "password": password},
                               headers={"apikey": config["key"], "Content-Type": "application/json"},
                               timeout=TIMEOUT)
    except requests.RequestException:
        raise AuthError("The sign-in service is unreachable. Try again shortly.") from None
    if response.status_code >= 400:
        # Deliberately the same message for unknown email and wrong password.
        raise AuthError("That email address and password do not match an account.")
    user = (response.json() or {}).get("user") or {}
    if not user.get("id"):
        raise AuthError("Sign-in failed. Contact whoever administers this workspace.")
    return {"id": user["id"], "email": user.get("email") or email}
