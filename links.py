"""
Share links that work on your Wi-Fi.

A link like http://192.168.1.20:8001/s/Xk3f9QaB2mPz opens a small page with the file
(photos, music and video play right there) and a Download button, on any phone or
computer on the same network. Links stop working when they expire or are removed.

This server listens on the network, so it serves nothing but these pages: no
dashboard, no file list, no API. Each link has a random 72-bit name that cannot be
guessed. It starts the first time a link is made (Windows may ask to allow it in
the firewall once) and needs SpiDrive to be running on the PC.
"""
import html
import posixpath
import socket
import time
from urllib.parse import quote

from aiohttp import web

import dav
from search import kind_of

HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex",
           "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                                      "media-src 'self'; base-uri 'none'; form-action 'none'"}

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#f5f6f8;--card:#fff;--text:#1b1f24;--muted:#6b7380;--accent:#2a8fd8;--line:#e3e6eb;color-scheme:light dark}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121417;--card:#1a1d21;--text:#e7e9ec;--muted:#8e96a1;--accent:#4ea8ea;--line:#2c3137}}}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 "Segoe UI",system-ui,-apple-system,Tahoma,sans-serif;padding:16px}}
.card{{width:min(680px,100%);background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;
box-shadow:0 6px 24px rgba(0,0,0,.08)}}
.media{{background:#000;display:grid;place-items:center}}.media img,.media video{{max-width:100%;max-height:70vh;display:block}}
.media audio{{width:100%;margin:28px 20px}}.body{{padding:18px 20px 20px}}
h1{{font-size:18px;margin:0 0 4px;word-break:break-word}}.sub{{color:var(--muted);font-size:13px}}
a.btn{{display:inline-flex;align-items:center;gap:8px;margin-top:16px;background:var(--accent);color:#fff;text-decoration:none;
padding:10px 18px;border-radius:10px;font-weight:600}}
.brand{{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12.5px;margin-top:18px}}
.brand i{{width:18px;height:18px;border-radius:5px;background:var(--accent);display:inline-block}}
.icon{{font-size:46px;padding:34px 0 10px;text-align:center}}
</style></head><body><main class="card">{media}<div class="body">
<h1 dir="auto">{name}</h1><div class="sub">{size} · link works until {until}</div>
{button}<div class="brand"><i></i>Shared from SpiDrive on this Wi-Fi</div></div></main></body></html>"""

GONE = PAGE.format(title="Link expired", media='<div class="icon">⌛</div>', name="This link has expired",
                   size="Ask for a new one", until="—", button="")


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def lan_ip():
    """This PC's address on the local network (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        ip = s.getsockname()[0]
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        s.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def make_app(drive):
    routes = web.RouteTableDef()

    def gone():
        return web.Response(status=404, text=GONE, content_type="text/html", headers=HEADERS)

    @routes.get(r"/s/{token:[\w-]{6,40}}")
    async def page(req):
        token = req.match_info["token"]
        e, link = drive.resolve_link(token)
        if e is None or e["status"] not in ("complete", "pending"):
            return gone()
        name = posixpath.basename(e["path"])
        src = f"/s/{token}/file/{quote(name)}"
        kind = kind_of(name)
        media = {"image": f'<div class="media"><img src="{src}" alt=""></div>',
                 "video": f'<div class="media"><video src="{src}" controls playsinline preload="metadata"></video></div>',
                 "audio": f'<div class="media"><audio src="{src}" controls preload="metadata"></audio></div>',
                 }.get(kind, '<div class="icon">📄</div>')
        body = PAGE.format(title=html.escape(name), media=media, name=html.escape(name), size=human(e["size"]),
                           until=time.strftime("%d %b %Y, %H:%M", time.localtime(link["expires"])),
                           button=f'<a class="btn" href="{src}?dl=1" download>⬇ Download</a>')
        return web.Response(text=body, content_type="text/html", headers=HEADERS)

    @routes.get(r"/s/{token:[\w-]{6,40}}/file/{name:.*}")
    async def file(req):
        token = req.match_info["token"]
        e, _ = drive.resolve_link(token)
        if e is None or e["status"] not in ("complete", "pending"):
            return gone()
        if req.query.get("dl"):
            drive.link_used(token)
        return await dav.send_entry(req, drive, e, "attachment" if req.query.get("dl") else "inline")

    @web.middleware
    async def headers(req, handler):
        resp = await handler(req)
        for k, v in HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    app = web.Application(middlewares=[headers])
    app.add_routes(routes)
    return app


class LinkServer:
    """Starts the share-link server the first time it is needed."""

    def __init__(self, drive, port, log=print):
        self.d, self.port, self.log = drive, int(port), log
        self.runner = None
        self.error = None

    @property
    def running(self):
        return self.runner is not None

    async def ensure(self):
        if self.runner is not None:
            return True
        runner = web.AppRunner(make_app(self.d), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "0.0.0.0", self.port).start()
        except OSError as e:
            await runner.cleanup()
            self.error = f"Port {self.port} is in use by another program ({e.strerror or e})."
            raise RuntimeError(f"Share links could not start: {self.error}") from None
        self.runner, self.error = runner, None
        self.log(f"share links: on the Wi-Fi at http://{lan_ip()}:{self.port}/s/...")
        return True

    def url(self, token):
        return f"http://{lan_ip()}:{self.port}/s/{token}"

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
