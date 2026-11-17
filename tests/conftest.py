"""Test setup: a throwaway config, and a fake Telegram client that keeps messages in memory."""
import asyncio
import contextlib
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

cfg = types.ModuleType("config")
cfg.__dict__.update(API_ID=1, API_HASH="x", PHONE="+1", CHANNEL="-1001", DATA_DIR=os.path.join(ROOT, ".test-data"),
                    CHUNK_SIZE=64 * 1024, ENCRYPT=False, PASSPHRASE="", WEB_PORT=8000, DAV_PORT=8080,
                    PROXY_MODE="off")
sys.modules["config"] = cfg

import drive_core  # noqa: E402


class FakeMedia:
    def __init__(self, data, thumb=None):
        self.data, self.thumb = data, thumb


class FakeMsg:
    def __init__(self, mid, data, caption="", thumb=None):
        self.id, self.media, self.message = mid, FakeMedia(data, thumb), caption


class FakeClient:
    """The few Telethon calls SpiDrive makes, backed by a dict of messages."""

    def __init__(self):
        self.msgs, self.next_id = {}, 1

    async def upload_file(self, reader, file_size=None, file_name=None, part_size_kb=None, progress_callback=None):
        out = bytearray()
        while True:
            b = reader.read(7000)
            if not b:
                break
            out += b
        return bytes(out)

    async def send_file(self, chat, file, caption="", **kw):
        data = file if isinstance(file, bytes) else open(file, "rb").read()
        m = FakeMsg(self.next_id, data, caption)
        self.msgs[m.id] = m
        self.next_id += 1
        return m

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
                      ("THUMBS_PATH", "thumbs.db"), ("TRASH_LOCAL", "trash-local"), ("OFFLINE", "offline")]:
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
        if not d.pending() and d.current_pending_id() is None:
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
