import asyncio
import datetime
import os
import sqlite3
import time

import pytest
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

import conftest
import drive_core
import keys
import links
import sync
from conftest import flush, put, read
from phone import Phone


# ---------------------------------------------------------------- upload controls

async def test_small_files_upload_in_parallel(drive):
    drive.client.delay = 0.01
    for i in range(6):
        await put(drive, f"/p/{i}.bin", os.urandom(40 * 1024), upload=False)
    await flush(drive)
    assert drive.client.max_active >= 2 and not drive.pending()


async def test_big_files_go_one_at_a_time(drive, monkeypatch):
    monkeypatch.setattr(drive_core, "SMALL_FILE", 10 * 1024)
    drive.client.delay = 0.005
    for i in range(3):
        await put(drive, f"/big/{i}.bin", os.urandom(50 * 1024), upload=False)
    await flush(drive)
    assert drive.client.max_active == 1


async def test_pause_and_resume(drive):
    drive.set_upload_controls(paused=True)
    await put(drive, "/a.txt", b"hello")                          # flush runs the worker
    assert len(drive.pending()) == 1 and drive.client.msgs == {}
    assert drive_core.Drive.__init__ and drive._meta("uploads_paused") == "1"
    drive.set_upload_controls(paused=False)
    await flush(drive)
    assert drive.pending() == [] and await read(drive, "/a.txt") == b"hello"


async def test_pausing_stops_a_running_upload_which_resumes_later(drive):
    drive.client.delay = 0.02
    await put(drive, "/slow.bin", os.urandom(200 * 1024), upload=False)
    worker = asyncio.create_task(drive.upload_worker(log=lambda m: None))
    await asyncio.sleep(0.15)
    drive.set_upload_controls(paused=True)
    await asyncio.sleep(0.1)
    assert drive.active_ids() == set() and len(drive.pending()) == 1
    parts_done = len(drive.client.msgs)
    drive.client.delay = 0
    drive.set_upload_controls(paused=False)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not drive.pending():
            break
    worker.cancel()
    assert not drive.pending() and parts_done >= 1
    assert len(drive.client.msgs) == 4                            # 200 KB / 64 KB parts, none sent twice


async def test_speed_limit(drive):
    drive.set_upload_controls(limit=400 * 1024)
    t0 = time.monotonic()
    await put(drive, "/l.bin", os.urandom(400 * 1024))
    assert time.monotonic() - t0 >= 0.6
    assert drive._meta("upload_limit") == str(400 * 1024)


# ---------------------------------------------------------------- duplicates

async def test_same_content_uploads_instantly(drive):
    data = os.urandom(150 * 1024)
    await put(drive, "/a.bin", data)
    n = len(drive.client.msgs)
    await put(drive, "/copy/b.bin", data)
    assert len(drive.client.msgs) == n                           # nothing uploaded
    assert await read(drive, "/copy/b.bin") == data
    await put(drive, "/a.bin", data, mtime=time.time() + 50)      # unchanged content saved again
    assert drive.versions("/a.bin") == [] and len(drive.client.msgs) == n
    await drive.delete("/a.bin")
    await drive.empty_trash()                                     # the copy still uses the messages
    assert await read(drive, "/copy/b.bin") == data


async def test_no_reuse_across_encryption_settings(drive, monkeypatch):
    data = os.urandom(10 * 1024)
    await put(drive, "/plain.bin", data)
    monkeypatch.setattr(conftest.cfg, "ENCRYPT", True)
    monkeypatch.setattr(conftest.cfg, "PASSPHRASE", "long enough pass")
    n = len(drive.client.msgs)
    await put(drive, "/secret.bin", data)
    assert len(drive.client.msgs) == n + 1 and drive.get_entry("/secret.bin")["encrypted"]


async def test_duplicate_finder(drive, monkeypatch):
    data = os.urandom(5000)
    await put(drive, "/x/one.bin", data)
    await put(drive, "/y/two.bin", data)                          # deduplicated: stored once
    async def none(*a):
        return None
    monkeypatch.setattr(drive, "_find_duplicate", none)
    await put(drive, "/z/three.bin", data)                        # uploaded separately
    await put(drive, "/z/other.bin", os.urandom(5000))
    [g] = drive.duplicates()
    assert sorted(f["path"] for f in g["files"]) == ["/x/one.bin", "/y/two.bin", "/z/three.bin"]
    assert g["stored"] == 2 and g["extra_bytes"] == 5000


