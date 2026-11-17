"""
TelegramDrive core.

Stores files in your private Telegram channel:
  - each file is split into chunks of CHUNK_SIZE bytes
  - each chunk is sent to the channel as one document message
  - a local SQLite index (DATA_DIR/index.db) remembers which messages
    belong to which file, plus folders, sizes and checksums
  - optional AES-256-CTR encryption (ENCRYPT / PASSPHRASE in config.py)

Files added through the web dashboard or the Windows drive are first saved
in DATA_DIR/incoming ("pending"), shown in the drive immediately, and then
uploaded to Telegram in the background by upload_worker().

Deleting moves files to the trash (kept TRASH_DAYS days), and replacing a file
keeps the previous one as a version. Both only change the index: a trashed file's
row stays in `files` with the path "trash:<id>", so its Telegram messages are kept
until the trash is emptied. Files can be kept on this PC (DATA_DIR/offline).

The web dashboard (server.py) and the Windows drive (dav.py) use this module.
"""
import asyncio
import hashlib
import inspect
import json
import os
import random
import posixpath
import re
import secrets
import shutil
import sqlite3
import time
import uuid

from telethon import TelegramClient
from telethon.tl.types import (DocumentAttributeFilename, MessageMediaDocument,
                               MessageMediaPhoto)

import config
import keys
import search
import thumbs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = getattr(config, "SESSION_PATH", None) or os.path.join(BASE_DIR, "session")  # + ".session"
DATA_DIR = config.DATA_DIR
DB_PATH = os.path.join(DATA_DIR, "index.db")
INCOMING = os.path.join(DATA_DIR, "incoming")      # local copies waiting to go to Telegram
CHUNK_SIZE = int(config.CHUNK_SIZE)
TELEGRAM_MAX = 2000 * 1024 * 1024                  # 2 GB limit for non-premium accounts
REQUEST_SIZE = 512 * 1024                          # download block size
UPLOAD_RETRIES = 5
MEDIA_CACHE_SECONDS = 1800
THUMBS_PATH = os.path.join(DATA_DIR, "thumbs.db")  # photo previews (never sent to the channel)
TRASH_LOCAL = os.path.join(DATA_DIR, "trash-local")  # deleted files that never reached Telegram
OFFLINE = os.path.join(DATA_DIR, "offline")        # files kept on this PC (decrypted)
TRASH_DAYS = int(getattr(config, "TRASH_DAYS", 30))
KEEP_VERSIONS = int(getattr(config, "KEEP_VERSIONS", 10))
LIVE = "substr(path,1,1)='/'"                      # rows that are on the drive (not in the trash)
UPLOAD_PARALLEL = int(getattr(config, "UPLOAD_PARALLEL", 3))   # uploads at the same time
SMALL_FILE = 20 * 1024 * 1024                      # bigger files upload one at a time
BACKUP_KEEP = int(getattr(config, "BACKUP_KEEP", 7))   # index backups kept in the channel
HEALTH_BUDGET = 64 * 1024 * 1024                   # bytes re-downloaded per health check


# ---------------------------------------------------------------- helpers

class NotLoggedIn(RuntimeError):
    pass


async def _call(fn, *args):
    """Call a callback that may or may not be async."""
    r = fn(*args)
    if inspect.isawaitable(r):
        await r


def norm(path):
    """Normalise a drive path: forward slashes, one leading '/', no trailing '/'."""
    p = (path or "/").replace("\\", "/").strip("/")
    p = posixpath.normpath("/" + p)
    return "/" if p in ("/", ".") else p


def _derive_key(passphrase, salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode("utf-8"))


