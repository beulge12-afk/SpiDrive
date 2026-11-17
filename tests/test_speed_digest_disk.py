import asyncio
import collections
import datetime
import logging
import os
import time

import adaptive
import conftest
import digest
import drive_core
from conftest import flush, put, read

Usage = collections.namedtuple("Usage", "total used free")


# ---------------------------------------------------------------- adaptive speed

def test_slows_down_on_flood_and_recovers():
    a = adaptive.AdaptiveSpeed(3)
    a.on_bytes(4 * 1024 * 1024 * 20, now=100)                       # 4 MB/s over the last 20 s
    a.on_flood(10, "SendMediaRequest", now=100)
    assert a.rate == 2 * 1024 * 1024 and a.parallel == 2
    a.on_flood(5, "SendMediaRequest", now=101)
    assert a.rate == 1024 * 1024 and a.parallel == 1
    a.tick(now=150)                                                 # not a calm minute yet
    assert a.rate == 1024 * 1024
    a.tick(now=170)
    assert a.rate == 1280 * 1024 and a.parallel == 2
    for i in range(40):
        a.tick(now=170 + 61 * (i + 1))
    assert a.rate is None and a.parallel == 3                       # back to no limit
    assert a.status(now=170)["floods_today"] == 2


async def test_telethon_flood_log_reaches_the_drive(drive):
    logging.getLogger("telethon.client.users").info(
        "Sleeping%s for %ds (%s) on %s flood wait", "", 17, datetime.timedelta(seconds=17), "SaveFilePartRequest")
    assert drive.adaptive.floods[-1][1:] == (17, "SaveFilePartRequest")
    assert drive.speed_limit() > 0 and drive.parallel_uploads() == drive_core.UPLOAD_PARALLEL - 1
    assert drive.db.execute("SELECT detail FROM events WHERE kind='flood'").fetchone()[0] == "17s on SaveFilePartRequest"


async def test_automatic_speed_limits_uploads_and_manual_overrides(drive):
    drive.adaptive.rate = 300 * 1024
    t0 = time.monotonic()
    await put(drive, "/a.bin", os.urandom(300 * 1024))
    assert time.monotonic() - t0 >= 0.6
    drive.set_upload_controls(limit=0)                              # manual: no limit
    assert not drive.auto_speed and drive.speed_limit() == 0
    t0 = time.monotonic()
    await put(drive, "/b.bin", os.urandom(300 * 1024))
    assert time.monotonic() - t0 < 0.5
    drive.set_upload_controls(limit="auto")
    assert drive.auto_speed and drive._meta("upload_auto") == "1"


async def test_long_flood_wait_error_backs_off(drive):
    class FloodWaitError(Exception):
        seconds = 120
    real = drive.client.upload_file

    async def flood(*a, **kw):
        raise FloodWaitError("A wait of 120 seconds is required")
    drive.client.upload_file = flood
    await put(drive, "/f.bin", b"x" * 1000, upload=False)
    worker = asyncio.create_task(drive.upload_worker(log=lambda m: None))
    await asyncio.sleep(0.2)
    worker.cancel()
    pid = drive.pending()[0]["id"]
    assert drive._retry_at[pid] - time.time() > 100 and drive.adaptive.floods[-1][1] == 120
    assert drive.db.execute("SELECT COUNT(*) FROM events WHERE kind='upload_failed'").fetchone()[0] == 1
    drive.client.upload_file = real


# ---------------------------------------------------------------- weekly digest

async def test_weekly_digest(drive, monkeypatch):
    for i in range(3):
        await put(drive, f"/w/{i}.bin", b"x" * 1024)
    await put(drive, "/old.bin", b"o")
    await drive.delete("/old.bin")
    drive.db.execute("UPDATE trash_batches SET deleted=?", (time.time() - 26 * 86400,))   # expires in 4 days
    await put(drive, "/stuck.pdf", b"s", upload=False)
    drive.event("upload_failed", "/stuck.pdf", "network")
    drive.event("flood", "", "17s on SendMediaRequest")
    drive.event("freed", "", "3 file(s), 1048576 bytes, this PC was low on space")
    text = digest.weekly_text(drive)
    assert text.startswith("📅 SpiDrive, ")
    assert "• 3 files, 3.0 KB added" in text           # the deleted one does not count
    assert "1 failed upload (still retrying): stuck.pdf" in text
    assert "1 file in the trash expires this week" in text
    assert "slow down 1 time" in text and "Freed 1.0 MB" in text and "Health not checked yet" in text

    assert await digest.maybe_send(drive) is None                     # first run: starts the week
    assert await digest.maybe_send(drive, now=time.time() + 3 * 86400) is None
    assert await digest.maybe_send(drive, now=time.time() + 8 * 86400) == "weekly digest sent to Saved Messages"
    assert drive.client.sent_to[-1][0] == "me" and "📅 SpiDrive" in drive.client.sent_to[-1][2]
    monkeypatch.setattr(conftest.cfg, "WEEKLY_DIGEST", False, raising=False)
    assert await digest.maybe_send(drive, now=time.time() + 30 * 86400) is None


