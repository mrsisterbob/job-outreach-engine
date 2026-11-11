"""HTTP to this machine only, immune to proxy settings.

requests honours HTTP_PROXY and the Windows proxy settings even for localhost
unless NO_PROXY says otherwise. A query meant for ActivityWatch (window titles,
app names) must never reach a proxy, so these helpers turn trust_env off, refuse
any non-loopback url before a socket opens, and never follow redirects.
"""
from urllib.parse import urlsplit

_MSG = "local_http only talks to this machine"
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _check_url(url):
    """Raise ValueError unless url is plain http to localhost, 127.0.0.1 or ::1."""
    if not isinstance(url, str) or not url:
        raise ValueError(_MSG)
    if any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        raise ValueError(_MSG)
    try:
        parts = urlsplit(url)
        if parts.scheme != "http" or "@" in parts.netloc:
            raise ValueError(_MSG)
        host = parts.hostname
        parts.port  # raises ValueError on a bad port
    except ValueError:
        raise ValueError(_MSG) from None
    if host not in _LOOPBACK:
        raise ValueError(_MSG)


def _request(method, url, kw):
    _check_url(url)
    if "allow_redirects" in kw or "proxies" in kw:
        raise TypeError("allow_redirects and proxies cannot be set")
    import requests
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, url, allow_redirects=False, **kw)


def local_post(url, **kw):
    return _request("POST", url, kw)


def local_get(url, **kw):
    return _request("GET", url, kw)


def local_delete(url, **kw):
    return _request("DELETE", url, kw)
