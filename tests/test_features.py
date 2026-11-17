import io
import time
import zipfile

import pytest
from PIL import Image

import conftest
import search
import server
import zipper
from conftest import put, read


def jpeg(w=800, h=600, color=(200, 30, 30)):
    out = io.BytesIO()
    img = Image.new("RGB", (w, h), color)
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2024:05:06 07:08:09"
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


# ---------------------------------------------------------------- keep on this PC

async def test_keep_downloads_a_local_copy_and_free_up_removes_it(drive):
    await put(drive, "/k/a.bin", b"k" * 200000)
    assert drive.set_keep(["/k"]) == 1
    await drive._keep_once()
    e = drive.get_entry("/k/a.bin")
    assert drive.is_local(e)
    drive.client.msgs.clear()                                     # works without Telegram now
    assert await read(drive, "/k/a.bin") == b"k" * 200000
    drive.set_keep(["/k/a.bin"], False)
    await drive._keep_once()
    assert not drive.is_local(drive.get_entry("/k/a.bin"))


async def test_kept_waiting_upload_moves_into_offline_without_download(drive):
    await put(drive, "/k.bin", b"z" * 1000, upload=False)
    drive.set_keep(["/k.bin"])
    await conftest.flush(drive)
    assert drive.is_local(drive.get_entry("/k.bin"))


# ---------------------------------------------------------------- previews and photos

async def test_preview_made_at_upload_with_photo_date(drive):
    await put(drive, "/p/a.jpg", jpeg())
    e = drive.get_entry("/p/a.jpg")
    drive.client.msgs.clear()                                     # no download needed
    data = await drive.thumbnail(e)
    assert Image.open(io.BytesIO(data)).size == (360, 270)
    [photo] = drive.photos()
    assert time.localtime(photo["date"])[:3] == (2024, 5, 6)


async def test_preview_made_later_for_older_photos(drive):
    await put(drive, "/b.png", b"")                               # placeholder to get ids moving
    await put(drive, "/old.jpg", jpeg(100, 50))
    drive.db.execute("DELETE FROM th.thumbs")
    data = await drive.thumbnail(drive.get_entry("/old.jpg"))
    assert Image.open(io.BytesIO(data)).size == (100, 50)
    assert await drive.thumbnail(drive.get_entry("/b.png")) is None   # broken image: remembered as failed


# ---------------------------------------------------------------- search

def test_persian_variants_and_digits():
    assert search.Query("كتاب ۱۲").match("/x/کتاب_12.pdf", 1, 0)
    assert search.Query("مي خواهم").match("/می‌خواهم.mp3", 1, 0)
    assert search.Query("میخواهم").match("/می خواهم.mp3", 1, 0)
    assert not search.Query("کتاب").match("/x/دفتر.pdf", 1, 0)


def test_filters():
    t = time.mktime((2026, 2, 10, 12, 0, 0, 0, 0, -1))
    q = search.Query("type:video size:>1gb after:2026-01")
    assert q.match("/m/film.mkv", 2 * 1024 ** 3, t)
    assert not q.match("/m/film.mkv", 500 * 1024 ** 2, t)
    assert not q.match("/m/film.mp3", 2 * 1024 ** 3, t)
    assert not search.Query("before:2026-02").match("/a", 1, t)
    assert search.Query("date:2026-02").match("/a", 1, t)
    assert search.Query("size:1mb..10mb ext:pdf in:docs").match("/My Docs/a.PDF", 5 * 1024 ** 2, t)
    assert search.Query("after:1404-11 before:1404-12").match("/a", 1, t)     # Bahman 1404
    assert search.Query("size:junk").errors


async def test_drive_search_uses_filters(drive):
    await put(drive, "/a/گزارش ۱.pdf", b"x" * 10)
    await put(drive, "/a/clip.mp4", b"x" * 5000)
    assert [e["path"] for e in drive.search("گزارش 1")[0]] == ["/a/گزارش ۱.pdf"]
    assert [e["path"] for e in drive.search("type:video")[0]] == ["/a/clip.mp4"]
    assert [e["path"] for e in drive.search("size:>1kb")[0]] == ["/a/clip.mp4"]


# ---------------------------------------------------------------- zip

async def zip_bytes(d, paths):
    items, _ = server.zip_items(d, paths)
    out = bytearray()

    async def write(b):
        out.extend(b)
    await zipper.stream(write, d, items)
    return zipfile.ZipFile(io.BytesIO(bytes(out)))


