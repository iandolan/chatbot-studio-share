import base64
import hashlib
import hmac
import secrets
import time
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from .config import Config


# ---------- admin auth ----------

def admin_enabled():
    return bool(Config.ADMIN_PASSWORD)


def is_admin():
    return not admin_enabled() or session.get("admin") is True


def login(password):
    if admin_enabled() and secrets.compare_digest(password, Config.ADMIN_PASSWORD):
        session.clear()
        session["admin"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True
        return True
    return not admin_enabled()


def logout():
    session.pop("admin", None)


def require_admin_page(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return redirect(url_for("auth.login_page", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def require_admin_api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return jsonify({"error": "Not authenticated"}), 401
        if admin_enabled() and request.method not in ("GET", "HEAD", "OPTIONS"):
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not expected or not hmac.compare_digest(expected, supplied):
                return jsonify({"error": "Session check failed. Reload the page and try again."}), 403
        return fn(*args, **kwargs)
    return wrapper


# Secret storage (per-bot API keys) ----------

def _fernet():
    from cryptography.fernet import Fernet
    digest = hashlib.sha256(Config.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext):
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext):
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def key_hint(plaintext):
    if len(plaintext) < 8:
        return "set"
    return f"{plaintext[:3]}…{plaintext[-4:]}"


def new_embed_key():
    return "pk_" + secrets.token_urlsafe(12)


# ---------- shared passphrase gate (internal-KB bots) ----------
#
# Passphrases are stored hashed with PBKDF2-HMAC-SHA256 and a per-hash random
# salt (never recoverable, verified in constant time). A correct passphrase
# yields a stable per-bot unlock token (an HMAC over the embed key) that the
# widget stores and replays, so we don't re-hash on every message.

_PBKDF2_ITERATIONS = 120_000


def hash_passphrase(passphrase):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_passphrase(passphrase, stored):
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", passphrase.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def unlock_token(embed_key, access_hash=""):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(Config.SECRET_KEY, salt="kb-access-v2").dumps({
        "key": embed_key, "version": hashlib.sha256(access_hash.encode()).hexdigest(),
    })


def valid_unlock_token(embed_key, token, access_hash=""):
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    try:
        value = URLSafeTimedSerializer(Config.SECRET_KEY, salt="kb-access-v2").loads(
            str(token), max_age=Config.ACCESS_TOKEN_TTL)
        return value == {"key": embed_key,
                         "version": hashlib.sha256(access_hash.encode()).hexdigest()}
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False


def rate_limited(key, limit, window_seconds):
    """Shared fixed-window counters; all workers reserve against the same DB."""
    from . import db
    now = time.time()
    key = hashlib.sha256(key.encode()).hexdigest()
    with db.transaction() as conn:
        conn.execute("DELETE FROM rate_limits WHERE expires <= ?", (now,))
        row = conn.execute(
            "INSERT INTO rate_limits(key,count,expires) VALUES (?,1,?) "
            "ON CONFLICT(key) DO UPDATE SET count=count+1 RETURNING count",
            (key, now + window_seconds)).fetchone()
    return row["count"] > limit


def client_ip():
    # Forwarded headers are interpreted only by the configured ProxyFix boundary.
    return request.remote_addr or "unknown"