# ---------------------------------------------------------------- Telegram inbox

async def test_inbox_is_organized(drive, monkeypatch):
    when = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=datetime.timezone.utc)
    c = drive.client
    c.add(b"jpg", media=MessageMediaPhoto(), date=when)
    c.add(b"ogg", media=MessageMediaDocument(), name=None, voice=object(), date=when)
    c.add(b"pdf", media=MessageMediaDocument(), name="bill.pdf", date=when)
    c.add(b"mp4", media=MessageMediaDocument(), name="clip.mp4", date=when)
    c.add(b"zzz", media=MessageMediaDocument(), name="thing.xyz", date=when)
    got = await drive.import_new_messages()
    folders = sorted(p.rsplit("/", 1)[0] for p in got)
    assert folders == ["/Telegram Inbox/Docs", "/Telegram Inbox/Other", "/Telegram Inbox/Photos/2026-11",
                       "/Telegram Inbox/Videos/2026-11", "/Telegram Inbox/Voice"]
    assert any(p.startswith("/Telegram Inbox/Voice/voice_") for p in got)
    monkeypatch.setattr(conftest.cfg, "INBOX_ORGANIZE", False, raising=False)
    c.add(b"pdf2", media=MessageMediaDocument(), name="late.pdf", date=when)
    assert await drive.import_new_messages() == ["/Telegram Inbox/late.pdf"]


# ---------------------------------------------------------------- keys and backups

@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(conftest.cfg, "ENCRYPT", True)
    monkeypatch.setattr(conftest.cfg, "PASSPHRASE", "first passphrase")


def reopen(monkeypatch, passphrase):
    monkeypatch.setattr(conftest.cfg, "PASSPHRASE", passphrase)
    d = drive_core.Drive()
    d.client, d.channel = conftest.FakeClient(), "channel"
    return d


async def test_change_passphrase_without_reencrypting(drive, secret, monkeypatch):
    await put(drive, "/s.txt", b"top secret" * 1000)
    msgs = dict(drive.client.msgs)
    with pytest.raises(ValueError):
        drive.change_passphrase("second passphrase", current="wrong one")
    drive.change_passphrase("second passphrase", current="first passphrase")
    assert drive.client.msgs == msgs                              # nothing re-uploaded
    old = reopen(monkeypatch, "first passphrase")
    with pytest.raises(RuntimeError):
        old._get_key()
    new = reopen(monkeypatch, "second passphrase")
    new.client = drive.client
    assert await read(new, "/s.txt") == b"top secret" * 1000


async def test_recovery_key_resets_a_forgotten_passphrase(drive, secret, monkeypatch):
    await put(drive, "/s.txt", b"x" * 100)
    rk = drive.create_recovery_key()
    assert keys.looks_like_recovery_key(rk.lower().replace("-", " "))
    with pytest.raises(ValueError):
        drive.change_passphrase("brand new pass", recovery_key=keys.new_recovery_key())
    drive.change_passphrase("brand new pass", recovery_key=rk)
    new = reopen(monkeypatch, "brand new pass")
    new.client = drive.client
    assert await read(new, "/s.txt") == b"x" * 100


async def test_index_backup_is_encrypted_and_only_recent_ones_kept(drive, secret, monkeypatch, tmp_path):
    monkeypatch.setattr(drive_core, "BACKUP_KEEP", 3)
    await put(drive, "/a/secret name.txt", b"1")
    rk = drive.create_recovery_key()
    for _ in range(5):
        await drive.backup_index()
    backups = [m for m in drive.client.msgs.values() if (m.message or "").startswith("#index_backup")]
    assert len(backups) == 3 and all(m.message.endswith("encrypted") for m in backups)
    blob = backups[-1].media.data
    assert b"secret name" not in blob and keys.is_encrypted_backup(blob)
    for secret_text in ("first passphrase", rk):
        data, _, how = keys.decrypt_backup(blob, secret_text)
        f = tmp_path / f"{how}.db"
        f.write_bytes(data)
        assert sqlite3.connect(f).execute("SELECT path FROM files WHERE path LIKE '/a/%'").fetchone()[0] == \
            "/a/secret name.txt"
    with pytest.raises(keys.WrongKey):
        keys.decrypt_backup(blob, "nope nope nope")


