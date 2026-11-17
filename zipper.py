"""
Stream many drive files as one ZIP download, without storing the ZIP anywhere.

Files are stored (not compressed): photos, music and video don't get smaller, and
this keeps the download as fast as reading from Telegram. ZIP64 is used, so files
and archives over 4 GB work.
"""
import time
import zipfile

FLUSH = 1024 * 1024


class _Sink:
    """A write-only file object that zipfile writes into; we pass the bytes on."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b
        return len(b)

    def flush(self):
        pass


def _date_time(mtime):
    t = time.localtime(max(mtime or 0, 315532800 + 86400))   # ZIP cannot store dates before 1980
    return t[:6]


async def stream(write, drive, items):
    """items: [(name in the zip, entry dict or None for an empty folder)].
    write: async function that sends bytes to the client."""
    sink = _Sink()
    zf = zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True)

    async def drain(force=False):
        if sink.buf and (force or len(sink.buf) >= FLUSH):
            data = bytes(sink.buf)
            sink.buf.clear()
            await write(data)

    for name, e in items:
        if e is None:
            zf.writestr(zipfile.ZipInfo(name.rstrip("/") + "/", _date_time(time.time())), b"")
            await drain()
            continue
        zi = zipfile.ZipInfo(name, _date_time(e["mtime"]))
        zi.compress_type = zipfile.ZIP_STORED
        zi.file_size = e["size"]
        zi.external_attr = 0o644 << 16
        with zf.open(zi, "w", force_zip64=True) as out:
            async for data in drive.iter_entry(e):
                out.write(data)
                await drain()
        await drain()
    zf.close()
    await drain(force=True)
