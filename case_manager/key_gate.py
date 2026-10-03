"""Keeps the whole site closed until an administrator has entered the
encryption key, and serves the page where that happens.

The key lives only in memory (key_manager.py), so every start of the server
begins locked. While locked, every request except the pages below is turned
away (pages are redirected here, /api/ calls get a 503). The page asks for the
key vault admin's login, password and a captcha first, then for the key itself; the key
is remembered only if it passes key_manager's check. After logging in, the
admin can also change the password.

The key vault admin is NOT an app user (and unrelated to users.is_admin):
accounts are looked up by an email hash that needs the key, so nobody can sign
in before it is loaded. The login name is fixed (KEY_VAULT_ADMIN_USER). The
password is stored only as a hash: until it is changed, the built-in default
below applies; a changed one lives in the key_vault_admin table, which needs
no key to read. The default is public (it is in the source), so change it
after the first login.

Must be registered before auth.bp so its gate runs before sign-in handling
(which reads encrypted columns).
"""

import hmac
import secrets
import threading
import time

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

import auth
import key_manager
import storage

bp = Blueprint("key_gate", __name__)

# Reachable while locked (blueprint endpoint names, plus Flask's static route).
_OPEN_ENDPOINTS = {"key_gate.encryption_key", "key_gate.healthz", "static"}

KEY_VAULT_ADMIN_USER = "key_vault_admin"
# Hash of the built-in default password, used until the admin sets their own.
DEFAULT_PASSWORD_HASH = (
    "scrypt:32768:8:1$0YmNlyDwePWRXYG8$bd8b4eab8d260f82a0febaefec0d83e7fe075a8416ba285cd1fca200347a9140"
    "9ddfd21a7d663b122c1921ead50eba22e11cfe333aa13787d0f805194af02ca2"
)
NEW_PASSWORD_MIN_CHARS = 8
NEW_PASSWORD_MAX_CHARS = 1024

# Per-process only, which is fine: the app runs as a single process.
_serializer = URLSafeTimedSerializer(secrets.token_bytes(32), salt="encryption-key-entry")
_TOKEN_MAX_AGE_SECONDS = 600
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 900
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def _locked_out(ip: str) -> bool:
    now = time.time()
    with _failures_lock:
        recent = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW_SECONDS]
        _failures[ip] = recent
        return len(recent) >= MAX_FAILURES


def _record_failure(ip: str) -> None:
    with _failures_lock:
        _failures.setdefault(ip, []).append(time.time())


@bp.before_app_request
def require_key_in_memory():
    if key_manager.KeyManager().is_key_in_memory() or request.endpoint in _OPEN_ENDPOINTS:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "The encryption key has not been entered yet. An administrator must enter it."}), 503
    return redirect(url_for("key_gate.encryption_key"))


@bp.route("/healthz")
def healthz():
    return jsonify({"ok": True, "key_loaded": key_manager.KeyManager().is_key_in_memory()})


def _password_hash() -> str:
    return storage.get_key_vault_admin_password_hash() or DEFAULT_PASSWORD_HASH


def _credentials_ok(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username.encode(), KEY_VAULT_ADMIN_USER.encode())
    # Always check the password (even for a wrong login) so timing doesn't reveal which was wrong.
    return check_password_hash(_password_hash(), password) and user_ok


def _page(step: str, error: str = "", status: int = 200, notice: str = ""):
    token = _serializer.dumps("key-entry") if step == "key" else ""
    using_default = step == "key" and not storage.get_key_vault_admin_password_hash()
    captcha = auth.new_captcha() if step == "login" else None  # a fresh challenge every time the login form is shown
    return render_template("encryption_key.html", step=step, error=error, notice=notice, token=token,
                           using_default=using_default, min_chars=NEW_PASSWORD_MIN_CHARS, captcha=captcha), status


def _token_ok() -> bool:
    try:
        _serializer.loads(request.form.get("token", ""), max_age=_TOKEN_MAX_AGE_SECONDS)
        return True
    except (BadSignature, SignatureExpired):
        return False


@bp.route("/encryption-key", methods=["GET", "POST"])
def encryption_key():
    if key_manager.KeyManager().is_key_in_memory():
        return _page("done")  # the key can't be replaced once it is loaded
    if request.method == "GET":
        return _page("login")

    ip = request.remote_addr or "?"
    if _locked_out(ip):
        return _page("login", "Too many failed attempts. Please try again later.", 429)
    step = request.form.get("step")

    if step == "login":
        # The captcha is checked (and used up) first, so passwords can't be guessed without solving one per try.
        if not auth.captcha_passed(request.form.get("captcha_id", ""), request.form.get("captcha_answer", "")):
            _record_failure(ip)
            return _page("login", "The characters you typed didn't match the image. Please try the new one.", 400)
        if not _credentials_ok(request.form.get("username", ""), request.form.get("password", "")):
            _record_failure(ip)
            return _page("login", "Incorrect login or password.", 401)
        return _page("key")

    # The other steps need the token that a successful login produced.
    if not _token_ok():
        return _page("login", "Please log in again.", 401)

    if step == "password":
        current, new = request.form.get("current_password", ""), request.form.get("new_password", "")
        if not check_password_hash(_password_hash(), current):
            _record_failure(ip)
            return _page("key", "The current password is incorrect.", 401)
        if new != request.form.get("confirm_password", ""):
            return _page("key", "The new passwords do not match.", 400)
        if not NEW_PASSWORD_MIN_CHARS <= len(new) <= NEW_PASSWORD_MAX_CHARS:
            return _page("key", f"The new password must be at least {NEW_PASSWORD_MIN_CHARS} characters.", 400)
        storage.set_key_vault_admin_password_hash(generate_password_hash(new))
        return _page("key", notice="Password changed.")

    try:  # step == "key"
        key_manager.KeyManager().remember_key(request.form.get("key", ""))
    except key_manager.InvalidKey as exc:
        _record_failure(ip)
        return _page("key", str(exc) + ". Nothing was stored.", 400)
    return _page("done")