async def test_backup_without_passphrase_stays_plain(drive):
    await drive.backup_index()
    [m] = drive.client.msgs.values()
    assert m.media.data.startswith(b"SQLite format 3") and drive._meta("last_backup_encrypted") == "0"


# ---------------------------------------------------------------- drive health

async def test_health_check_finds_damaged_and_missing_parts(drive):
    await put(drive, "/ok.bin", os.urandom(100 * 1024))
    await put(drive, "/bad.bin", os.urandom(10 * 1024))
    await put(drive, "/gone.bin", os.urandom(10 * 1024))
    assert drive.health()["status"] == "not checked yet"
    assert await drive.health_check(log=lambda m: None) == []
    h = drive.health()
    assert h["status"] == "good" and h["percent"] == 100.0
    bad = drive._chunks(drive.get_file("/bad.bin")["id"])[0]["msg_id"]
    drive.client.msgs[bad].media.data = b"Z" * (10 * 1024)
    drive.client.msgs.pop(drive._chunks(drive.get_file("/gone.bin")["id"])[0]["msg_id"])
    drive._media_cache.clear()
    drive.db.execute("DELETE FROM health")
    problems = await drive.health_check(log=lambda m: None)
    assert len(problems) == 2
    h = drive.health()
    assert h["status"] == "problem" and {p["path"] for p in h["problems"]} == {"/bad.bin", "/gone.bin"}


async def test_health_reads_telegram_not_the_kept_copy(drive):
    await put(drive, "/k.bin", b"k" * 5000)
    drive.set_keep(["/k.bin"])
    await drive._keep_once()
    drive.client.msgs.clear()
    drive._media_cache.clear()
    assert len(await drive.health_check(log=lambda m: None)) == 1


# ---------------------------------------------------------------- share links

async def test_share_links(drive, aiohttp_client):
    await put(drive, "/pics/sunset.jpg", b"jpegdata")
    token, expires = drive.create_link("/pics/sunset.jpg", 1)
    assert expires > time.time() + 3000
    c = await aiohttp_client(links.make_app(drive))
    page = await c.get(f"/s/{token}")
    text = await page.text()
    assert page.status == 200 and "sunset.jpg" in text and "<img" in text
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]
    r = await c.get(f"/s/{token}/file/sunset.jpg?dl=1")
    assert await r.read() == b"jpegdata" and "attachment" in r.headers["Content-Disposition"]
    assert drive.links()[0]["downloads"] == 1
    drive.move("/pics", "/photos")                                 # links follow renames
    assert (await c.get(f"/s/{token}")).status == 200
    assert (await c.get("/s/doesnotexist")).status == 404
    assert (await c.get("/api/list")).status == 404                # nothing else is served
    drive.revoke_link(token)
    assert (await c.get(f"/s/{token}")).status == 404
    t2, _ = drive.create_link("/photos/sunset.jpg", 1)
    drive.db.execute("UPDATE links SET expires=?", (time.time() - 1,))
    assert (await c.get(f"/s/{t2}")).status == 404


# ---------------------------------------------------------------- phone commands

async def test_phone_commands(drive):
    sent = []

    async def send(path):
        sent.append(path)
    phone = Phone(drive, send, log=lambda m: None)
    await put(drive, "/Docs/invoice 2026.pdf", b"pdf")
    await put(drive, "/Docs/invoice 2025.pdf", b"pdf2")
    await put(drive, "/Music/song.mp3", b"mp3")
    assert "/find" in await phone.handle("/help")
    out = await phone.handle("/find invoice")
    assert "2 found" in out and "1. invoice 2025.pdf" in out
    assert await phone.handle("/get 2") is None and sent == ["/Docs/invoice 2026.pdf"]
    assert "Which one" in await phone.handle("/get invoice")
    await phone.handle("/get song")
    assert sent[-1] == "/Music/song.mp3"
    assert "Keeping song.mp3" in await phone.handle("/keep /Music/song.mp3")
    assert drive.get_entry("/Music/song.mp3")["keep"]
    assert "3 files" in await phone.handle("/stats")
    assert "Unknown command" in await phone.handle("/nope")
    assert await phone.handle("hello, not a command") is None
    assert not (await phone.handle("/find type:video")).startswith("/")   # replies never look like commands


