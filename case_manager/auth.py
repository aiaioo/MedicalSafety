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
import ipaddress
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
KEY_SESSION_COOKIE = "cm_key_session"  # someone using secret keys without an account
SESSION_LIFETIME = timedelta(days=14)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX_CHARS = 254
PASSWORD_MIN_CHARS = 8
PASSWORD_MAX_CHARS = 1024  # scrypt hashes any length, but there's no reason to let a request make it hash megabytes

# Endpoints reachable without signing in -- everything else redirects to
# the sign-in page (or, for /api/ routes, answers 401).
PUBLIC_ENDPOINTS = {
    "auth.signin", "auth.signup", "auth.unlock", "static",
    # The content aggregation page and the articles on it are the one part
    # of the site meant for anyone, signed in or not -- see app.py's
    # public_view/public_article/api_article_image.
    "public_view", "public_article", "api_article_image",
}

# Key holders (people using a secret key without an account) have no users
# row and no sign-in: a key session (see storage.create_key_session) stands in,
# presented as a User with the negative of its id (which the eff_user_* views
# understand -- see db/migrations/019_key_sessions.sql). They reach only the
# objects their keys cover: never the home page, collaborations, account
# settings, key management, or creating new top-level causes.
KEY_SESSION_LIFETIME = timedelta(days=7)
GUEST_BLOCKED_ENDPOINTS = {
    "index", "collaborations_view", "api_collaborations", "api_collaborations_captcha",
    "api_collaborations_seen", "api_collaboration_item", "api_collaboration_accept",
    "api_collaboration_access", "api_set_show_advanced", "api_default_cause",
    "account_view", "account_photo", "api_account_details", "api_account_photo", "api_account_password", "api_keys", "api_key_item",
}
GUEST_BLOCKED_POSTS = {
    "api_causes", "api_upload_document", "api_reports", "api_allegation_cases", "api_allegations",
}

# A page addressed by one of these query parameters is that object's URL, so
# a signed-out visitor to it is offered the key prompt if it has any keys.
# endpoint -> (query parameter, kind of object)
KEYED_PAGES = {
    "page_view": ("doc", "source"),
    "documents_view": ("report", "report"),
    "annexures_view": ("annexure", "report"),
    "allegations_view": ("allegation", "allegation"),
    "causes_view": ("cause", "cause"),
    "cases_view": ("case", "case"),
}
KEY_LENGTH = 32
KEY_DEACTIVATED_MESSAGE = (
    "This key has been temporarily deactivated by the owner. "
    "Please message the owner for access or a new key."
)
KEY_DELETED_MESSAGE = (
    "Your key has been deleted by the owner. "
    "Please message the owner for access or a new key."
)


# The "are you human?" image on the sign-up form. Each challenge's answer is
# kept server-side (storage.create_signup_captcha), single-use, and expires.
CAPTCHA_LIFETIME = timedelta(minutes=10)
CAPTCHA_LENGTH = 5
CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I -- easy to confuse

# At most this many sign-up attempts (successful or not) per client IP per
# window. Behind a reverse proxy, set TRUSTED_PROXY_HOPS so the client's real
# address is used -- see app.py.
SIGNUP_ATTEMPT_LIMIT = 10
SIGNUP_ATTEMPT_WINDOW_SECONDS = 3600

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
    show_advanced: bool = False
    is_guest: bool = False  # a key session, not a registered user
    is_admin: bool = False
    is_content_creator: bool = False

    @classmethod
    def from_row(cls, row: dict) -> User:
        return cls(
            id=row["id"], email=row["email"], password_hash=row["password_hash"],
            show_advanced=row.get("show_advanced", False),
            is_admin=row.get("is_admin", False), is_content_creator=row.get("is_content_creator", False),
        )

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


def _client_key() -> str:
    """The rate-limit key for this request: the client IP, with IPv6
    addresses collapsed to their /64 (one subscriber typically owns a whole
    /64, so per-address limits would be trivial to dodge)."""
    addr = request.remote_addr or "unknown"
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


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

    if g.user is None:
        key_token = request.cookies.get(KEY_SESSION_COOKIE)
        key_session = storage.get_key_session(_hash_token(key_token)) if key_token else None
        if key_session is not None:
            g.user = User(id=-key_session, email="", password_hash="", is_guest=True)

    if g.user is not None and g.user.is_guest and (
        request.endpoint in GUEST_BLOCKED_ENDPOINTS
        or (request.endpoint in GUEST_BLOCKED_POSTS and request.method != "GET")
    ):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Please sign in with an account to do that"}), 403
        return redirect(url_for("auth.signin"))

    if g.user is None and request.endpoint not in PUBLIC_ENDPOINTS:
        if request.path.startswith("/api/"):
            return jsonify({"error": "Please sign in first"}), 401
        next_path = request.full_path if request.query_string else request.path
        target = _keyed_target()
        if target is not None:
            return redirect(url_for("auth.unlock", kind=target[0], id=target[1], next=next_path))
        return redirect(url_for("auth.signin", next=next_path))
    return None