def _ctr(key, nonce, offset):
    """AES-CTR cipher positioned at an arbitrary byte offset of the file.
    CTR encrypt == decrypt, and it lets us start anywhere (needed for streaming)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    counter = (int.from_bytes(nonce, "big") + offset // 16) % (1 << 128)
    ctx = Cipher(algorithms.AES(key), modes.CTR(counter.to_bytes(16, "big"))).encryptor()
    if offset % 16:
        ctx.update(b"\0" * (offset % 16))
    return ctx


class ChunkReader:
    """File-like object exposing bytes [start, start+length) of a local file,
    optionally encrypted on the fly, hashing the plaintext as it is read.
    Lets Telethon upload a 1.9 GB chunk without loading it into RAM or
    writing a temporary copy."""

    def __init__(self, path, start, length, cipher_factory=None):
        self.f = open(path, "rb")
        self.start = start
        self.length = length
        self.pos = 0
        self.name = "chunk.bin"
        self._cipher_factory = cipher_factory
        self._cipher = None
        self._cipher_pos = None
        self._hash = hashlib.sha256()
        self.hashed = 0

    def read(self, n=-1):
        remaining = self.length - self.pos
        if n is None or n < 0 or n > remaining:
            n = remaining
        if n <= 0:
            return b""
        self.f.seek(self.start + self.pos)
        data = self.f.read(n)
        if not data:
            raise IOError("local file shrank while uploading")
        if self.pos == self.hashed:            # hash each byte exactly once, in order
            self._hash.update(data)
            self.hashed += len(data)
        if self._cipher_factory:
            if self._cipher is None or self._cipher_pos != self.pos:
                self._cipher = self._cipher_factory(self.start + self.pos)
            out = self._cipher.update(data)
            self._cipher_pos = self.pos + len(data)
        else:
            out = data
        self.pos += len(data)
        return out

    def seek(self, offset, whence=0):
        if whence == 0:
            new = offset
        elif whence == 1:
            new = self.pos + offset
        else:
            new = self.length + offset
        self.pos = max(0, min(self.length, new))
        return self.pos

    def tell(self):
        return self.pos

    def seekable(self):
        return True

    def hexdigest(self):
        return self._hash.hexdigest()

    def close(self):
        self.f.close()


# ---------------------------------------------------------------- drive

class Drive:
    def __init__(self):
        if not str(config.CHANNEL).strip():
            raise SystemExit("CHANNEL is empty in config.py. Put the -100... ID from telegram_login.py there.")
        if CHUNK_SIZE > TELEGRAM_MAX:
            raise SystemExit("CHUNK_SIZE is bigger than Telegram's 2 GB limit.")
        os.makedirs(DATA_DIR, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("ATTACH DATABASE ? AS th", (THUMBS_PATH,))
        self._init_db()
        from proxy import ProxyManager
        self.proxy = ProxyManager(config)              # direct, or through a proxy (proxy.py)
        self.client = None                             # created in connect()
        self.channel = None
        self._key = None
        self._media_cache = {}
        # background upload state
        os.makedirs(INCOMING, exist_ok=True)
        self._wake = asyncio.Event()
        self._active = {}             # pending id -> upload task running now
        self._active_size = {}        # pending id -> size
        self._trash = set()           # staged files to delete once Windows releases them
        self._retry_at = {}           # pending id -> time of next attempt
        self.progress = {}            # pending id -> {"done", "total", "error"}
        self.recent = []              # finished uploads, newest last (for the dashboard)
        self._thumb_changes = 0       # writes to thumbs.db, left out of version()
        self._thumb_sem = asyncio.Semaphore(2)
        self._pending_thumbs = {}     # staged file path -> preview bytes
        self._keep_wake = asyncio.Event()
        self.keeping = {}             # file id -> {"done", "total", "path"} while kept files download
        self.paused = self._meta("uploads_paused") == "1"
        self.limit = int(self._meta("upload_limit") or 0)   # bytes per second, 0 = no limit
        self._next_free = 0.0         # shared schedule for the speed limit
        self._handlers = []           # Telegram event handlers (phone commands)
        self.health_running = False

    # ---------- lifecycle

    def _new_client(self, via):
        from proxy import client_kwargs
        c = TelegramClient(SESSION_PATH, config.API_ID, config.API_HASH, **client_kwargs(via))
        c.flood_sleep_threshold = 300                  # auto-wait on short flood limits
        for callback, event in self._handlers:         # survive switching to another proxy
            c.add_event_handler(callback, event)
        return c

    def channel_id(self):
        """The storage channel's id, like -1001234567890."""
        return int(str(config.CHANNEL).strip())

    def on_event(self, callback, event):
        """Listen to Telegram updates (also after the connection is switched)."""
        self._handlers.append((callback, event))
        if self.client is not None:
            self.client.add_event_handler(callback, event)

    async def connect(self, interactive=True):
        """interactive=True asks for the login code in the console if needed (drive.py,
        server.py). The app passes False and shows its setup wizard instead."""
        via = await self.proxy.choose()
        self.client = self._new_client(via)
        if interactive:
            await self.client.start(phone=config.PHONE)
        else:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise NotLoggedIn("Telegram login is missing or expired")
        cid = int(config.CHANNEL)
        try:
            self.channel = await self.client.get_entity(cid)
        except ValueError:
            await self.client.get_dialogs()            # refresh entity cache, then retry
            self.channel = await self.client.get_entity(cid)
        return self

    async def switch_connection(self, force_search=True):
        """Reconnect through another route (a new proxy, or direct). Uploads in
        progress fail once and are retried by the upload queue."""
        exclude = [self.proxy.current] if self.proxy.current else ["direct"]
        via = await self.proxy.choose(force_search=force_search, exclude=exclude if force_search else ())
        old = self.client
        if old is not None:
            try:
                await asyncio.wait_for(old.disconnect(), 10)
            except Exception:
                pass
        self.client = self._new_client(via)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise NotLoggedIn("Telegram login is missing or expired")
        self.channel = await self.client.get_entity(int(config.CHANNEL))
        self._media_cache.clear()
        return via

    async def close(self):
        if self.client is not None:
            await self.client.disconnect()
        self.db.close()

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *exc):
        await self.close()

    # ---------- database

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            path       TEXT UNIQUE NOT NULL,
            size       INTEGER NOT NULL,
            mtime      REAL NOT NULL,
            created    REAL NOT NULL,
            encrypted  INTEGER NOT NULL DEFAULT 0,
            nonce      BLOB,
            chunk_size INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'uploading'
        );
        CREATE TABLE IF NOT EXISTS chunks(
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            idx     INTEGER NOT NULL,
            msg_id  INTEGER NOT NULL,
            start   INTEGER NOT NULL,
            size    INTEGER NOT NULL,
            sha256  TEXT NOT NULL,
            PRIMARY KEY(file_id, idx)
        );
        CREATE TABLE IF NOT EXISTS dirs(path TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value BLOB);
        CREATE INDEX IF NOT EXISTS chunks_msg ON chunks(msg_id);
        CREATE TABLE IF NOT EXISTS pending(
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            path    TEXT UNIQUE NOT NULL,
            tmp     TEXT NOT NULL,
            size    INTEGER NOT NULL,
            mtime   REAL NOT NULL,
            created REAL NOT NULL
        );
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        if "origin" not in cols:     # 'local' = uploaded by SpiDrive, 'telegram' = sent from Telegram
            self.db.execute("ALTER TABLE files ADD COLUMN origin TEXT NOT NULL DEFAULT 'local'")
        if "keep" not in cols:       # 1 = keep a copy on this PC
            self.db.execute("ALTER TABLE files ADD COLUMN keep INTEGER NOT NULL DEFAULT 0")
        if "keep" not in {r[1] for r in self.db.execute("PRAGMA table_info(pending)")}:
            self.db.execute("ALTER TABLE pending ADD COLUMN keep INTEGER NOT NULL DEFAULT 0")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS trash_batches(
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            path    TEXT NOT NULL,                     -- what was deleted (or the replaced file)
            is_dir  INTEGER NOT NULL DEFAULT 0,
            deleted REAL NOT NULL,
            reason  TEXT NOT NULL DEFAULT 'deleted'    -- 'deleted' or 'version'
        );
        CREATE TABLE IF NOT EXISTS trash_items(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            batch     INTEGER NOT NULL REFERENCES trash_batches(id) ON DELETE CASCADE,
            file_id   INTEGER REFERENCES files(id) ON DELETE CASCADE,
            orig_path TEXT NOT NULL,
            is_dir    INTEGER NOT NULL DEFAULT 0,
            tmp       TEXT,                            -- local copy of a file that never reached Telegram
            size      INTEGER NOT NULL DEFAULT 0,
            mtime     REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS trash_items_batch ON trash_items(batch);
        CREATE INDEX IF NOT EXISTS trash_batches_path ON trash_batches(path);
        CREATE TABLE IF NOT EXISTS health(
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            idx     INTEGER NOT NULL,
            checked REAL NOT NULL,
            ok      INTEGER NOT NULL,
            error   TEXT,
            PRIMARY KEY(file_id, idx)
        );
        CREATE TABLE IF NOT EXISTS links(
            token     TEXT PRIMARY KEY,
            path      TEXT NOT NULL,
            file_id   INTEGER,
            created   REAL NOT NULL,
            expires   REAL NOT NULL,
            downloads INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS th.thumbs(
            key    TEXT PRIMARY KEY,                   -- sha256 of the first part, or msg:<id>
            data   BLOB,
            taken  REAL,
            failed INTEGER NOT NULL DEFAULT 0
        );
        """)
        self.db.commit()

    def version(self):
        """Changes whenever the index changes (here or in another process)."""
        dv = self.db.execute("PRAGMA data_version").fetchone()[0]
        return f"{self.db.total_changes - self._thumb_changes}-{dv}"

    def _meta(self, key):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def _set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
        self.db.commit()

    def _get_key(self):
        """The master key that encrypts files (see keys.py)."""
        if self._key is None:
            self._key = self._unlock(config.PASSPHRASE)
        return self._key

    def _unlock(self, passphrase):
        if not passphrase:
            raise RuntimeError("This needs encryption but PASSPHRASE is empty in config.py.")
        wrapped = self._meta("wrap_pass")
        if wrapped is not None:
            try:
                return keys.unwrap(keys.kdf(passphrase, self._meta("wrap_pass_salt")), wrapped)
            except keys.WrongKey:
                raise RuntimeError("Wrong PASSPHRASE: it differs from the one used for earlier files.") from None
        # first use, or an index from before keys.py: the key comes from the passphrase itself
        salt = self._meta("salt")
        if salt is None:
            salt = secrets.token_bytes(16)
            self._set_meta("salt", salt)
        key = _derive_key(passphrase, salt)
        check = hashlib.sha256(b"tgdrive-check" + key).digest()
        stored = self._meta("key_check")
        if stored is None:
            self._set_meta("key_check", check)
        elif stored != check:
            raise RuntimeError("Wrong PASSPHRASE: it differs from the one used for earlier files.")
        self._wrap_for_passphrase(key, passphrase)
        return key

    def _wrap_for_passphrase(self, master, passphrase):
        salt = secrets.token_bytes(16)
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('wrap_pass_salt',?)", (salt,))
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('wrap_pass',?)",
                        (keys.wrap(keys.kdf(passphrase, salt), master),))
        self.db.commit()

    def has_key(self):
        return self._meta("wrap_pass") is not None or self._meta("key_check") is not None

    def key_status(self):
        created = self._meta("recovery_created")
        return {"has_key": self.has_key(), "recovery": float(created) if created else None}

    def change_passphrase(self, new, current=None, recovery_key=None):
        """Re-wrap the master key for a new passphrase. Proves the right to do so with the
        current passphrase or the recovery key. Files are not touched."""
        if len(new or "") < 8:
            raise ValueError("The new passphrase must have at least 8 characters.")
        if recovery_key:
            if self._meta("wrap_recovery") is None:
                raise ValueError("No recovery key was created for this drive.")
            rk = keys.normalize_recovery_key(recovery_key)
            master = keys.unwrap(keys.kdf(rk, self._meta("wrap_recovery_salt")), self._meta("wrap_recovery"))
        elif self.has_key():
            try:
                master = self._unlock(current or "")
            except RuntimeError:
                raise ValueError("The current passphrase is not right.") from None
        else:                                        # nothing encrypted yet: no key to re-wrap
            if config.PASSPHRASE and not secrets.compare_digest(
                    (current or "").encode("utf-8"), str(config.PASSPHRASE).encode("utf-8")):
                raise ValueError("The current passphrase is not right.")
            master = None
        if master is not None:
            self._wrap_for_passphrase(master, new)
            self._key = master
        return master is not None

    def create_recovery_key(self):
        """A new recovery key (replaces an older one). Shown once: print it or write it down."""
        master = self._get_key()
        rk = keys.new_recovery_key()
        salt = secrets.token_bytes(16)
        for k, v in (("wrap_recovery_salt", salt),
                     ("wrap_recovery", keys.wrap(keys.kdf(keys.normalize_recovery_key(rk), salt), master)),
                     ("recovery_created", str(time.time()))):
            self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, v))
        self.db.commit()
        return rk

    def _chunks(self, file_id):
        return self.db.execute(
            "SELECT * FROM chunks WHERE file_id=? ORDER BY idx", (file_id,)).fetchall()

    # ---------- queries
    #
    # An "entry" is a dict describing one file. Files waiting in the upload
    # queue have status 'pending' and a negative id; they shadow an older
    # Telegram copy at the same path until their upload finishes.

    def get_file(self, path):
        """The Telegram-stored file at `path` (any status), or None."""
        return self.db.execute("SELECT * FROM files WHERE path=?", (norm(path),)).fetchone()

    def get_file_by_id(self, file_id):
        return self.db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()

    def _pending_row(self, path):
        return self.db.execute("SELECT * FROM pending WHERE path=?", (norm(path),)).fetchone()

    @staticmethod
    def _pending_entry(p):
        return {"id": -p["id"], "path": p["path"], "size": p["size"], "mtime": p["mtime"],
                "created": p["created"], "encrypted": int(bool(config.ENCRYPT)), "nonce": None,
                "chunk_size": 0, "status": "pending", "origin": "local", "tmp": p["tmp"],
                "keep": p["keep"]}

    def get_entry(self, path):
        """Pending upload or stored file at `path`, as a dict, or None."""
        p = self._pending_row(path)
        if p is not None:
            return self._pending_entry(p)
        r = self.get_file(path)
        return dict(r) if r is not None else None

    def get_entry_by_id(self, entry_id):
        if entry_id < 0:
            p = self.db.execute("SELECT * FROM pending WHERE id=?", (-entry_id,)).fetchone()
            return self._pending_entry(p) if p is not None else None
        r = self.get_file_by_id(entry_id)
        if r is None or self._pending_row(r["path"]) is not None:
            return None
        return dict(r)

    def exists(self, path):
        return self.get_entry(path) is not None or self.is_dir(path)

    def free_path(self, path):
        """`path` if unused, otherwise 'name (2).ext', 'name (3).ext', ..."""
        path = norm(path)
        if not self.exists(path):
            return path
        stem, ext = posixpath.splitext(path)
        i = 2
        while True:
            p = f"{stem} ({i}){ext}"
            if not self.exists(p):
                return p
            i += 1

    def search(self, query, limit=500):
        """Files matching a smart search (see search.py): words in any Persian spelling,
        plus filters like type:video size:>1gb after:2026-01. Returns (entries, errors)."""
        q = search.Query(query)
        if q.empty:
            return [], q.errors

        def where(size, date):
            sql, args = "", []
            for cond, val in ((f" AND {size}>=?", q.min_size), (f" AND {size}<=?", q.max_size),
                              (f" AND {date}>=?", q.after), (f" AND {date}<?", q.before)):
                if val is not None:
                    sql, args = sql + cond, args + [val]
            return sql, args

        found = {}
        sql, args = where("size", "mtime")
        for p in self.db.execute("SELECT * FROM pending WHERE 1" + sql, args):
            if q.match(p["path"], p["size"], p["mtime"]):
                found[p["path"]] = self._pending_entry(p)
        # dates: when a photo was taken (from its preview), else the file date
        sql, args = where("f.size", "COALESCE(t.taken, f.mtime)")
        for r in self.db.execute(
                "SELECT f.*, COALESCE(t.taken, f.mtime) AS date FROM files f "
                "LEFT JOIN chunks c ON c.file_id=f.id AND c.idx=0 "
                "LEFT JOIN th.thumbs t ON t.key = CASE WHEN c.sha256='' THEN 'msg:'||c.msg_id ELSE c.sha256 END "
                "WHERE f.status='complete' AND substr(f.path,1,1)='/'" + sql, args):
            if r["path"] not in found and q.match(r["path"], r["size"], r["date"]):
                found[r["path"]] = dict(r)
        return [found[p] for p in sorted(found)][:limit], q.errors

    def is_dir(self, path):
        path = norm(path)
        if path == "/":
            return True
        if self.db.execute("SELECT 1 FROM dirs WHERE path=?", (path,)).fetchone():
            return True
        pre = path + "/"
        for table in ("files", "pending"):
            if self.db.execute(f"SELECT 1 FROM {table} WHERE substr(path,1,?)=? LIMIT 1",
                               (len(pre), pre)).fetchone():
                return True
        return False

    def listdir(self, path="/"):
        """Return (sorted subfolder names, file entries) directly inside `path`."""
        path = norm(path)
        pre = "/" if path == "/" else path + "/"
        dirs, files = set(), {}
        for p in self.db.execute("SELECT * FROM pending WHERE substr(path,1,?)=?", (len(pre), pre)):
            rest = p["path"][len(pre):]
            if "/" in rest:
                dirs.add(rest.split("/")[0])
            else:
                files[p["path"]] = self._pending_entry(p)
        for r in self.db.execute("SELECT * FROM files WHERE substr(path,1,?)=?", (len(pre), pre)):
            rest = r["path"][len(pre):]
            if "/" in rest:
                dirs.add(rest.split("/")[0])
            else:
                files.setdefault(r["path"], dict(r))
        for (p,) in self.db.execute(
                "SELECT path FROM dirs WHERE substr(path,1,?)=?", (len(pre), pre)):
            dirs.add(p[len(pre):].split("/")[0])
        return sorted(dirs), [files[k] for k in sorted(files)]

    def walk(self, path="/"):
        """All complete files under a folder (recursive)."""
        path = norm(path)
        pre = "/" if path == "/" else path + "/"
        return self.db.execute(
            "SELECT * FROM files WHERE substr(path,1,?)=? AND status='complete' ORDER BY path",
            (len(pre), pre)).fetchall()

    def stats(self):
        r = self.db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status='complete' AND {LIVE}").fetchone()
        inc = self.db.execute(f"SELECT COUNT(*) FROM files WHERE status!='complete' AND {LIVE}").fetchone()[0]
        p = self.db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM pending").fetchone()
        t = self.db.execute(
            "SELECT COUNT(DISTINCT b.id), COALESCE(SUM(i.size),0) FROM trash_batches b "
            "LEFT JOIN trash_items i ON i.batch=b.id WHERE b.reason='deleted'").fetchone()
        v = self.db.execute(
            "SELECT COUNT(DISTINCT b.id), COALESCE(SUM(i.size),0) FROM trash_batches b "
            "JOIN trash_items i ON i.batch=b.id WHERE b.reason='version'").fetchone()
        return {"files": r[0], "bytes": r[1], "incomplete": inc,
                "pending": p[0], "pending_bytes": p[1], "trash": t[0], "trash_bytes": t[1],
                "versions": v[0], "versions_bytes": v[1]}

    def set_mtime(self, path, mtime):
        """Change a file's modified time (Windows sends it after copying a file)."""
        p = self._pending_row(path)
        if p is not None:
            try:
                os.utime(p["tmp"], (mtime, mtime))
            except OSError:
                pass
            self.db.execute("UPDATE pending SET mtime=? WHERE id=?", (mtime, p["id"]))
        else:
            self.db.execute("UPDATE files SET mtime=? WHERE path=?", (mtime, norm(path)))
        self.db.commit()

    # ---------- folders

    def mkdir(self, path):
        path = norm(path)
        if self.get_entry(path):
            raise FileExistsError(f"a file already exists at {path}")
        while path != "/":
            self.db.execute("INSERT OR IGNORE INTO dirs(path) VALUES(?)", (path,))
            path = posixpath.dirname(path)
        self.db.commit()

    def move(self, src, dst):
        """Rename/move a file or folder (only changes the index, nothing is re-uploaded)."""
        src, dst = norm(src), norm(dst)
        if src == "/" or dst == "/":
            raise ValueError("cannot move the root folder")
        if src == dst:
            return
        if self.exists(dst):
            raise FileExistsError(dst)
        if self.get_entry(src):
            self.db.execute("UPDATE pending SET path=? WHERE path=?", (dst, src))
            self.db.execute("UPDATE files SET path=? WHERE path=?", (dst, src))
            self.db.execute("UPDATE trash_batches SET path=? WHERE reason='version' AND path=?", (dst, src))
        elif self.is_dir(src):
            if dst.startswith(src + "/"):
                raise ValueError("cannot move a folder inside itself")
            pre = src + "/"
            for table in ("files", "pending", "dirs"):
                rows = self.db.execute(
                    f"SELECT path FROM {table} WHERE path=? OR substr(path,1,?)=?",
                    (src, len(pre), pre)).fetchall()
                for (p,) in rows:
                    self.db.execute(f"UPDATE {table} SET path=? WHERE path=?", (dst + p[len(src):], p))
            for bid, p in self.db.execute(
                    "SELECT id, path FROM trash_batches WHERE reason='version' AND substr(path,1,?)=?",
                    (len(pre), pre)).fetchall():
                self.db.execute("UPDATE trash_batches SET path=? WHERE id=?", (dst + p[len(src):], bid))
        else:
            raise FileNotFoundError(src)
        self.db.commit()
        if posixpath.dirname(dst) != "/":
            self.mkdir(posixpath.dirname(dst))

    async def copy(self, src, dst):
        """Copy a file or folder. Stored files are not uploaded again: the copy
        points at the same Telegram messages (they are only deleted from the
        channel when no copy uses them any more)."""
        src, dst = norm(src), norm(dst)
        if self.exists(dst):
            raise FileExistsError(dst)
        if self.get_entry(src):
            pairs = [(src, dst)]
        elif self.is_dir(src):
            if dst == src or dst.startswith(src + "/"):
                raise ValueError("cannot copy a folder inside itself")
            pre = src + "/"
            paths = {p for (p,) in self.db.execute(
                "SELECT path FROM pending WHERE substr(path,1,?)=? UNION "
                "SELECT path FROM files WHERE substr(path,1,?)=? AND status='complete'",
                (len(pre), pre, len(pre), pre))}
            pairs = [(p, dst + p[len(src):]) for p in sorted(paths)]
            self.mkdir(dst)
            for (d,) in self.db.execute(
                    "SELECT path FROM dirs WHERE substr(path,1,?)=?", (len(pre), pre)).fetchall():
                self.mkdir(dst + d[len(src):])
        else:
            raise FileNotFoundError(src)
        for a, b in pairs:
            e = self.get_entry(a)
            if e["status"] == "pending":
                tmp = self.new_staging_path()
                await asyncio.to_thread(shutil.copy2, e["tmp"], tmp)
                self.stage(tmp, b)
            elif e["status"] == "complete":
                cur = self.db.execute(
                    "INSERT INTO files(path,size,mtime,created,encrypted,nonce,chunk_size,status,origin) "
                    "VALUES(?,?,?,?,?,?,?,'complete',?)",
                    (b, e["size"], e["mtime"], time.time(), e["encrypted"], e["nonce"],
                     e["chunk_size"], e["origin"]))
                self.db.execute(
                    "INSERT INTO chunks(file_id,idx,msg_id,start,size,sha256) "
                    "SELECT ?,idx,msg_id,start,size,sha256 FROM chunks WHERE file_id=?",
                    (cur.lastrowid, e["id"]))
                self.db.commit()
        if posixpath.dirname(dst) != "/":
            self.mkdir(posixpath.dirname(dst))
        return len(pairs)

    # ---------- staging (files waiting to be uploaded to Telegram)

    def new_staging_path(self):
        return os.path.join(INCOMING, uuid.uuid4().hex)

    def stage(self, tmp, path, mtime=None):
        """Take ownership of the local file `tmp` and queue it for upload to
        `path`. It appears in the drive straight away."""
        path = norm(path)
        if path == "/" or self.is_dir(path):
            self._discard(tmp)
            raise IsADirectoryError(path)
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        old = self._pending_row(path)
        if old is not None:
            self._cancel_if_current(old["id"])
            self._discard(old["tmp"])
        self.db.execute("DELETE FROM pending WHERE path=?", (path,))
        self.db.execute(
            "INSERT INTO pending(path,tmp,size,mtime,created) VALUES(?,?,?,?,?)",
            (path, tmp, os.path.getsize(tmp), os.path.getmtime(tmp), time.time()))
        self.db.commit()
        if posixpath.dirname(path) != "/":
            self.mkdir(posixpath.dirname(path))
        self._wake.set()
        return path

    def pending(self):
        return self.db.execute("SELECT * FROM pending ORDER BY id").fetchall()

    def active_ids(self):
        """Pending uploads running right now."""
        return set(self._active)

    def _cancel_if_current(self, pending_id):
        t = self._active.get(pending_id)
        if t is not None:
            t.cancel()

    def set_upload_controls(self, paused=None, limit=None):
        """Pause or resume uploads, and set a speed limit in bytes per second (0 = none).
        Pausing stops running uploads; they continue from their last finished part."""
        if paused is not None:
            self.paused = bool(paused)
            self._set_meta("uploads_paused", "1" if self.paused else "0")
            if self.paused:
                for t in list(self._active.values()):
                    t.cancel()
        if limit is not None:
            self.limit = max(0, int(limit))
            self._set_meta("upload_limit", str(self.limit))
        self._wake.set()

    async def _throttle(self, nbytes):
        """Keep all uploads together under the speed limit."""
        if not self.limit or nbytes <= 0:
            return
        now = time.monotonic()
        self._next_free = max(self._next_free, now) + nbytes / self.limit
        delay = self._next_free - now - 0.25
        if delay > 0:
            await asyncio.sleep(delay)

    def _discard(self, tmp):
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            self._trash.add(tmp)      # still open (Windows); try again later

    def _empty_trash(self):
        for tmp in list(self._trash):
            try:
                os.remove(tmp)
                self._trash.discard(tmp)
            except FileNotFoundError:
                self._trash.discard(tmp)
            except OSError:
                pass

    def clean_incoming(self):
        """Remove staged files that no pending upload refers to (after a crash)."""
        keep = {os.path.normcase(os.path.abspath(p["tmp"])) for p in self.pending()}
        for name in os.listdir(INCOMING):
            full = os.path.join(INCOMING, name)
            if os.path.normcase(os.path.abspath(full)) not in keep:
                self._discard(full)
        for p in self.pending():
            if not os.path.exists(p["tmp"]):
                self.db.execute("DELETE FROM pending WHERE id=?", (p["id"],))
        self.db.commit()

    def _finished(self, path, state, size, error=None):
        self.recent.append({"path": path, "state": state, "total": size,
                            "error": error, "t": time.time()})
        del self.recent[:-200]

    async def upload_worker(self, log=print):
        """Runs forever: uploads pending files to Telegram, several small files at once
        (big ones one at a time), retrying with a growing delay when the network fails."""
        jobs = set()
        try:
            while True:
                self._empty_trash()
                now = time.time()
                if not self.paused:
                    big_running = any(sz > SMALL_FILE for sz in self._active_size.values())
                    for p in self.pending():
                        if len(self._active_size) >= UPLOAD_PARALLEL:
                            break
                        if p["id"] in self._active_size or self._retry_at.get(p["id"], 0) > now:
                            continue
                        big = p["size"] > SMALL_FILE
                        if big and big_running:
                            continue
                        big_running |= big
                        self._active_size[p["id"]] = p["size"]
                        t = asyncio.create_task(self._run_job(p, log))
                        jobs.add(t)
                        t.add_done_callback(lambda t, pid=p["id"]: (
                            jobs.discard(t), self._active_size.pop(pid, None), self._wake.set()))
                waits = [t - now for t in self._retry_at.values() if t > now]
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=min(waits + [30]))
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in list(jobs):
                t.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def _run_job(self, job, log):
        pid, target = job["id"], job["path"]
        if not os.path.exists(job["tmp"]):
            self.db.execute("DELETE FROM pending WHERE id=?", (pid,))
            self.db.commit()
            return
        prog = self.progress.setdefault(pid, {"done": 0, "total": job["size"], "error": None})
        prog["error"] = None

        def cb(done, total, prog=prog):
            prog["done"], prog["total"] = done, total

        task = asyncio.create_task(self.upload(job["tmp"], target, overwrite=True, progress=cb))
        self._active[pid] = task
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            self._active.pop(pid, None)

        now_p = self.db.execute("SELECT * FROM pending WHERE id=?", (pid,)).fetchone()
        if task.cancelled() and now_p is not None:
            return                                       # paused: continues later
        if task.cancelled() or now_p is None:
            # deleted or replaced while uploading
            self.progress.pop(pid, None)
            self._retry_at.pop(pid, None)
            if not task.cancelled() and task.exception() is None and now_p is None \
                    and self._pending_row(target) is None and not task.result().get("deduped"):
                row = self.get_file_by_id(task.result()["id"])
                if row is not None:
                    await self._delete_rows([row])
            return
        exc = task.exception()
        if exc is not None:
            tries = prog.get("tries", 0) + 1
            prog["tries"] = tries
            wait = min(300, 15 * 2 ** (tries - 1))
            prog["error"] = f"{exc or type(exc).__name__}"
            self._retry_at[pid] = time.time() + wait
            log(f"upload failed ({target}): {exc!r}. Retrying in {wait}s")
            return

        result = task.result()
        row = self.get_file_by_id(result["id"])
        if row is not None and row["path"] != now_p["path"]:     # renamed while uploading
            other = self.get_file(now_p["path"])
            if other is not None and other["id"] != row["id"]:
                if other["status"] == "complete":
                    await self._retire_version(other)
                else:
                    await self._delete_rows([other])
            self.db.execute("UPDATE files SET path=? WHERE id=?", (now_p["path"], row["id"]))
        if row is not None and row["mtime"] != now_p["mtime"]:
            self.db.execute("UPDATE files SET mtime=? WHERE id=?", (now_p["mtime"], row["id"]))
        kept = await self._after_upload(row, now_p)
        self.db.execute("DELETE FROM pending WHERE id=?", (pid,))
        self.db.commit()
        if not kept:
            self._discard(now_p["tmp"])
        self.progress.pop(pid, None)
        self._retry_at.pop(pid, None)
        self._finished(now_p["path"], "done", now_p["size"])
        log(f"{'already on the drive, added instantly' if result.get('deduped') else 'uploaded'}: {now_p['path']}")

    async def _after_upload(self, row, p):
        """Make the photo preview from the local copy (so it never has to be downloaded),
        and keep the copy on this PC if asked. Returns True if the copy was kept."""
        if row is None:
            return False
        if thumbs.available() and search.kind_of(p["path"]) == "image":
            key = self.thumb_key(dict(row))
            if key and self._thumb_row(key) is None:
                try:
                    data, taken = await asyncio.to_thread(thumbs.make, p["tmp"])
                    self._save_thumb(key, data, taken)
                except Exception:
                    pass
        if not (p["keep"] or row["keep"]):
            return False
        self.db.execute("UPDATE files SET keep=1 WHERE id=?", (row["id"],))
        try:
            os.makedirs(OFFLINE, exist_ok=True)
            os.replace(p["tmp"], self.offline_path(row["id"]))
            return True
        except OSError:
            self._keep_wake.set()                   # download it again instead
            return False

    # ---------- upload

    async def upload(self, local_path, remote_path, overwrite=False, progress=None):
        """Upload a local file. Interrupted uploads resume from the last finished chunk."""
        remote = norm(remote_path)
        if remote == "/" or self.is_dir(remote):
            raise IsADirectoryError(remote)
        size = os.path.getsize(local_path)
        mtime = os.path.getmtime(local_path)
        enc = bool(config.ENCRYPT)

        row = self.get_file(remote)
        keep = 0
        if row is not None:
            if row["status"] == "complete":
                if not overwrite:
                    raise FileExistsError(remote)
                if row["encrypted"] == int(enc) and await self._same_content(local_path, size, row):
                    # nothing changed but the date (sync, or saved without changes)
                    self.db.execute("UPDATE files SET mtime=? WHERE id=?", (mtime, row["id"]))
                    self.db.commit()
                    if progress:
                        await _call(progress, size, size)
                    return {**dict(self.get_file_by_id(row["id"])), "deduped": True}
                keep = row["keep"]
                await self._retire_version(row)        # the old file stays restorable
                row = None
            elif (row["size"], row["mtime"], row["encrypted"]) != (size, mtime, int(enc)):
                await self._delete_rows([row])         # stale partial upload of a different file
                row = None
        if row is None:
            dup = await self._find_duplicate(local_path, size, enc)
            if dup is not None:                        # same content already in the channel
                cur = self.db.execute(
                    "INSERT INTO files(path,size,mtime,created,encrypted,nonce,chunk_size,status,origin,keep) "
                    "VALUES(?,?,?,?,?,?,?,'complete','local',?)",
                    (remote, size, mtime, time.time(), dup["encrypted"], dup["nonce"], dup["chunk_size"], keep))
                self.db.execute(
                    "INSERT INTO chunks(file_id,idx,msg_id,start,size,sha256) "
                    "SELECT ?,idx,msg_id,start,size,sha256 FROM chunks WHERE file_id=?", (cur.lastrowid, dup["id"]))
                self.db.commit()
                if posixpath.dirname(remote) != "/":
                    self.mkdir(posixpath.dirname(remote))
                if progress:
                    await _call(progress, size, size)
                return {**dict(self.get_file_by_id(cur.lastrowid)), "deduped": True}
            self.db.execute(
                "INSERT INTO files(path,size,mtime,created,encrypted,nonce,chunk_size,status,keep) "
                "VALUES(?,?,?,?,?,?,?,'uploading',?)",
                (remote, size, mtime, time.time(), int(enc),
                 secrets.token_bytes(16) if enc else None, CHUNK_SIZE, keep))
            self.db.commit()
            row = self.get_file(remote)

        key = self._get_key() if enc else None
        chunk_size = row["chunk_size"]
        n = (size + chunk_size - 1) // chunk_size
        done = {c["idx"] for c in self._chunks(row["id"])}

        for idx in range(n):
            if idx in done:
                continue
            start = idx * chunk_size
            length = min(chunk_size, size - start)
            msg_id, digest = await self._upload_chunk(
                local_path, row, idx, n, start, length, key, progress)
            self.db.execute(
                "INSERT INTO chunks(file_id,idx,msg_id,start,size,sha256) VALUES(?,?,?,?,?,?)",
                (row["id"], idx, msg_id, start, length, digest))
            self.db.commit()

        self.db.execute("UPDATE files SET status='complete' WHERE id=?", (row["id"],))
        self.db.commit()
        if posixpath.dirname(remote) != "/":
            self.mkdir(posixpath.dirname(remote))
        return {**dict(self.get_file_by_id(row["id"])), "deduped": False}

    # ---------- duplicates

    @staticmethod
    def _hash_range(path, start, length):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                b = f.read(min(1024 * 1024, left))
                if not b:
                    break
                h.update(b)
                left -= len(b)
        return h.hexdigest()

    async def _same_content(self, local_path, size, row, cache=None):
        """True if the local file has exactly the content of a stored file (checked part
        by part with the SHA-256 the index already has; stops at the first difference)."""
        if row["size"] != size or size == 0:
            return False
        chunks = self._chunks(row["id"])
        if not chunks or any(not c["sha256"] for c in chunks) or sum(c["size"] for c in chunks) != size:
            return False
        cache = {} if cache is None else cache
        for c in chunks:
            k = (c["start"], c["size"])
            if k not in cache:
                cache[k] = await asyncio.to_thread(self._hash_range, local_path, c["start"], c["size"])
            if cache[k] != c["sha256"]:
                return False
        return True

    async def _find_duplicate(self, local_path, size, enc):
        """A stored file (on the drive or in the trash) with the same content and the same
        encryption setting, or None. Only files of exactly the same size are read."""
        if size == 0:
            return None
        cache = {}
        for r in self.db.execute(
                "SELECT * FROM files WHERE size=? AND status='complete' AND encrypted=? "
                "ORDER BY substr(path,1,1)='/' DESC, id DESC LIMIT 8", (size, int(enc))).fetchall():
            if await self._same_content(local_path, size, r, cache):
                return r
        return None

    def duplicates(self):
        """Groups of files on the drive with identical content. 'stored' says how many
        separate copies are in the channel (copies SpiDrive made itself share one)."""
        groups = {}
        for r in self.db.execute(
                "SELECT f.*, (SELECT group_concat(sha256, ',') FROM "
                "(SELECT sha256 FROM chunks WHERE file_id=f.id ORDER BY idx)) AS sig, "
                "(SELECT group_concat(msg_id, ',') FROM "
                "(SELECT msg_id FROM chunks WHERE file_id=f.id ORDER BY idx)) AS msgs "
                f"FROM files f WHERE f.status='complete' AND f.size>0 AND {LIVE.replace('path', 'f.path')}"):
            if not r["sig"] or ",," in "," + r["sig"] + "," or r["sig"].startswith(","):
                continue
            groups.setdefault((r["size"], r["sig"]), []).append(r)
        out = []
        for (size, _), rows in groups.items():
            if len(rows) < 2:
                continue
            stored = len({r["msgs"] for r in rows})
            out.append({"size": size, "stored": stored, "extra_bytes": (stored - 1) * size,
                        "files": [dict(r) for r in sorted(rows, key=lambda r: (r["created"], r["id"]))]})
        out.sort(key=lambda g: (-g["extra_bytes"], -g["size"] * len(g["files"])))
        return out

    async def _upload_chunk(self, local_path, row, idx, n, start, length, key, progress):
        remote, total = row["path"], row["size"]
        if key:
            name = f"{row['id']}_{idx}.bin"           # hide real names when encrypted
            caption = ""
            factory = lambda off: _ctr(key, row["nonce"], off)
        else:
            base = posixpath.basename(remote)
            name = base if n == 1 else f"{base}.part{idx + 1:03d}"
            caption = f"{remote[-900:]}\n[{idx + 1}/{n}]"
            factory = None

        last = [0]

        async def cb(sent, _):
            if progress:
                await _call(progress, start + sent, total)
            await self._throttle(sent - last[0])
            last[0] = sent

        for attempt in range(1, UPLOAD_RETRIES + 1):
            last[0] = 0
            reader = ChunkReader(local_path, start, length, factory)
            try:
                handle = await self.client.upload_file(
                    reader, file_size=length, file_name=name,
                    part_size_kb=512, progress_callback=cb)
                msg = await self.client.send_file(
                    self.channel, handle, force_document=True, caption=caption,
                    attributes=[DocumentAttributeFilename(name)])
                if reader.hashed != length:
                    raise IOError("chunk was not read completely")
                return msg.id, reader.hexdigest()
            except (OSError, ConnectionError, asyncio.TimeoutError) as e:
                if attempt == UPLOAD_RETRIES:
                    raise
                wait = 10 * attempt
                print(f"\n  network error ({e!r}), retrying chunk {idx + 1} in {wait}s ...")
                await asyncio.sleep(wait)
            finally:
                reader.close()

    # ---------- download / streaming

    async def _media(self, msg_id, fresh=False):
        hit = self._media_cache.get(msg_id)
        if hit and not fresh and time.time() - hit[1] < MEDIA_CACHE_SECONDS:
            return hit[0]
        msg = await self.client.get_messages(self.channel, ids=msg_id)
        if msg is None or msg.media is None:
            raise FileNotFoundError(f"message {msg_id} is missing from the channel")
        self._media_cache[msg_id] = (msg.media, time.time())
        return msg.media

    def offline_path(self, file_id):
        return os.path.join(OFFLINE, str(file_id))

    def _local_copy(self, row):
        """Path of the kept copy on this PC, if it is there and complete."""
        p = self.offline_path(row["id"])
        try:
            return p if os.path.getsize(p) == row["size"] else None
        except OSError:
            return None

    @staticmethod
    async def _iter_file(path, offset, end):
        with open(path, "rb") as f:
            f.seek(offset)
            pos = offset
            while pos < end:
                data = await asyncio.to_thread(f.read, min(REQUEST_SIZE, end - pos))
                if not data:
                    break
                pos += len(data)
                yield data

    async def iter_bytes(self, remote, offset=0, length=None):
        """Async generator yielding the (decrypted) bytes of a stored file,
        optionally only a byte range. Used by download and by the servers."""
        row = self.get_file(remote)
        if row is None or row["status"] != "complete":
            raise FileNotFoundError(remote)
        async for data in self.iter_row(row, offset, length):
            yield data

    async def iter_row(self, row, offset=0, length=None, local=True):
        """Like iter_bytes, for a files row (also one in the trash). Reads the copy
        kept on this PC when there is one (unless local=False)."""
        remote = row["path"]
        size = row["size"]
        end = size if length is None else min(size, offset + length)
        if offset >= end:
            return
        local = self._local_copy(row) if local else None
        if local:
            async for data in self._iter_file(local, offset, end):
                yield data
            return
        key = self._get_key() if row["encrypted"] else None

        for ch in self._chunks(row["id"]):
            c_start, c_end = ch["start"], ch["start"] + ch["size"]
            if c_end <= offset or c_start >= end:
                continue
            lo = max(offset, c_start) - c_start
            hi = min(end, c_end) - c_start
            aligned = lo - lo % REQUEST_SIZE              # Telegram wants aligned offsets
            skip, remaining = lo - aligned, hi - lo
            cipher = _ctr(key, row["nonce"], c_start + lo) if key else None
            media = await self._media(ch["msg_id"])
            async for data in self.client.iter_download(
                    media, offset=aligned, request_size=REQUEST_SIZE):
                data = bytes(data)
                if skip:
                    if len(data) <= skip:
                        skip -= len(data)
                        continue
                    data, skip = data[skip:], 0
                if len(data) > remaining:
                    data = data[:remaining]
                if cipher:
                    data = cipher.update(data)
                remaining -= len(data)
                yield data
                if remaining <= 0:
                    break
            if remaining > 0:
                raise IOError(f"chunk {ch['idx']} of {remote} ended early")

    async def download(self, remote, local_path, progress=None):
        """Download a file to disk and verify every chunk's SHA-256."""
        row = self.get_file(remote)
        if row is None or row["status"] != "complete":
            raise FileNotFoundError(remote)
        return await self._download_row(row, local_path, progress)

    async def _download_row(self, row, local_path, progress=None):
        folder = os.path.dirname(os.path.abspath(local_path))
        os.makedirs(folder, exist_ok=True)
        tmp = local_path + ".part"
        done = 0
        try:
            with open(tmp, "wb") as out:
                for ch in self._chunks(row["id"]):
                    h = hashlib.sha256()
                    async for data in self.iter_row(row, ch["start"], ch["size"]):
                        out.write(data)
                        h.update(data)
                        done += len(data)
                        if progress:
                            progress(done, row["size"])
                    if ch["sha256"] and h.hexdigest() != ch["sha256"]:
                        raise IOError(f"checksum mismatch in chunk {ch['idx'] + 1} of {row['path']}")
            os.replace(tmp, local_path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        os.utime(local_path, (row["mtime"], row["mtime"]))
        return local_path

    async def iter_entry(self, entry, offset=0, length=None):
        """Like iter_bytes, but also works for files still waiting to upload
        (those are read from the local copy)."""
        if entry["status"] != "pending":
            row = self.get_file_by_id(entry["id"])
            if row is None or row["status"] != "complete":
                raise FileNotFoundError(entry["path"])
            async for data in self.iter_row(row, offset, length):
                yield data
            return
        end = entry["size"] if length is None else min(entry["size"], offset + length)
        async for data in self._iter_file(entry["tmp"], offset, end):
            yield data

    # ---------- delete

    async def _delete_rows(self, rows):
        """Remove stored files from the index, and their messages from the
        channel unless another file (a copy) still uses them."""
        if not rows:
            return
        gone = {r["id"] for r in rows}
        ids = sorted({c["msg_id"] for r in rows for c in self._chunks(r["id"])})
        shared = set()
        for mid in ids:
            for (fid,) in self.db.execute("SELECT file_id FROM chunks WHERE msg_id=?", (mid,)):
                if fid not in gone:
                    shared.add(mid)
        ids = [m for m in ids if m not in shared]
        for i in range(0, len(ids), 100):
            await self.client.delete_messages(self.channel, ids[i:i + 100])
        for mid in ids:
            self._media_cache.pop(mid, None)
        for fid in gone:
            self.db.execute("DELETE FROM files WHERE id=?", (fid,))
        self.db.commit()

    async def delete(self, path, reason="deleted"):
        """Move a file, or a folder and everything in it, to the trash. Nothing leaves
        Telegram until the trash is emptied (by hand, or after TRASH_DAYS days).
        reason='version' files a replaced file under the versions of its path instead."""
        path = norm(path)
        if path == "/":
            raise ValueError("refusing to delete the whole drive")
        if self.get_entry(path) is not None:
            rows = [r for r in [self.get_file(path)] if r is not None]
            pend = [p for p in [self._pending_row(path)] if p is not None]
            dirs, is_folder = [], False
        elif self.is_dir(path):
            pre = path + "/"
            rows = self.db.execute(
                "SELECT * FROM files WHERE substr(path,1,?)=?", (len(pre), pre)).fetchall()
            pend = self.db.execute(
                "SELECT * FROM pending WHERE substr(path,1,?)=?", (len(pre), pre)).fetchall()
            dirs = [path] + [p for (p,) in self.db.execute(
                "SELECT path FROM dirs WHERE substr(path,1,?)=? ORDER BY path", (len(pre), pre))]
            is_folder = True
        else:
            raise FileNotFoundError(path)
        complete = [r for r in rows if r["status"] == "complete"]
        partial = [r for r in rows if r["status"] != "complete"]
        if not is_folder and complete and pend:
            # an uploaded copy plus a newer one still waiting: the uploaded one is a version
            await self._retire_version(complete.pop(), commit=False)
        batch = self._new_batch(path, is_folder, "version" if reason == "version" and not is_folder else "deleted")
        for r in complete:
            self._trash_row(batch, r)
        for p in pend:
            self._trash_pending(batch, p)
        for d in dirs:
            self.db.execute("INSERT INTO trash_items(batch,orig_path,is_dir) VALUES(?,?,1)", (batch, d))
        if is_folder:
            pre = path + "/"
            self.db.execute("DELETE FROM dirs WHERE path=? OR substr(path,1,?)=?", (path, len(pre), pre))
        self.db.commit()
        await self._delete_rows(partial)                # interrupted uploads are of no use
        self._drop_empty_batches()
        self._keep_wake.set()
        if reason == "version" and not is_folder:
            await self._limit_versions(path)
        return len({r["path"] for r in complete} | {p["path"] for p in pend})

    # ---------- trash and versions

    def _new_batch(self, path, is_dir, reason):
        return self.db.execute(
            "INSERT INTO trash_batches(path,is_dir,deleted,reason) VALUES(?,?,?,?)",
            (path, int(is_dir), time.time(), reason)).lastrowid

    def _trash_row(self, batch, row):
        """Take a stored file off the drive (its messages stay in the channel)."""
        self.db.execute(
            "INSERT INTO trash_items(batch,file_id,orig_path,size,mtime) VALUES(?,?,?,?,?)",
            (batch, row["id"], row["path"], row["size"], row["mtime"]))
        self.db.execute("UPDATE files SET path=?, keep=0 WHERE id=?", (f"trash:{row['id']}", row["id"]))

    def _trash_pending(self, batch, p):
        """Move a file that is still waiting to upload into the local trash folder."""
        self._cancel_if_current(p["id"])
        os.makedirs(TRASH_LOCAL, exist_ok=True)
        dest = os.path.join(TRASH_LOCAL, uuid.uuid4().hex)
        try:
            os.replace(p["tmp"], dest)
        except FileNotFoundError:
            dest = None
        except OSError:                                 # still open (Windows): copy it instead
            try:
                shutil.copy2(p["tmp"], dest)
            except OSError:
                dest = None
            self._discard(p["tmp"])
        self.db.execute("DELETE FROM pending WHERE id=?", (p["id"],))
        if dest:
            self.db.execute(
                "INSERT INTO trash_items(batch,orig_path,tmp,size,mtime) VALUES(?,?,?,?,?)",
                (batch, p["path"], dest, p["size"], p["mtime"]))

    async def _retire_version(self, row, commit=True):
        """Keep a file that is being replaced as an older version of its path."""
        self._trash_row(self._new_batch(row["path"], False, "version"), row)
        if commit:
            self.db.commit()
            await self._limit_versions(row["path"])

    async def _limit_versions(self, path):
        old = [b for (b,) in self.db.execute(
            "SELECT id FROM trash_batches WHERE reason='version' AND path=? ORDER BY deleted DESC, id DESC "
            "LIMIT -1 OFFSET ?", (path, KEEP_VERSIONS))]
        if old:
            await self.purge_batches(old)

    def _drop_empty_batches(self):
        self.db.execute("DELETE FROM trash_batches WHERE id NOT IN (SELECT batch FROM trash_items)")
        self.db.commit()

    def trash_list(self):
        """Deleted items, newest first."""
        out = []
        for b in self.db.execute(
                "SELECT b.*, SUM(CASE WHEN i.is_dir=0 THEN 1 ELSE 0 END) AS files, "
                "COALESCE(SUM(i.size),0) AS bytes, MAX(i.file_id) AS file_id "
                "FROM trash_batches b JOIN trash_items i ON i.batch=b.id "
                "WHERE b.reason='deleted' GROUP BY b.id ORDER BY b.deleted DESC, b.id DESC"):
            out.append({"id": b["id"], "path": b["path"], "name": posixpath.basename(b["path"]),
                        "folder": posixpath.dirname(b["path"]), "is_dir": bool(b["is_dir"]),
                        "deleted": b["deleted"], "expires": b["deleted"] + TRASH_DAYS * 86400,
                        "files": b["files"] or 0, "bytes": b["bytes"],
                        "file_id": None if b["is_dir"] else b["file_id"]})
        return out

    def versions(self, path):
        """Earlier versions of a file, newest first."""
        return [dict(r) for r in self.db.execute(
            "SELECT b.id, b.deleted AS replaced, i.size, i.mtime, i.file_id FROM trash_batches b "
            "JOIN trash_items i ON i.batch=b.id WHERE b.reason='version' AND b.path=? AND i.file_id IS NOT NULL "
            "ORDER BY b.deleted DESC, b.id DESC", (norm(path),))]

    def _batch(self, batch_id):
        b = self.db.execute("SELECT * FROM trash_batches WHERE id=?", (batch_id,)).fetchone()
        if b is None:
            raise FileNotFoundError(f"trash item {batch_id}")
        return b

    async def restore(self, batch_id):
        """Put a deleted item back where it was. Files whose place is taken now come
        back as 'name (2).ext'. Restoring a version makes it the current file again
        (the current one becomes a version). Returns the restored paths."""
        b = self._batch(batch_id)
        if b["reason"] == "version":
            return [await self._restore_version(b)]
        items = self.db.execute("SELECT * FROM trash_items WHERE batch=? ORDER BY is_dir DESC, orig_path",
                                (batch_id,)).fetchall()
        done = []
        for it in items:
            dest = it["orig_path"]
            if it["is_dir"]:
                if not self.get_entry(dest):
                    self.mkdir(dest)
                continue
            dest = self.free_path(dest)
            if it["file_id"] is not None:
                self.db.execute("UPDATE files SET path=? WHERE id=?", (dest, it["file_id"]))
                self.db.commit()
                if posixpath.dirname(dest) != "/":
                    self.mkdir(posixpath.dirname(dest))
                done.append(dest)
            elif it["tmp"] and os.path.exists(it["tmp"]):
                tmp = self.new_staging_path()
                os.replace(it["tmp"], tmp)
                self.db.execute("UPDATE trash_items SET tmp=NULL WHERE id=?", (it["id"],))
                done.append(self.stage(tmp, dest, mtime=it["mtime"]))
        self.db.execute("DELETE FROM trash_batches WHERE id=?", (batch_id,))
        self.db.commit()
        return done

    async def _restore_version(self, b):
        it = self.db.execute("SELECT * FROM trash_items WHERE batch=? AND file_id IS NOT NULL",
                             (b["id"],)).fetchone()
        if it is None:
            raise FileNotFoundError(f"version {b['id']}")
        path = b["path"]
        if self._pending_row(path) is not None:
            raise ValueError("A newer copy of this file is still uploading. Try again when it is done.")
        if self.is_dir(path) and self.get_entry(path) is None:
            raise IsADirectoryError(path)
        self.db.execute("DELETE FROM trash_batches WHERE id=?", (b["id"],))
        current = self.get_file(path)
        if current is not None:
            if current["status"] == "complete":
                self._trash_row(self._new_batch(path, False, "version"), current)
            else:
                self.db.commit()
                await self._delete_rows([current])
        self.db.execute("UPDATE files SET path=? WHERE id=?", (path, it["file_id"]))
        self.db.commit()
        if posixpath.dirname(path) != "/":
            self.mkdir(posixpath.dirname(path))
        await self._limit_versions(path)
        return path

    async def purge_batches(self, batch_ids):
        """Delete trash items for good (their messages leave the channel unless a
        copy still uses them)."""
        ids = list(batch_ids)
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        items = self.db.execute(f"SELECT * FROM trash_items WHERE batch IN ({marks})", ids).fetchall()
        fids = [i["file_id"] for i in items if i["file_id"] is not None]
        rows = []
        for i in range(0, len(fids), 500):
            part = fids[i:i + 500]
            rows += self.db.execute(
                f"SELECT * FROM files WHERE id IN ({','.join('?' * len(part))})", part).fetchall()
        await self._delete_rows(rows)
        for it in items:
            if it["tmp"]:
                self._discard(it["tmp"])
        self.db.execute(f"DELETE FROM trash_batches WHERE id IN ({marks})", ids)
        self.db.commit()
        return sum(1 for i in items if not i["is_dir"])

    async def empty_trash(self):
        ids = [b for (b,) in self.db.execute("SELECT id FROM trash_batches WHERE reason='deleted'")]
        return await self.purge_batches(ids)

    async def purge_expired(self, days=None):
        """Empty trash items and versions older than TRASH_DAYS. Returns what was removed."""
        days = TRASH_DAYS if days is None else days
        cutoff = time.time() - days * 86400
        old = self.db.execute("SELECT id, path, reason FROM trash_batches WHERE deleted<?", (cutoff,)).fetchall()
        await self.purge_batches([b["id"] for b in old])
        self._drop_empty_batches()
        self.db.execute("DELETE FROM links WHERE expires<?", (time.time() - 86400,))
        self.db.commit()
        return [f"{'old version of ' if b['reason'] == 'version' else ''}{b['path']}" for b in old]

    # ---------- keep on this PC

    def set_keep(self, paths, keep=True):
        """Keep files (or everything in folders) on this PC, or free up that space."""
        n = 0
        for path in paths:
            path = norm(path)
            pre = "/" if path == "/" else path + "/"
            for table in ("files", "pending"):
                n += self.db.execute(
                    f"UPDATE {table} SET keep=? WHERE (path=? OR substr(path,1,?)=?) AND {LIVE}",
                    (int(bool(keep)), path, len(pre), pre)).rowcount
        self.db.commit()
        self._keep_wake.set()
        return n

    def is_local(self, entry):
        """True when the file opens without Telegram (waiting upload, or kept copy ready)."""
        if entry["status"] == "pending":
            return True
        return entry["status"] == "complete" and entry.get("keep") and self._local_copy(entry) is not None

    async def keep_worker(self, log=print):
        """Runs forever: downloads files marked 'keep on this PC' and removes copies
        that are no longer wanted."""
        while True:
            self._keep_wake.clear()
            try:
                await self._keep_once(log)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"keep on this PC: {e!r}")
            try:
                await asyncio.wait_for(self._keep_wake.wait(), 60)
            except asyncio.TimeoutError:
                pass

    async def _keep_once(self, log=print):
        os.makedirs(OFFLINE, exist_ok=True)
        wanted = {r["id"]: r for r in self.db.execute(
            f"SELECT * FROM files WHERE keep=1 AND status='complete' AND {LIVE}")}
        for name in os.listdir(OFFLINE):
            fid = name.split(".")[0]
            if not fid.isdigit() or int(fid) not in wanted:
                self._discard(os.path.join(OFFLINE, name))
        for fid, row in wanted.items():
            fresh = self.get_file_by_id(fid)
            if fresh is None or not fresh["keep"] or not fresh["path"].startswith("/"):
                continue
            if self._local_copy(fresh):
                continue
            prog = self.keeping[fid] = {"path": fresh["path"], "done": 0, "total": fresh["size"]}
            try:
                await self._download_row(fresh, self.offline_path(fid),
                                         progress=lambda d, t, prog=prog: prog.update(done=d))
                log(f"kept on this PC: {fresh['path']}")
            finally:
                self.keeping.pop(fid, None)
            after = self.get_file_by_id(fid)
            if after is None or not after["keep"]:
                self._discard(self.offline_path(fid))

    # ---------- photo previews

    def thumb_key(self, entry):
        if entry.get("status") != "complete":
            return None
        c = self.db.execute("SELECT msg_id, sha256 FROM chunks WHERE file_id=? AND idx=0",
                            (entry["id"],)).fetchone()
        if c is None:
            return None
        return c["sha256"] or f"msg:{c['msg_id']}"

    def _thumb_row(self, key):
        return self.db.execute("SELECT * FROM th.thumbs WHERE key=?", (key,)).fetchone()

    def _save_thumb(self, key, data, taken=None, failed=False):
        before = self.db.total_changes
        self.db.execute("INSERT OR REPLACE INTO th.thumbs(key,data,taken,failed) VALUES(?,?,?,?)",
                        (key, data, taken, int(failed)))
        self.db.commit()
        self._thumb_changes += self.db.total_changes - before

    async def thumbnail(self, entry):
        """A small JPEG preview of a photo (or of a video sent from Telegram), or None.
        Made once and stored in thumbs.db."""
        kind = search.kind_of(entry["path"])
        if kind not in ("image", "video"):
            return None
        if entry["status"] == "pending":
            if kind != "image" or not thumbs.available():
                return None
            hit = self._pending_thumbs.get(entry["tmp"])
            if hit is None:
                try:
                    hit = (await asyncio.to_thread(thumbs.make, entry["tmp"]))[0]
                except Exception:
                    hit = b""
                if len(self._pending_thumbs) > 300:
                    self._pending_thumbs.clear()
                self._pending_thumbs[entry["tmp"]] = hit
            return hit or None
        key = self.thumb_key(entry)
        if key is None or not thumbs.available():
            return None
        r = self._thumb_row(key)
        if r is not None:
            return r["data"]
        async with self._thumb_sem:
            r = self._thumb_row(key)
            if r is not None:
                return r["data"]
            row = self.get_file_by_id(entry["id"])
            if row is None:
                return None
            sources = []
            if row["origin"] == "telegram":            # Telegram already has a preview
                c = self._chunks(row["id"])
                msg = await self.client.get_messages(self.channel, ids=c[0]["msg_id"]) if c else None
                raw = await self.client.download_media(msg, file=bytes, thumb=-1) if msg else None
                if raw:
                    sources.append(raw)
            if kind == "image" and row["size"] <= thumbs.SOURCE_LIMIT and not sources:
                sources.append(b"".join([d async for d in self.iter_row(row)]))
            data = taken = None
            for src in sources:
                try:
                    data, taken = await asyncio.to_thread(thumbs.make, src)
                    break
                except Exception:
                    continue
            self._save_thumb(key, data, taken, failed=data is None)
            return data

    def photos(self):
        """Photos and videos on the drive, newest first, with the date they were taken
        when known (else the file date)."""
        out = []
        for r in self.db.execute(
                "SELECT f.id, f.path, f.size, f.mtime, f.created, f.status, f.encrypted, f.origin, f.keep, "
                "t.taken, t.failed FROM files f "
                "LEFT JOIN chunks c ON c.file_id=f.id AND c.idx=0 "
                "LEFT JOIN th.thumbs t ON t.key = CASE WHEN c.sha256='' THEN 'msg:'||c.msg_id ELSE c.sha256 END "
                "WHERE f.status='complete' AND substr(f.path,1,1)='/'"):
            kind = search.kind_of(r["path"])
            if kind in ("image", "video"):
                e = dict(r)
                e["kind"], e["date"] = kind, r["taken"] or r["mtime"]
                out.append(e)
        for p in self.pending():
            kind = search.kind_of(p["path"])
            if kind in ("image", "video"):
                e = self._pending_entry(p)
                e["kind"], e["date"], e["taken"] = kind, p["mtime"], None
                out.append(e)
        out.sort(key=lambda e: e["date"], reverse=True)
        return out

    # ---------- Telegram -> drive

    INBOX = "/Telegram Inbox"

    def _inbox_folder(self, msg, name):
        """Where a file sent from Telegram goes: Photos/2026-11, Videos/2026-11, Voice,
        Music, Docs or Other inside the Telegram Inbox (or the Inbox itself when
        INBOX_ORGANIZE is off)."""
        if not getattr(config, "INBOX_ORGANIZE", True):
            return self.INBOX
        month = time.strftime("%Y-%m", time.localtime(msg.date.timestamp()))
        if getattr(msg, "voice", None) is not None:
            return f"{self.INBOX}/Voice"
        kind = "image" if isinstance(msg.media, MessageMediaPhoto) else search.kind_of(name)
        if kind == "image":
            return f"{self.INBOX}/Photos/{month}"
        if kind == "video":
            return f"{self.INBOX}/Videos/{month}"
        return f"{self.INBOX}/" + {"audio": "Music", "document": "Docs", "archive": "Docs"}.get(kind, "Other")
    _OWN_CAPTION = re.compile(r"\n\[\d+/\d+\]$")
    _OWN_ENC_NAME = re.compile(r"^\d+_\d+\.bin$")

    async def import_new_messages(self, min_age=20):
        """Add files that were sent or forwarded to the channel from Telegram
        (not by SpiDrive) to the drive, in /Telegram Inbox. Returns new paths."""
        last = int(self._meta("last_scanned") or 0)
        newest, imported = last, []
        async for msg in self.client.iter_messages(self.channel, min_id=last, reverse=True):
            if time.time() - msg.date.timestamp() < min_age:
                break                                   # too fresh: may be our own upload in progress
            newest = msg.id
            if not isinstance(msg.media, (MessageMediaDocument, MessageMediaPhoto)) or msg.file is None:
                continue
            caption = msg.message or ""
            if caption.startswith("#index_backup") or self._OWN_CAPTION.search(caption):
                continue
            if self.db.execute("SELECT 1 FROM chunks WHERE msg_id=?", (msg.id,)).fetchone():
                continue
            name = msg.file.name
            if name and self._OWN_ENC_NAME.match(name) and not caption:
                continue                                # leftover piece of an interrupted encrypted upload
            if not name:
                kind = "photo" if isinstance(msg.media, MessageMediaPhoto) else (
                    "voice" if getattr(msg, "voice", None) is not None else "file")
                name = f"{kind}_{msg.date:%Y%m%d_%H%M%S}_{msg.id}{msg.file.ext or ''}"
            name = name.replace("/", "_").replace("\\", "_")
            size = int(msg.file.size or 0)
            folder = self._inbox_folder(msg, name)
            path = self.free_path(f"{folder}/{name}")
            cur = self.db.execute(
                "INSERT INTO files(path,size,mtime,created,encrypted,nonce,chunk_size,status,origin) "
                "VALUES(?,?,?,?,0,NULL,?,'complete','telegram')",
                (path, size, msg.date.timestamp(), time.time(), max(size, 1)))
            if size:
                self.db.execute(
                    "INSERT INTO chunks(file_id,idx,msg_id,start,size,sha256) VALUES(?,0,?,0,?,'')",
                    (cur.lastrowid, msg.id, size))
            self.db.commit()
            if folder != self.INBOX:
                self.mkdir(folder)
            imported.append(path)
        if newest != last:
            self._set_meta("last_scanned", str(newest))
        if imported:
            self.mkdir(self.INBOX)
        return imported

    async def prune_missing(self):
        """Remove files whose messages were deleted in Telegram. Returns removed paths."""
        ids = [r[0] for r in self.db.execute("SELECT DISTINCT msg_id FROM chunks")]
        missing = set()
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            msgs = await self.client.get_messages(self.channel, ids=batch)
            for mid, m in zip(batch, msgs):
                if getattr(m, "media", None) is None:
                    missing.add(mid)
        if not missing:
            return []
        if len(missing) > 5 and len(missing) > len(ids) / 2:
            raise RuntimeError(f"{len(missing)} of {len(ids)} messages look missing; "
                               "not pruning (check the CHANNEL id).")
        marks = ",".join("?" * len(missing))
        rows = self.db.execute(
            f"SELECT DISTINCT f.* FROM files f JOIN chunks c ON c.file_id=f.id "
            f"WHERE c.msg_id IN ({marks})", tuple(missing)).fetchall()
        await self._delete_rows(rows)                   # also removes the file's other chunks
        return [r["path"] for r in rows]

    # ---------- index backup

    async def backup_index(self):
        """Send a copy of index.db to the channel. Without the index, the channel is just
        a pile of chunks, so this runs every day. With a passphrase set, the copy is
        encrypted (see keys.py). Only the newest BACKUP_KEEP copies are kept."""
        self.db.commit()
        tmp = os.path.join(DATA_DIR, "index_backup.db")
        bk = sqlite3.connect(tmp)
        self.db.backup(bk)
        bk.close()
        encrypted = bool(config.PASSPHRASE)
        try:
            if encrypted:
                master = self._get_key()
                wraps = {"pass": (self._meta("wrap_pass_salt"), self._meta("wrap_pass"))}
                if self._meta("wrap_recovery") is not None:
                    wraps["recovery"] = (self._meta("wrap_recovery_salt"), self._meta("wrap_recovery"))
                with open(tmp, "rb") as f:
                    blob = keys.encrypt_backup(f.read(), master, wraps)
                os.remove(tmp)
                tmp = os.path.join(DATA_DIR, "index_backup.spidrive")
                with open(tmp, "wb") as f:
                    f.write(blob)
            msg = await self.client.send_file(
                self.channel, tmp, force_document=True,
                caption=f"#index_backup {time.strftime('%Y-%m-%d %H:%M')}" + (" encrypted" if encrypted else ""))
        finally:
            os.remove(tmp)
        self._set_meta("last_backup", str(time.time()))
        self._set_meta("last_backup_encrypted", "1" if encrypted else "0")
        await self._prune_backups()
        return msg.id

    async def _prune_backups(self):
        """Delete all but the newest BACKUP_KEEP index backups from the channel."""
        found = []
        async for m in self.client.iter_messages(self.channel, search="#index_backup", limit=200):
            if getattr(m, "file", None) is not None and (m.message or "").startswith("#index_backup"):
                found.append(m.id)
        old = sorted(found, reverse=True)[BACKUP_KEEP:]
        if old:
            await self.client.delete_messages(self.channel, old)
        return len(old)

    # ---------- drive health

    async def health_check(self, budget=HEALTH_BUDGET, log=print):
        """Download a few random parts again (ones not checked recently first) and compare
        them with the SHA-256 stored when they were uploaded. Returns the problems found."""
        if self.health_running:
            return []
        self.health_running = True
        problems, used = [], 0
        try:
            cands = self.db.execute(
                "SELECT c.file_id, c.idx, c.start, c.size, c.sha256 FROM chunks c "
                "JOIN files f ON f.id=c.file_id "
                "LEFT JOIN health h ON h.file_id=c.file_id AND h.idx=c.idx "
                "WHERE f.status='complete' AND c.sha256!='' "
                "ORDER BY h.checked IS NOT NULL, h.checked < ? DESC, random() LIMIT 40",
                (time.time() - 30 * 86400,)).fetchall()
            for c in cands:
                if used and used + c["size"] > budget:
                    continue
                row = self.get_file_by_id(c["file_id"])
                if row is None:
                    continue
                h, error = hashlib.sha256(), None
                try:
                    async for data in self.iter_row(row, c["start"], c["size"], local=False):
                        h.update(data)
                    if h.hexdigest() != c["sha256"]:
                        error = "The copy in Telegram does not match what was uploaded (checksum mismatch)."
                except FileNotFoundError:
                    error = "This part is missing from the channel (its message was deleted)."
                except IOError as e:
                    error = f"Could not be read completely: {e}"
                used += c["size"]
                self.db.execute(
                    "INSERT OR REPLACE INTO health(file_id,idx,checked,ok,error) VALUES(?,?,?,?,?)",
                    (c["file_id"], c["idx"], time.time(), int(error is None), error))
                self.db.commit()
                if error:
                    problems.append(f"{row['path']}: {error}")
                    log(f"health: {row['path']} part {c['idx'] + 1}: {error}")
            self._set_meta("health_last", str(time.time()))
            return problems
        finally:
            self.health_running = False

    def health(self):
        """The 'drive health' card: how much was verified lately, and any problems."""
        since = time.time() - 30 * 86400
        total = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(c.size),0) FROM chunks c JOIN files f ON f.id=c.file_id "
            f"WHERE f.status='complete' AND c.sha256!='' AND {LIVE.replace('path', 'f.path')}").fetchone()
        ok = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(c.size),0) FROM chunks c JOIN files f ON f.id=c.file_id "
            "JOIN health h ON h.file_id=c.file_id AND h.idx=c.idx "
            f"WHERE h.ok=1 AND h.checked>=? AND {LIVE.replace('path', 'f.path')}", (since,)).fetchone()
        problems = [{"path": r["path"], "part": r["idx"] + 1, "error": r["error"], "checked": r["checked"],
                     "file_id": r["file_id"]}
                    for r in self.db.execute(
                        "SELECT f.path, h.* FROM health h JOIN files f ON f.id=h.file_id "
                        f"WHERE h.ok=0 AND {LIVE.replace('path', 'f.path')} ORDER BY h.checked DESC")]
        last = self._meta("health_last")
        return {"parts": total[0], "bytes": total[1], "verified_parts": ok[0], "verified_bytes": ok[1],
                "percent": round(100 * ok[1] / total[1], 1) if total[1] else 100.0,
                "last_check": float(last) if last else None, "problems": problems,
                "running": self.health_running,
                "status": "problem" if problems else ("good" if last else "not checked yet")}

    # ---------- share links (served on the Wi-Fi by server.py)

    def create_link(self, path, hours):
        e = self.get_entry(path)
        if e is None or e["status"] not in ("complete", "pending"):
            raise FileNotFoundError(path)
        token = secrets.token_urlsafe(9)
        expires = time.time() + max(0.25, min(float(hours), 24 * 30)) * 3600
        self.db.execute("INSERT INTO links(token,path,file_id,created,expires) VALUES(?,?,?,?,?)",
                        (token, norm(path), e["id"] if e["id"] > 0 else None, time.time(), expires))
        self.db.commit()
        return token, expires

    def links(self, path=None):
        now = time.time()
        if path is None:
            rows = self.db.execute("SELECT * FROM links WHERE expires>? ORDER BY created DESC", (now,))
        else:
            rows = self.db.execute("SELECT * FROM links WHERE expires>? AND path=? ORDER BY created DESC",
                                   (now, norm(path)))
        return [dict(r) for r in rows]

    def revoke_link(self, token):
        self.db.execute("DELETE FROM links WHERE token=?", (token,))
        self.db.commit()

    def resolve_link(self, token):
        """The file a link points to, or None if the link is unknown or expired. Follows
        the file when it is renamed or moved."""
        r = self.db.execute("SELECT * FROM links WHERE token=?", (token,)).fetchone()
        if r is None or r["expires"] <= time.time():
            return None, r
        e = self.get_entry(r["path"])
        if e is None and r["file_id"]:
            row = self.get_file_by_id(r["file_id"])
            if row is not None and row["path"].startswith("/"):
                e = dict(row)
        return e, r

    def link_used(self, token):
        self.db.execute("UPDATE links SET downloads=downloads+1 WHERE token=?", (token,))
        self.db.commit()
