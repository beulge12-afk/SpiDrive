"""
Control SpiDrive from your phone through Telegram's Saved Messages.

Send one of these to yourself (in Saved Messages) and SpiDrive, running on your PC,
answers there:

    /find <words or filters>   search the drive (same search as the dashboard)
    /get <number | /path | words>   send that file to Saved Messages
    /keep <number | /path | words>  keep it on the PC (opens without Telegram)
    /stats                     how much is stored, what is uploading, drive health
    /digest                    this week's summary (also sent by itself once a week)
    /help                      this list

Nothing listens on the network: the commands travel through your own Telegram
account, so it works from anywhere, with no open ports. Only messages in your own
Saved Messages count; nobody else can send commands. Commands sent while the PC is
off are not answered later.
"""
import posixpath
import re
import time

HELP = ("📁 SpiDrive commands (send them here, in Saved Messages):\n\n"
        "/find report 2026 — search (filters work too: /find type:video size:>1gb)\n"
        "/get 2 — send result number 2 from the last /find\n"
        "/get /Docs/report.pdf — send a file by its path\n"
        "/keep 2 — keep that file on the PC\n"
        "/stats — storage, uploads and drive health\n"
        "/digest — this week's summary\n"
        "/help — this list")
COMMAND = re.compile(r"^/(\w+)(?:@\w+)?(?:\s+(.*))?$", re.S)


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class Phone:
    def __init__(self, drive, send_file, log=print):
        """send_file(path) sends a drive file to Saved Messages (server.share_file)."""
        self.d = drive
        self.send_file = send_file
        self.log = log
        self.last = []                    # paths from the last /find, for /get 2

    def attach(self):
        """Start answering commands in Saved Messages."""
        from telethon import events
        self.d.on_event(self.on_message, events.NewMessage(chats="me", outgoing=True, pattern=r"^/\w"))

    async def on_message(self, event):
        try:
            reply = await self.handle(event.raw_text or "")
        except Exception as e:                          # never let one command stop the listener
            reply = f"⚠️ {e}"
        if reply:
            await event.respond(reply)

    async def handle(self, text):
        m = COMMAND.match(text.strip())
        if not m:
            return None
        cmd, arg = m.group(1).lower(), (m.group(2) or "").strip()
        self.log(f"phone: /{cmd} {arg}".rstrip())
        if cmd in ("help", "start"):
            return HELP
        if cmd == "find":
            return self.find(arg)
        if cmd == "get":
            return await self.get(arg)
        if cmd == "keep":
            return self.keep(arg)
        if cmd == "stats":
            return self.stats()
        if cmd == "digest":
            import digest
            return digest.weekly_text(self.d)
        return f"❓ Unknown command /{cmd}. Send /help for the list."

    # ---------- commands

    def find(self, query):
        if not query:
            return "🔎 What should I look for? For example: /find invoice 2026"
        hits, errors = self.d.search(query, limit=200)
        if errors:
            return "⚠️ " + "\n".join(errors)
        if not hits:
            return f"🔎 Nothing found for “{query}”."
        self.last = [h["path"] for h in hits[:20]]
        lines = [f"{i}. {posixpath.basename(h['path'])} — {human(h['size'])}\n    {posixpath.dirname(h['path'])}"
                 for i, h in enumerate(hits[:20], 1)]
        more = f"\n…and {len(hits) - 20} more. Add words or filters to narrow it down." if len(hits) > 20 else ""
        return f"🔎 {len(hits)} found:\n\n" + "\n".join(lines) + more + "\n\nSend /get 1 to receive the first one."

    def _resolve(self, arg):
        """A number from the last /find, a /path, or words that match exactly one file."""
        if not arg:
            raise ValueError("Which file? Send /find first, then /get 1 (or /get /path/to/file).")
        if arg.isdigit():
            i = int(arg)
            if not self.last:
                raise ValueError("Send /find first, then pick a number from its list.")
            if not 1 <= i <= len(self.last):
                raise ValueError(f"Pick a number from 1 to {len(self.last)}.")
            path = self.last[i - 1]
            if self.d.get_entry(path) is None:
                raise ValueError(f"{path} is not on the drive any more.")
            return path
        if arg.startswith("/"):
            if self.d.get_entry(arg) is None:
                raise ValueError(f"There is no file {arg} on the drive.")
            return arg
        hits, _ = self.d.search(arg, limit=50)
        if len(hits) == 1:
            return hits[0]["path"]
        if not hits:
            raise ValueError(f"Nothing found for “{arg}”.")
        self.last = [h["path"] for h in hits[:20]]
        names = "\n".join(f"{i}. {posixpath.basename(h['path'])}" for i, h in enumerate(hits[:5], 1))
        raise ValueError(f"{len(hits)} files match. Which one?\n\n{names}\n\nSend /get with its number.")

    async def get(self, arg):
        try:
            path = self._resolve(arg)
        except ValueError as e:
            return f"📄 {e}"
        e = self.d.get_entry(path)
        await self.send_file(path)
        return None if e["size"] < 20 * 1024 * 1024 else f"📤 Sending {posixpath.basename(path)} ({human(e['size'])})."

    def keep(self, arg):
        try:
            path = self._resolve(arg)
        except ValueError as e:
            return f"📄 {e}"
        self.d.set_keep([path])
        return f"💾 Keeping {posixpath.basename(path)} on the PC. It opens without Telegram once it is downloaded."

    def stats(self):
        s, h = self.d.stats(), self.d.health()
        lines = [f"📊 {s['files']} files, {human(s['bytes'])}"]
        if s["pending"]:
            lines.append(f"⬆️ {s['pending']} waiting to upload ({human(s['pending_bytes'])})"
                         + (" — paused" if self.d.paused else ""))
        if s["trash"]:
            lines.append(f"🗑 Trash: {s['trash']} items, {human(s['trash_bytes'])}")
        status = {"good": "✅ good", "problem": f"⚠️ {len(h['problems'])} problem(s)",
                  "not checked yet": "not checked yet"}[h["status"]]
        lines.append(f"🩺 Drive health: {status}, {h['percent']:.0f}% verified in the last 30 days")
        last = self.d._meta("last_backup")
        if last:
            lines.append(f"💽 Index backup: {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(last)))}")
        return "\n".join(lines)