def _keyed_target() -> tuple[str, str] | None:
    """(kind, id) of the shared object this signed-out request is for, if
    anyone has put a key on it."""
    param_kind = KEYED_PAGES.get(request.endpoint or "")
    if param_kind is None:
        return None
    object_id = request.args.get(param_kind[0], "")
    if not object_id or len(object_id) > 200 or not storage.object_has_keys(param_kind[1], object_id):
        return None
    return param_kind[1], object_id


def render_unlock(kind: str, object_id: str, next_url: str, error: str = "", status: int = 200):
    return render_template(
        "unlock.html", kind=kind, object_id=object_id, next=next_url, error=error, captcha=new_captcha(),
    ), status


@bp.route("/unlock", methods=["GET", "POST"])
def unlock():
    """The key prompt: a secret key plus a captcha gives a guest session (or,
    for someone already in a guest session, more access) on one shared
    object -- and everything beneath it."""
    kind = request.values.get("kind", "")
    object_id = request.values.get("id", "")
    next_url = _safe_next(request.values.get("next"))
    if kind not in storage.SHARE_KINDS or not storage.object_has_keys(kind, object_id):
        return redirect(url_for("auth.signin", next=next_url))
    if g.user is not None and not g.user.is_guest:
        return redirect(next_url)  # signed-in users use their own access
    if request.method == "GET":
        return render_unlock(kind, object_id, next_url)

    if not storage.record_signup_attempt("unlock:" + _client_key(), SIGNUP_ATTEMPT_LIMIT, SIGNUP_ATTEMPT_WINDOW_SECONDS):
        return render_unlock(kind, object_id, next_url, "Too many attempts from your network. Please try again later.", 429)
    # The captcha is checked (and used up) first, so keys can't be guessed without solving one per try.
    if not captcha_passed(request.form.get("captcha_id") or "", request.form.get("captcha_answer") or ""):
        return render_unlock(kind, object_id, next_url, "The characters you typed didn't match the image. Please try the new one.", 400)
    entered = (request.form.get("key") or "").strip()
    found = storage.find_key(kind, object_id, entered) if len(entered) == KEY_LENGTH else None
    if found is None:
        return render_unlock(kind, object_id, next_url, "That key isn't valid for this page.", 403)
    if not found["active"]:
        return render_unlock(kind, object_id, next_url, KEY_DEACTIVATED_MESSAGE, 403)

    if g.user is not None:  # already holding keys: add this one to their session
        storage.redeem_key(-g.user.id, kind, entered)
        return redirect(next_url)
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + KEY_SESSION_LIFETIME
    storage.redeem_key(storage.create_key_session(_hash_token(token), expires_at), kind, entered)
    resp = redirect(next_url)
    resp.set_cookie(
        KEY_SESSION_COOKIE, token, expires=expires_at,
        httponly=True, samesite="Lax", secure=_cookie_secure(),
    )
    return resp


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    next_url = _safe_next(request.values.get("next"))
    if request.method == "GET":
        if g.user is not None and not g.user.is_guest:
            return redirect(next_url)
        return _render_signup("", "", next_url)

    if not storage.record_signup_attempt(_client_key(), SIGNUP_ATTEMPT_LIMIT, SIGNUP_ATTEMPT_WINDOW_SECONDS):
        error = "Too many sign-up attempts from your network. Please try again later."
        return _render_signup(normalize_email(request.form.get("email")), error, next_url, 429)

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
    elif not request.form.get("accept_terms"):
        error = "Please accept the terms and conditions to create an account."
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
        if g.user is not None and not g.user.is_guest:
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
    key_token = request.cookies.get(KEY_SESSION_COOKIE)
    if key_token:
        storage.delete_key_session(_hash_token(key_token))
    resp = redirect(url_for("auth.signin"))
    resp.delete_cookie(SESSION_COOKIE, httponly=True, samesite="Lax", secure=_cookie_secure())
    resp.delete_cookie(KEY_SESSION_COOKIE, httponly=True, samesite="Lax", secure=_cookie_secure())
    return resp