async def test_zip_of_folder_and_files(drive, monkeypatch):
    monkeypatch.setattr(conftest.cfg, "ENCRYPT", True)
    monkeypatch.setattr(conftest.cfg, "PASSPHRASE", "correct horse")
    await put(drive, "/t/a.txt", b"A" * 150000)                   # encrypted, several parts
    monkeypatch.setattr(conftest.cfg, "ENCRYPT", False)
    await put(drive, "/t/s/ü.txt", b"unicode")
    await put(drive, "/t/wait.txt", b"pending", upload=False)
    drive.mkdir("/t/empty")
    z = await zip_bytes(drive, ["/t"])
    assert z.testzip() is None
    assert sorted(z.namelist()) == ["t/a.txt", "t/empty/", "t/s/ü.txt", "t/wait.txt"]
    assert z.read("t/a.txt") == b"A" * 150000 and z.read("t/wait.txt") == b"pending"
    z = await zip_bytes(drive, ["/t/a.txt", "/t/s"])
    assert sorted(z.namelist()) == ["a.txt", "s/ü.txt"]


# ---------------------------------------------------------------- web server

@pytest.fixture
async def client(drive, aiohttp_client):
    from aiohttp import web
    app = web.Application(middlewares=[server.guard])
    app.add_routes(server.routes)
    app["drive"], app["hooks"], app["shares"], app["share_tasks"] = drive, {}, [], set()
    app["syncer"] = type("S", (), {"describe": lambda self: []})()
    return await aiohttp_client(app)


async def test_other_websites_are_blocked(client, drive):
    r = await client.post("/api/mkdir", data='{"path":"/evil"}',
                          headers={"Content-Type": "text/plain", "Origin": "https://evil.example"})
    assert r.status == 403 and not drive.is_dir("/evil")
    r = await client.get("/api/list?path=/", headers={"Host": "rebind.evil.example:8000"})
    assert r.status == 421
    r = await client.post("/api/mkdir", json={"path": "/ok"}, headers={"Origin": f"http://{client.host}:{client.port}"})
    assert r.status == 200


async def test_api_trash_versions_batch_zip_photos(client, drive):
    await put(drive, "/x/a.jpg", jpeg())
    await put(drive, "/x/b.txt", b"1")
    await put(drive, "/x/b.txt", b"2")
    v = await (await client.get("/api/versions?path=/x/b.txt")).json()
    assert len(v["versions"]) == 1
    r = await (await client.post("/api/batch", json={"action": "move", "paths": ["/x/a.jpg"], "dest": "/y"})).json()
    assert r == {"done": 1, "errors": []}
    r = await (await client.post("/api/batch", json={"action": "delete", "paths": ["/x"]})).json()
    assert r["done"] == 1
    t = await (await client.get("/api/trash")).json()
    assert [i["path"] for i in t["items"]] == ["/x"]
    await client.post("/api/trash/restore", json={"ids": [t["items"][0]["id"]]})
    assert drive.get_entry("/x/b.txt")
    r = await client.post("/api/zip", data={"paths": '["/x", "/y/a.jpg"]'})
    assert r.status == 200 and "attachment" in r.headers["Content-Disposition"]
    z = zipfile.ZipFile(io.BytesIO(await r.read()))
    assert sorted(z.namelist()) == ["x/b.txt", "y/a.jpg"]
    photos = await (await client.get("/api/photos")).json()
    [p] = photos["files"]
    r = await client.get(f"/t/{p['id']}")
    assert r.status == 200 and r.headers["Content-Type"] == "image/jpeg"
    s = await (await client.get("/api/search?q=type:image")).json()
    assert [f["path"] for f in s["files"]] == ["/y/a.jpg"]


async def test_dav_move_into_itself_loses_nothing(drive, aiohttp_client):
    import dav
    await put(drive, "/a/b/keep.txt", b"important")
    c = await aiohttp_client(dav.make_app(drive))
    r = await c.request("MOVE", "/a", headers={"Destination": "/a/b", "Overwrite": "T"})
    assert r.status == 409 and drive.get_entry("/a/b/keep.txt")


async def test_dav_save_over_keeps_a_version(drive, aiohttp_client):
    import dav
    await put(drive, "/doc.txt", b"old")
    await put(drive, "/~tmp123.txt", b"new")
    c = await aiohttp_client(dav.make_app(drive))
    r = await c.request("MOVE", "/~tmp123.txt", headers={"Destination": "/doc.txt"})
    assert r.status == 204
    assert await read(drive, "/doc.txt") == b"new"
    assert len(drive.versions("/doc.txt")) == 1 and drive.trash_list() == []
