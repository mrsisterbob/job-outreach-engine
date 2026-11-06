"""Signed, expiring page links. Pure stdlib, no I/O, no clock, no env."""
import hashlib
import hmac

DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 3600


def make_signature(secret: str, path: str, expires_at: int) -> str:
    """URL-safe hex HMAC-SHA256 over f"{path}\\n{expires_at}"."""
    msg = f"{path}\n{expires_at}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def sign_path(secret, path, now, ttl=DEFAULT_TTL_SECONDS) -> str:
    """Return the query string 'exp=<epoch>&sig=<signature>'."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty str")
    if ttl <= 0:
        raise ValueError("ttl must be positive")
    ttl = min(int(ttl), MAX_TTL_SECONDS)
    exp = int(now) + ttl
    return f"exp={exp}&sig={make_signature(secret, path, exp)}"


def verify_link(secret, path, exp, sig, now) -> bool:
    """True only for an unexpired, untampered link for exactly this path."""
    try:
        if not isinstance(secret, str) or not secret:
            return False
        if not isinstance(path, str):
            return False
        if not isinstance(exp, str) or not exp:
            return False
        if not (exp.isascii() and exp.isdigit()):
            return False
        if not isinstance(sig, str) or not sig:
            return False
        exp_i = int(exp)
        if exp_i <= now or exp_i > now + MAX_TTL_SECONDS:
            return False
        expected = make_signature(secret, path, exp_i).encode("utf-8")
        return hmac.compare_digest(expected, sig.encode("utf-8"))
    except Exception:
        return False