# ---------------------------------------------------------------- backup-only folders

async def test_backup_only_folder(drive, tmp_path, monkeypatch):
    local = tmp_path / "Documents"
    (local / "sub").mkdir(parents=True)
    old = time.time() - 60
    for name, data in (("a.txt", b"A"), ("sub/b.txt", b"B")):
        (local / name).write_bytes(data)
        os.utime(local / name, (old, old))
    monkeypatch.setattr(conftest.cfg, "SYNC_FOLDERS",
                        {str(local): {"remote": "/Backup/Documents", "mode": "backup"}}, raising=False)
    s = sync.Syncer(drive, log=lambda m: None)
    assert s.describe()[0]["mode"] == "backup"
    await s.run_once()
    await flush(drive)
    assert await read(drive, "/Backup/Documents/sub/b.txt") == b"B"
    (local / "a.txt").unlink()                                     # deleted on the PC
    await put(drive, "/Backup/Documents/from-drive.txt", b"D")      # added on the drive
    await s.run_once()
    await flush(drive)
    assert drive.get_entry("/Backup/Documents/a.txt")               # drive copy stays
    assert not (local / "from-drive.txt").exists()                  # nothing comes back
    (local / "sub" / "b.txt").write_bytes(b"B changed")
    os.utime(local / "sub" / "b.txt", (old + 30, old + 30))
    await s.run_once()
    await flush(drive)
    assert await read(drive, "/Backup/Documents/sub/b.txt") == b"B changed"
    assert len(drive.versions("/Backup/Documents/sub/b.txt")) == 1


# ---------------------------------------------------------------- server endpoints

async def test_server_endpoints(drive, aiohttp_client, monkeypatch, unused_tcp_port):
    import server
    from aiohttp import web
    monkeypatch.setattr(conftest.cfg, "PASSPHRASE", "server side pass")
    app = web.Application(middlewares=[server.guard])
    app.add_routes(server.routes)
    app["drive"], app["hooks"], app["shares"], app["share_tasks"] = drive, {}, [], set()
    app["syncer"] = type("S", (), {"describe": lambda self: []})()
    app["links"] = links.LinkServer(drive, unused_tcp_port, log=lambda m: None)
    c = await aiohttp_client(app)
    await put(drive, "/a.txt", b"hello")
    r = await (await c.post("/api/uploads", json={"paused": True, "limit": 1024 * 1024})).json()
    assert r == {"paused": True, "limit": 1024 * 1024, "auto": False}
    s = await (await c.get("/api/stats")).json()
    assert s["uploads"]["paused"] and s["health"]["status"] == "not checked yet"
    r = await (await c.post("/api/links", json={"path": "/a.txt", "hours": 2})).json()
    assert r["url"].endswith(r["token"]) and f":{unused_tcp_port}/s/" in r["url"]
    import aiohttp
    async with aiohttp.ClientSession() as http:                    # the real listener on the network port
        async with http.get(f"http://127.0.0.1:{unused_tcp_port}/s/{r['token']}") as page:
            assert page.status == 200 and "a.txt" in await page.text()
    assert len((await (await c.get("/api/links?path=/a.txt")).json())["links"]) == 1
    rk = (await (await c.post("/api/keys/recovery", json={})).json())["recovery_key"]
    assert keys.looks_like_recovery_key(rk)
    assert (await (await c.get("/api/keys")).json())["recovery"]
    await c.post("/api/health/check", json={})
    await asyncio.sleep(0.2)
    assert (await (await c.get("/api/health")).json())["status"] == "good"
    assert (await (await c.get("/api/duplicates")).json())["groups"] == []
    await app["links"].stop()
