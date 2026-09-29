"""Users, sign-up / sign-in / sign-out, and the "must be signed in" gate in
front of every other page and API route.

A signed-in browser holds a random session token in an HttpOnly cookie; the
server keeps only that token's SHA-256 in the user_sessions table (see
db/migrations/001_users_and_access.sql), so signing out -- or an expiring
session -- genuinely ends it server-side, and nothing secret has to be
configured for sessions to work. Which causes/cases/reports/sources a
signed-in user may then see or change is decided per object in app.py (see
require_role there), against the user_* association tables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from captcha.image import ImageCaptcha
from flask import Blueprint, g, jsonify, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import storage

SESSION_COOKIE = "cm_session"
SESSION_LIFETIME = timedelta(days=14)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX_CHARS = 254
PASSWORD_MIN_CHARS = 8
PASSWORD_MAX_CHARS = 1024  # scrypt hashes any length, but there's no reason to let a request make it hash megabytes

# Endpoints reachable without signing in -- everything else redirects to
# the sign-in page (or, for /api/ routes, answers 401).
PUBLIC_ENDPOINTS = {"auth.signin", "auth.signup", "static"}

# The "are you human?" image on the sign-up form. Each challenge's answer is
# kept server-side (storage.create_signup_captcha), single-use, and expires.
CAPTCHA_LIFETIME = timedelta(minutes=10)
CAPTCHA_LENGTH = 5
CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I -- easy to confuse

bp = Blueprint("auth", __name__)
_captcha_image = ImageCaptcha(width=200, height=70)


@dataclass(frozen=True)
class User:
    """A registered user: an email address plus a one-way hash of their
    password (werkzeug's salted scrypt -- the plain password is never
    stored or kept around after hashing/checking it)."""

    id: int
    email: str
    password_hash: str

    @classmethod
    def from_row(cls, row: dict) -> User:
        return cls(id=row["id"], email=row["email"], password_hash=row["password_hash"])

    @staticmethod
    def hash_password(password: str) -> str:
        return generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @classmethod
    def register(cls, email: str, password: str) -> User | None:
        """Creates and returns a new user, or None if that email is already
        registered."""
        row = storage.create_user(normalize_email(email), cls.hash_password(password))
        if row is None:
            return None
        storage.create_general_cause(row["id"])  # every user starts with a default cause
        return cls.from_row(row)

    @classmethod
    def authenticate(cls, email: str, password: str) -> User | None:
        row = storage.get_user_by_email(normalize_email(email))
        if row is None:
            # Still spend the time a real check would, so response timing
            # doesn't reveal which emails are registered.
            check_password_hash(_DUMMY_PASSWORD_HASH, password)
            return None
        user = cls.from_row(row)
        return user if user.check_password(password) else None


_DUMMY_PASSWORD_HASH = User.hash_password(secrets.token_urlsafe(16))


def new_captcha() -> dict:
    """Creates a fresh challenge: {"id", "image"} where image is a data: URI
    of a PNG showing the (case-insensitive) answer."""
    answer = "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(CAPTCHA_LENGTH))
    captcha_id = secrets.token_urlsafe(16)
    storage.create_signup_captcha(captcha_id, answer, datetime.now(timezone.utc) + CAPTCHA_LIFETIME)
    png = _captcha_image.generate(answer).getvalue()
    return {"id": captcha_id, "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}


def captcha_passed(captcha_id: str, attempt: str) -> bool:
    """Checks (and uses up) a challenge. Wrong, expired and already-used
    all come back False."""
    answer = storage.take_signup_captcha_answer(captcha_id)
    if answer is None:
        return False
    return hmac.compare_digest(answer, (attempt or "").strip().upper())


def _render_signup(email: str, error: str, next_url: str, status: int = 200):
    return render_template(
        "auth.html", mode="signup", email=email, error=error, next=next_url, captcha=new_captcha(),
    ), status


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cookie_secure() -> bool:
    # Behind a TLS-terminating proxy, request.is_secure is False even though
    # the browser is on https -- SESSION_COOKIE_SECURE=1 forces the Secure
    # flag there (see deploy/case-manager.env.example).
    return request.is_secure or os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def _start_session(user: User, next_url: str):
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_LIFETIME
    storage.create_session(user.id, _hash_token(token), expires_at)
    resp = redirect(next_url)
    resp.set_cookie(
        SESSION_COOKIE, token, expires=expires_at,
        httponly=True, samesite="Lax", secure=_cookie_secure(),
    )
    return resp


def _safe_next(raw: str | None) -> str:
    """Only ever redirect back to a path on this site, never to an
    attacker-supplied absolute URL."""
    if raw and raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
        parts = urlsplit(raw)
        if not parts.scheme and not parts.netloc:
            return raw
    return url_for("index")


@bp.before_app_request
def load_user_and_require_signin():
    g.user = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = storage.get_session_user(_hash_token(token))
        if row is not None:
            g.user = User.from_row(row)

    if g.user is None and request.endpoint not in PUBLIC_ENDPOINTS:
        if request.path.startswith("/api/"):
            return jsonify({"error": "Please sign in first"}), 401
        next_path = request.full_path if request.query_string else request.path
        return redirect(url_for("auth.signin", next=next_path))
    return None


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    next_url = _safe_next(request.values.get("next"))
    if request.method == "GET":
        if g.user is not None:
            return redirect(next_url)
        return _render_signup("", "", next_url)

    email = normalize_email(request.form.get("email"))
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""
    captcha_ok = captcha_passed(request.form.get("captcha_id") or "", request.form.get("captcha_answer") or "")

    error = ""
    if not EMAIL_RE.match(email) or len(email) > EMAIL_MAX_CHARS:
        error = "Please enter a valid email address."
    elif len(password) < PASSWORD_MIN_CHARS:
        error = f"Your password must be at least {PASSWORD_MIN_CHARS} characters long."
    elif len(password) > PASSWORD_MAX_CHARS:
        error = "That password is too long."
    elif password != confirm:
        error = "The two passwords don't match."
    elif not captcha_ok:
        error = "The characters you typed didn't match the image. Please try the new one."
    if error:
        return _render_signup(email, error, next_url, 400)

    user = User.register(email, password)
    if user is None:
        error = "An account with that email already exists. Sign in instead."
        return _render_signup(email, error, next_url, 409)
    return _start_session(user, next_url)


@bp.route("/signin", methods=["GET", "POST"])
def signin():
    next_url = _safe_next(request.values.get("next"))
    if request.method == "GET":
        if g.user is not None:
            return redirect(next_url)
        return render_template("auth.html", mode="signin", email="", error="", next=next_url)

    email = normalize_email(request.form.get("email"))
    password = (request.form.get("password") or "")[:PASSWORD_MAX_CHARS]
    user = User.authenticate(email, password)
    if user is None:
        error = "Incorrect email or password."
        return render_template("auth.html", mode="signin", email=email, error=error, next=next_url), 401
    return _start_session(user, next_url)


@bp.route("/signout", methods=["POST"])
def signout():
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        storage.delete_session(_hash_token(token))
    resp = redirect(url_for("auth.signin"))
    resp.delete_cookie(SESSION_COOKIE, httponly=True, samesite="Lax", secure=_cookie_secure())
    return resp
