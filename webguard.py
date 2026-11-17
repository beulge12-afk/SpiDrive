"""
Stops other websites from using SpiDrive's local servers.

A page on any website can make your browser send requests to http://127.0.0.1:8000.
Without these checks it could delete, move or share your files (cross-site request
forgery), or read them by pointing its own domain name at 127.0.0.1 (DNS rebinding).

  - host_ok:   the Host header must be this PC (127.0.0.1, localhost, ::1), unless the
               dashboard was opened up for phones on the Wi-Fi (then a password is required).
  - origin_ok: requests that change something must come from SpiDrive's own pages.
"""
from urllib.parse import urlsplit

LOCAL_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "PROPFIND"}


def _hostname(host):
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_ok(req, any_host=False):
    return any_host or _hostname(req.host) in LOCAL_NAMES


def origin_ok(req):
    if req.method in SAFE_METHODS:
        return True
    origin = req.headers.get("Origin")
    if origin is None:
        # Browsers send Origin on cross-site POSTs; Sec-Fetch-Site covers the rest.
        return req.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")
    if origin == "null":
        return False
    return urlsplit(origin).netloc.lower() == (req.host or "").lower()
