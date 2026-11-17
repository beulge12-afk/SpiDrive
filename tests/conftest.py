"""Test setup: a throwaway config, and a fake Telegram client that keeps messages in memory."""
import asyncio
import contextlib
import datetime
import inspect
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

cfg = types.ModuleType("config")
cfg.__dict__.update(API_ID=1, API_HASH="x", PHONE="+1", CHANNEL="-1001001", DATA_DIR=os.path.join(ROOT, ".test-data"),
                    CHUNK_SIZE=64 * 1024, ENCRYPT=False, PASSPHRASE="", WEB_PORT=8000, DAV_PORT=8080,
                    PROXY_MODE="off")
sys.modules["config"] = cfg

import drive_core  # noqa: E402


class FakeMedia:
    def __init__(self, data, thumb=None):
        self.data, self.thumb = data, thumb


class FakeMsg:
    def __init__(self, mid, data, caption="", thumb=None, name=None, media=None, voice=None, date=None):
        self.id, self.message = mid, caption
        self.media = media if media is not None else FakeMedia(data, thumb)
        self.media.data, self.media.thumb = data, thumb
        self.file = types.SimpleNamespace(name=name, size=len(data), ext=os.path.splitext(name or "")[1] or ".bin")
        self.voice = voice
        self.date = date or datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)


class FakeClient:
    """The few Telethon calls SpiDrive makes, backed by a dict of messages."""

    def __init__(self):
        self.msgs, self.next_id = {}, 1
        self.delay, self.active, self.max_active, self.sent_to = 0, 0, 0, []

    async def upload_file(self, reader, file_size=None, file_name=None, part_size_kb=None, progress_callback=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            out = bytearray()
            while True:
                b = reader.read(16 * 1024)
                if not b:
                    break
                out += b
                if self.delay:
                    await asyncio.sleep(self.delay)
                if progress_callback:
                    r = progress_callback(len(out), file_size)
                    if inspect.isawaitable(r):
                        await r
            return bytes(out)
        finally:
            self.active -= 1

    def add(self, data, caption="", **kw):
        m = FakeMsg(self.next_id, data, caption, **kw)
        self.msgs[m.id] = m
        self.next_id += 1
        return m

    async def send_file(self, chat, file, caption="", **kw):
        if chat != "channel":
            self.sent_to.append((chat, file, caption))
        data = file if isinstance(file, bytes) else open(file, "rb").read() if isinstance(file, str) else b"media"
        return self.add(data, caption, name=os.path.basename(file) if isinstance(file, str) else None)

    async def iter_messages(self, chat, search=None, limit=None, min_id=0, reverse=False):
        for mid in sorted(self.msgs, reverse=not reverse):
            m = self.msgs.get(mid)
            if m is None or mid <= min_id or (search and search not in (m.message or "")):
                continue
            yield m

    async def send_message(self, chat, text, **kw):
        if chat != "channel":
            self.sent_to.append((chat, None, text))
        m = self.add(b"", text)
        m.media = None
        return m

    async def edit_message(self, chat, msg_id, text, **kw):
        m = self.msgs.get(msg_id)
        if m is None:
            raise type("MessageIdInvalidError", (Exception,), {})("message not found")
        m.message = text
        return m

    async def pin_message(self, chat, msg_id, notify=False):
        self.pinned = msg_id

    async def get_me(self):
        return types.SimpleNamespace(premium=False, first_name="Me")

    async def get_messages(self, chat, ids=None):
        if isinstance(ids, list):
            return [self.msgs.get(i) for i in ids]
        return self.msgs.get(ids)

    async def delete_messages(self, chat, ids):
        for i in ids:
            self.msgs.pop(i, None)

    def iter_download(self, media, offset=0, request_size=512 * 1024):
        async def gen():
            for i in range(offset, len(media.data), request_size):
                yield media.data[i:i + request_size]
        return gen()

    async def download_media(self, msg, file=bytes, thumb=None):
        return msg.media.thumb


@pytest.fixture
def drive(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for name, sub in [("DATA_DIR", ""), ("DB_PATH", "index.db"), ("INCOMING", "incoming"),
                      ("THUMBS_PATH", "thumbs.db"), ("TRASH_LOCAL", "trash-local"), ("OFFLINE", "offline"),
                      ("RECENT", "recent")]:
        monkeypatch.setattr(drive_core, name, str(data / sub) if sub else str(data))
    monkeypatch.setattr(drive_core, "CHUNK_SIZE", 64 * 1024)
    monkeypatch.setattr(cfg, "ENCRYPT", False)
    d = drive_core.Drive()
    d.client = FakeClient()
    d.channel = "channel"
    yield d
    d.db.close()


async def flush(d):
    """Run the upload queue until it is empty."""
    task = asyncio.create_task(d.upload_worker(log=lambda m: None))
    for _ in range(1000):
        await asyncio.sleep(0.005)
        if not d.pending() and not d.active_ids():
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def put(d, path, data, mtime=None, upload=True):
    tmp = d.new_staging_path()
    with open(tmp, "wb") as f:
        f.write(data)
    d.stage(tmp, path, mtime=mtime)
    if upload:
        await flush(d)


async def read(d, path):
    e = d.get_entry(path)
    return b"".join([x async for x in d.iter_entry(e)])