async def test_digest_phone_command(drive):
    from phone import Phone
    p = Phone(drive, send_file=None, log=lambda m: None)
    assert (await p.handle("/digest")).startswith("📅 SpiDrive")


# ---------------------------------------------------------------- recently opened copies and low disk

async def test_opened_files_are_remembered_and_read_locally(drive):
    await put(drive, "/v/movie.bin", os.urandom(200 * 1024))
    e = drive.get_entry("/v/movie.bin")
    part = b"".join([x async for x in drive.iter_entry(e, 1000, 5000)])   # a seek: not remembered
    assert len(part) == 5000 and not drive.is_recent(drive.get_entry("/v/movie.bin"))
    data = b"".join([x async for x in drive.iter_entry(e)])
    assert drive.is_recent(drive.get_entry("/v/movie.bin"))
    drive.client.msgs.clear()
    drive._media_cache.clear()
    assert await read(drive, "/v/movie.bin") == data                 # from this PC now


async def test_aborted_reads_leave_nothing(drive):
    await put(drive, "/big.bin", os.urandom(300 * 1024))
    gen = drive.iter_entry(drive.get_entry("/big.bin"))
    await gen.__anext__()
    await gen.aclose()                                               # the viewer closed early
    assert not drive.is_recent(drive.get_entry("/big.bin"))
    assert not [n for n in os.listdir(drive_core.RECENT)]


async def test_low_disk_frees_least_recently_opened_but_never_kept(drive, monkeypatch):
    for name in ("a", "b", "c", "k"):
        await put(drive, f"/{name}.bin", os.urandom(100 * 1024))
        await read(drive, f"/{name}.bin")
        drive.db.execute("UPDATE recent SET opened=? WHERE file_id=?",
                         ({"a": 10, "b": 30, "c": 20, "k": 5}[name], drive.get_file(f"/{name}.bin")["id"]))
    drive.db.commit()
    drive.set_keep(["/k.bin"])
    await drive._keep_once()                                         # the recent copy becomes the kept one
    assert drive._kept_copy(drive.get_file("/k.bin")) and not drive._recent_copy(drive.get_file("/k.bin"))
    free = {"v": 50 * 1024}

    def usage(path):
        return Usage(1000 * 1024, 0, free["v"])                      # 5% free: too little
    monkeypatch.setattr(drive_core.shutil, "disk_usage", usage)
    real_discard = drive._discard

    def discard(p):
        if os.path.exists(p):
            free["v"] += os.path.getsize(p)
        real_discard(p)
    monkeypatch.setattr(drive, "_discard", discard)
    msg = drive.free_space(log=lambda m: None)
    assert msg and "low on space" in msg[0]
    left = {n: drive.is_recent(drive.get_entry(f"/{n}.bin")) for n in "abc"}
    assert left == {"a": False, "c": True, "b": True}                # 'a' was opened longest ago; 70 KB was enough
    assert drive.is_local(drive.get_entry("/k.bin"))                 # kept: untouched
    assert drive.disk["low"] is False
    free["v"] = 10 * 1024                                            # still low and nothing more to free...
    drive.free_space(log=lambda m: None)
    assert not any(drive.is_recent(drive.get_entry(f"/{n}.bin")) for n in "abc")
    drive._discard = real_discard


async def test_recent_copies_stay_under_their_size_limit(drive, monkeypatch):
    monkeypatch.setattr(drive_core, "RECENT_CACHE_SIZE", 250 * 1024)
    for i in range(4):
        await put(drive, f"/{i}.bin", os.urandom(100 * 1024))
        await read(drive, f"/{i}.bin")
        drive.db.execute("UPDATE recent SET opened=? WHERE file_id=?", (i, drive.get_file(f"/{i}.bin")["id"]))
    drive.db.commit()
    drive.free_space(log=lambda m: None)
    assert [drive.is_recent(drive.get_entry(f"/{i}.bin")) for i in range(4)] == [False, False, True, True]
