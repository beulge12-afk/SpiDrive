"""
A weekly digest in your Saved Messages, for example:

    📅 SpiDrive, 10–17 Nov
    • 42 files, 3.1 GB added
    • 1 failed upload: report.pdf
    • Health OK, 64% verified this month
    • 2 files in the trash expire this week
    • Telegram asked to slow down 3 times; the speed was adjusted
    • Freed 1.2 GB on this PC when it was low on space
    • In total: 1,234 files, 120 GB

It is sent once a week while SpiDrive runs (WEEKLY_DIGEST, Settings > Telegram),
and /digest in Saved Messages shows it at any time.
"""
import re
import time

import config
from drive_core import LIVE, TRASH_DAYS

WEEK = 7 * 86400


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _plural(n, word, plural=None):
    return f"{n:,} {word if n == 1 else (plural or word + 's')}"


def weekly_text(d, now=None):
    now = time.time() if now is None else now
    since = now - WEEK
    db = d.db
    added = db.execute(f"SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status='complete' AND {LIVE} "
                       "AND created>=?", (since,)).fetchone()
    failed = [r[0] for r in db.execute(
        "SELECT DISTINCT e.path FROM events e JOIN pending p ON p.path=e.path "
        "WHERE e.kind='upload_failed' AND e.t>=? ORDER BY e.path", (since,))]
    expiring = db.execute(
        "SELECT COUNT(*) FROM trash_items i JOIN trash_batches b ON b.id=i.batch "
        "WHERE b.reason='deleted' AND i.is_dir=0 AND b.deleted+?<?", (TRASH_DAYS * 86400, now + WEEK)).fetchone()[0]
    floods = db.execute("SELECT COUNT(*) FROM events WHERE kind='flood' AND t>=?", (since,)).fetchone()[0]
    freed = 0
    for (detail,) in db.execute("SELECT detail FROM events WHERE kind='freed' AND t>=?", (since,)):
        m = re.search(r"(\d+) bytes", detail)
        freed += int(m.group(1)) if m else 0
    h, s = d.health(), d.stats()

    start, end = time.localtime(since), time.localtime(now)
    span = (f"{start.tm_mday}–{time.strftime('%d %b', end).lstrip('0')}" if start.tm_mon == end.tm_mon
            else f"{time.strftime('%d %b', start).lstrip('0')} – {time.strftime('%d %b', end).lstrip('0')}")
    lines = [f"📅 SpiDrive, {span}"]
    lines.append(f"• {_plural(added[0], 'file')}, {human(added[1])} added" if added[0] else "• No new files")
    if failed:
        names = ", ".join(p.rsplit("/", 1)[-1] for p in failed[:3]) + (" …" if len(failed) > 3 else "")
        lines.append(f"• {_plural(len(failed), 'failed upload')} (still retrying): {names}")
    else:
        lines.append("• No failed uploads")
    if h["status"] == "problem":
        lines.append(f"• ⚠️ Health: {_plural(len(h['problems']), 'problem')}, open the dashboard to see them")
    elif h["status"] == "good":
        lines.append(f"• Health OK, {h['percent']:.0f}% verified this month")
    else:
        lines.append("• Health not checked yet")
    if expiring:
        lines.append(f"• {_plural(expiring, 'file')} in the trash {'expires' if expiring == 1 else 'expire'} this week")
    if floods:
        lines.append(f"• Telegram asked to slow down {_plural(floods, 'time')}; the upload speed was adjusted")
    if freed:
        lines.append(f"• Freed {human(freed)} on this PC when it was low on space")
    if s["pending"]:
        lines.append(f"• {_plural(s['pending'], 'file')} waiting to upload" + (" (uploads are paused)" if d.paused else ""))
    lines.append(f"• In total: {_plural(s['files'], 'file')}, {human(s['bytes'])}")
    return "\n".join(lines)


async def maybe_send(d, now=None):
    """Send the digest to Saved Messages once a week. The first one goes a week after
    SpiDrive first runs with this feature."""
    if not getattr(config, "WEEKLY_DIGEST", True):
        return None
    now = time.time() if now is None else now
    last = d._meta("digest_last")
    if last is None:
        d._set_meta("digest_last", str(now))
        return None
    if now - float(last) < WEEK:
        return None
    await send(d, now)
    return "weekly digest sent to Saved Messages"


async def send(d, now=None):
    now = time.time() if now is None else now
    await d.client.send_message("me", weekly_text(d, now), link_preview=False)
    d._set_meta("digest_last", str(now))
    d.event("digest")
