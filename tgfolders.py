"""
Drive folders, visible in the Telegram channel.

Telegram channels have no folders, so SpiDrive keeps one message per folder:

    📌 📁 My Drive                      (pinned)
        📁 Photos →                    tap: jumps to the Photos message
        📄 notes.txt · 12 KB →         tap: jumps to the file itself

    📁 Photos
        ⬆️ My Drive
        📁 2026 Trip →
        🖼 IMG_0001.jpg · 2.1 MB →

Every minute SpiDrive works out what each folder message should say and edits the
ones that changed (a few per minute, so Telegram doesn't slow the account down).
Big folders take several messages, linked with Next / Previous. Folders that no
longer exist lose their message.

Files that are encrypted or split into parts are listed by name but not linked,
because their messages can't be opened in Telegram; send /get <name> in Saved
Messages to receive them (phone.py). Files still uploading appear once they are in
Telegram.

Turn it off with TELEGRAM_FOLDERS = False (Settings, Telegram): the folder messages
are then deleted from the channel.
"""
import hashlib
import html
import posixpath
import time

from search import kind_of

ICONS = {"image": "🖼", "video": "🎬", "audio": "🎵", "document": "📄", "archive": "🗜", "other": "📎"}
PAGE_CHARS = 3300          # Telegram allows 4096 characters per message
PAGE_LINKS = 60            # keep the number of links per message modest
EDITS_PER_RUN = 15         # messages sent or edited per run (the rest follow next minute)
VERIFY_SECONDS = 600       # how often to check that nobody deleted a folder message


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class FolderMessages:
    def __init__(self, drive, log=print):
        self.d = drive
        self.log = log
        self._last_verify = 0.0
        drive.db.executescript("""
        CREATE TABLE IF NOT EXISTS tg_folders(
            path   TEXT NOT NULL,
            page   INTEGER NOT NULL,
            msg_id INTEGER NOT NULL,
            digest TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(path, page)
        );
        """)
        drive.db.commit()

    # ---------- what the folders contain

    def _tree(self):
        """{folder: ([subfolder names], [file rows])} for every folder on the drive."""
        folders = {"/": (set(), [])}

        def add_folder(p):
            while p != "/":
                folders.setdefault(p, (set(), []))
                parent = posixpath.dirname(p)
                siblings = folders.setdefault(parent, (set(), []))[0]
                if posixpath.basename(p) in siblings:
                    break                                 # the rest of the way up is known already
                siblings.add(posixpath.basename(p))
                p = parent

        for (p,) in self.d.db.execute("SELECT path FROM dirs"):
            if p.startswith("/") and p != "/":
                add_folder(p)
        for r in self.d.db.execute(
                "SELECT f.id, f.path, f.size, f.encrypted, f.origin, "
                "(SELECT COUNT(*) FROM chunks c WHERE c.file_id=f.id) AS parts, "
                "(SELECT msg_id FROM chunks c WHERE c.file_id=f.id AND c.idx=0) AS first_msg "
                "FROM files f WHERE f.status='complete' AND substr(f.path,1,1)='/'"):
            parent = posixpath.dirname(r["path"])
            if parent != "/":
                add_folder(parent)
            folders[parent][1].append(r)
        return {p: (sorted(d, key=str.casefold), sorted(f, key=lambda r: r["path"].casefold()))
                for p, (d, f) in folders.items()}

    def _link(self, msg_id):
        cid = str(self.d.channel_id()).replace("-100", "", 1).lstrip("-")
        return f"https://t.me/c/{cid}/{msg_id}"

    @staticmethod
    def _title(path):
        return "My Drive" if path == "/" else path.strip("/")

    def _entries(self, path, subdirs, files):
        """[(plain text, html)] lines for one folder, before paging. Folder links are
        filled in later ({dir:name} placeholders), once every folder has a message."""
        out = []
        for name in subdirs:
            child = posixpath.join(path, name)
            out.append((f"📁 {name}", ("dir", child, name)))
        for r in files:
            name = posixpath.basename(r["path"])
            icon = ICONS[kind_of(name)]
            openable = r["first_msg"] and r["parts"] == 1 and not r["encrypted"]
            extra = (" 🔒" if r["encrypted"] else "") + (f" 🧩{r['parts']}" if r["parts"] > 1 else "")
            out.append((f"{icon} {name} · {human(r['size'])}{extra}",
                        ("file", r["first_msg"] if openable else None, name, icon, human(r["size"]), extra)))
        return out

    def _pages(self, entries):
        pages, cur, chars, links = [], [], 0, 0
        for plain, spec in entries:
            if cur and (chars + len(plain) + 1 > PAGE_CHARS or links >= PAGE_LINKS):
                pages.append(cur)
                cur, chars, links = [], 0, 0
            cur.append(spec)
            chars += len(plain) + 1
            links += 1
        pages.append(cur)
        return pages

    def _render(self, path, page_no, pages, ids, n_dirs, n_files, size, hidden):
        """The message text (HTML) for one page of a folder."""
        e = html.escape
        lines = [f"📁 <b>{e(self._title(path))}</b>"]
        if path != "/":
            parent = posixpath.dirname(path)
            up = ids.get((parent, 0))
            lines.append(f"⬆️ <a href=\"{self._link(up)}\">{e(self._title(parent))}</a>" if up
                         else f"⬆️ {e(self._title(parent))}")
        summary = [f"{n_dirs} folder{'s' if n_dirs != 1 else ''}" if n_dirs else "",
                   f"{n_files} file{'s' if n_files != 1 else ''}" if n_files else "", human(size) if n_files else ""]
        lines.append("<i>" + (" · ".join(x for x in summary if x) or "empty") +
                     (f" · page {page_no + 1} of {len(pages)}" if len(pages) > 1 else "") + "</i>")
        lines.append("")
        for spec in pages[page_no]:
            if spec[0] == "dir":
                _, child, name = spec
                mid = ids.get((child, 0))
                lines.append(f"📁 <a href=\"{self._link(mid)}\">{e(name)}</a>" if mid else f"📁 {e(name)}")
            else:
                _, mid, name, icon, size_text, extra = spec
                label = f"<a href=\"{self._link(mid)}\">{e(name)}</a>" if mid else e(name)
                lines.append(f"{icon} {label} · {size_text}{extra}")
        nav = []
        if page_no > 0 and ids.get((path, page_no - 1)):
            nav.append(f"<a href=\"{self._link(ids[(path, page_no - 1)])}\">⬅️ Previous</a>")
        if page_no + 1 < len(pages) and ids.get((path, page_no + 1)):
            nav.append(f"<a href=\"{self._link(ids[(path, page_no + 1)])}\">Next ➡️</a>")
        if nav:
            lines += ["", "   ".join(nav)]
        if hidden:
            lines += ["", "<i>🔒 encrypted and 🧩 split files can't open here: send /get and the name "
                          "to your Saved Messages to receive them.</i>"]
        return "\n".join(lines)

    # ---------- keeping the channel up to date

    async def run(self, enabled=True, budget=EDITS_PER_RUN):
        """One round: create, edit or delete folder messages. Returns what changed."""
        db, c, ch = self.d.db, self.d.client, self.d.channel
        stored = {(r["path"], r["page"]): (r["msg_id"], r["digest"]) for r in db.execute("SELECT * FROM tg_folders")}
        if not enabled:
            if stored:
                await c.delete_messages(ch, [m for m, _ in stored.values()])
                db.execute("DELETE FROM tg_folders")
                db.commit()
                return ["folder messages removed from the channel"]
            return []
        if time.time() - self._last_verify > VERIFY_SECONDS and stored:
            await self._verify(stored)
            stored = {(r["path"], r["page"]): (r["msg_id"], r["digest"])
                      for r in db.execute("SELECT * FROM tg_folders")}
        tree = self._tree()
        plan = {}                                     # path -> (pages, counts)
        for path, (subdirs, files) in tree.items():
            entries = self._entries(path, subdirs, files)
            hidden = any(e[1][0] == "file" and e[1][1] is None for e in entries)
            plan[path] = (self._pages(entries), len(subdirs), len(files), sum(r["size"] for r in files), hidden)
        wanted = [(p, i) for p in sorted(plan, key=lambda p: (p.count("/") if p != "/" else 0, p))
                  for i in range(len(plan[p][0]))]
        done, ids = [], {k: v[0] for k, v in stored.items()}

        # 1. a message for every folder page (parents first, so the root is the oldest)
        for key in wanted:
            if key in ids:
                continue
            if budget <= 0:
                break
            msg = await c.send_message(ch, f"📁 {html.escape(self._title(key[0]))}", parse_mode="html",
                                       link_preview=False, silent=True)
            ids[key] = msg.id
            db.execute("INSERT OR REPLACE INTO tg_folders(path,page,msg_id,digest) VALUES(?,?,?,'')",
                       (key[0], key[1], msg.id))
            db.commit()
            budget -= 1
            if key == ("/", 0):
                try:
                    await c.pin_message(ch, msg.id, notify=False)
                except Exception as e:                  # not fatal: the message is still there
                    self.log(f"telegram folders: could not pin My Drive ({e!r})")
            done.append(f"new folder message: {self._title(key[0])}")

        # 2. bring each message up to date
        for key in wanted:
            if budget <= 0:
                break
            if key not in ids:
                continue
            pages, n_dirs, n_files, size, hidden = plan[key[0]]
            text = self._render(key[0], key[1], pages, ids, n_dirs, n_files, size, hidden)
            digest = hashlib.sha1(text.encode()).hexdigest()
            old = stored.get(key, (None, ""))[1]
            row = db.execute("SELECT digest FROM tg_folders WHERE path=? AND page=?", key).fetchone()
            if row and row["digest"] == digest:
                continue
            try:
                await c.edit_message(ch, ids[key], text, parse_mode="html", link_preview=False)
            except Exception as e:
                name = type(e).__name__
                if name == "MessageNotModifiedError":
                    pass
                elif name in ("MessageIdInvalidError", "MessageAuthorRequiredError") or "not found" in str(e).lower():
                    # deleted in Telegram: make it again next round (the parent then links the new one)
                    db.execute("DELETE FROM tg_folders WHERE path=? AND page=?", key)
                    db.commit()
                    budget -= 1
                    continue
                else:
                    raise
            db.execute("UPDATE tg_folders SET digest=? WHERE path=? AND page=?", (digest, key[0], key[1]))
            db.commit()
            budget -= 1
            if old:
                done.append(f"updated folder message: {self._title(key[0])}")

        # 3. folders (or pages) that are gone
        gone = [k for k in stored if k not in set(wanted)]
        if gone and budget > 0:
            await c.delete_messages(ch, [stored[k][0] for k in gone])
            for k in gone:
                db.execute("DELETE FROM tg_folders WHERE path=? AND page=?", k)
            db.commit()
            done.append(f"removed {len(gone)} folder message(s)")
        return done

    async def _verify(self, stored):
        """Forget folder messages that were deleted in Telegram, so they are made again
        (and their parent links the new one)."""
        self._last_verify = time.time()
        keys = list(stored)
        for i in range(0, len(keys), 100):
            part = keys[i:i + 100]
            msgs = await self.d.client.get_messages(self.d.channel, ids=[stored[k][0] for k in part])
            for k, m in zip(part, msgs):
                if m is None or type(m).__name__ == "MessageEmpty":
                    self.d.db.execute("DELETE FROM tg_folders WHERE path=? AND page=?", k)
        self.d.db.commit()

    def root_link(self):
        r = self.d.db.execute("SELECT msg_id FROM tg_folders WHERE path='/' AND page=0").fetchone()
        return self._link(r["msg_id"]) if r else None
