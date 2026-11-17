"""
Smart search for SpiDrive.

  - Persian / Arabic spelling variants match each other: ي/ی, ك/ک, ة/ه, أ/إ/آ/ا,
    Persian and Arabic digits match 0-9, and the half-space (ZWNJ) and diacritics
    are ignored. "میخواهم", "می‌خواهم" and "مي خواهم" all find each other.
  - Filters, mixed freely with words:
        type:video   (video, image/photo, audio/music, doc, archive, other; also ویدیو, عکس, ...)
        size:>1gb   size:<500mb   size:100mb..2gb
        after:2026-01   before:2026-03-15   date:2026-02   after:30d   (Persian years: after:1404-07)
        in:Videos   (folder name contains)        ext:pdf
        "exact words"
Dates are the file's own date (modified time; for files sent from Telegram, when they were sent).
"""
import calendar
import mimetypes
import posixpath
import re
import time
import unicodedata

DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv",
           ".odt", ".rtf", ".epub", ".srt"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"}
EXTRA_TYPES = {".mkv": "video/x-matroska", ".webm": "video/webm", ".m4v": "video/mp4", ".ts": "video/mp2t",
               ".flac": "audio/flac", ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac",
               ".webp": "image/webp", ".avif": "image/avif", ".heic": "image/heic", ".heif": "image/heif"}
for _e, _t in EXTRA_TYPES.items():
    mimetypes.add_type(_t, _e)


def kind_of(path):
    """image, video, audio, document, archive or other."""
    ext = posixpath.splitext(path)[1].lower()
    mt = mimetypes.guess_type(path)[0] or ""
    for k in ("image", "video", "audio"):
        if mt.startswith(k + "/"):
            return k
    if ext in DOC_EXT:
        return "document"
    if ext in ARCHIVE_EXT:
        return "archive"
    return "other"


# ---------------------------------------------------------------- normalising text

_MAP = {}
for src, dst in {"ي": "ی", "ى": "ی", "ئ": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "ؤ": "و",
                 "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ء": ""}.items():
    _MAP[ord(src)] = dst
for i in range(10):
    _MAP[0x06F0 + i] = str(i)          # Persian digits
    _MAP[0x0660 + i] = str(i)          # Arabic digits
for cp in (0x200C, 0x200D, 0x200E, 0x200F, 0x0640, 0xFEFF):   # ZWNJ, ZWJ, marks, tatweel, BOM
    _MAP[cp] = None
for cp in list(range(0x064B, 0x0660)) + [0x0670]:              # harakat
    _MAP[cp] = None
_SEP = re.compile(r"[\s_\-.+,()\[\]]+")


def normalize(text):
    """Lower-case text with Persian/Arabic variants and digits unified."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    return t.translate(_MAP).casefold()


def _forms(name):
    n = normalize(name)
    spaced = _SEP.sub(" ", n).strip()
    return spaced, spaced.replace(" ", "")


# ---------------------------------------------------------------- filters

TYPE_ALIASES = {
    "image": "image", "images": "image", "photo": "image", "photos": "image", "picture": "image", "pic": "image",
    "img": "image", "عکس": "image", "تصویر": "image",
    "video": "video", "videos": "video", "movie": "video", "film": "video", "ویدیو": "video", "ویدئو": "video",
    "فیلم": "video",
    "audio": "audio", "music": "audio", "song": "audio", "sound": "audio", "موسیقی": "audio", "اهنگ": "audio",
    "صوت": "audio",
    "doc": "document", "docs": "document", "document": "document", "documents": "document", "سند": "document",
    "archive": "archive", "archives": "archive", "zip": "archive", "فشرده": "archive",
    "other": "other",
}
KEYS = {"type": "type", "kind": "type", "نوع": "type", "size": "size", "حجم": "size",
        "after": "after", "since": "after", "از": "after", "before": "before", "until": "before", "تا": "before",
        "date": "date", "on": "date", "تاریخ": "date", "in": "in", "folder": "in", "در": "in", "ext": "ext"}
_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2, "g": 1024 ** 3,
          "gb": 1024 ** 3, "t": 1024 ** 4, "tb": 1024 ** 4}


def parse_size(text):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?b?)", text.strip().lower())
    if not m:
        raise ValueError(f"size '{text}': use something like 500mb or 1.5gb")
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def jalali_to_gregorian(jy, jm, jd):
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)
    months = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gd > months[gm]:
        gd -= months[gm]
        gm += 1
    return gy, gm + 1, gd


def _next_period(y, m, unit):
    if unit == "y":
        return y + 1, 1, 1
    return (y + 1, 1, 1) if m == 12 else (y, m + 1, 1)


def parse_period(text, now=None):
    """'2026', '2026-01', '2026-01-15', Persian years like '1404-07', or '30d'/'2w'/'6m'/'1y' (ago)
    -> (start, end) timestamps in local time; end is exclusive."""
    t = normalize(text).strip().replace("/", "-")
    now = time.time() if now is None else now
    m = re.fullmatch(r"(\d+)([dwmy])", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        start = now - n * {"d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}[u]
        return start, now + 1
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", t)
    if not m:
        raise ValueError(f"date '{text}': use 2026, 2026-01 or 2026-01-15")
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    unit = "d" if d else ("m" if mo else "y")
    mo, d = int(mo or 1), int(d or 1)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        raise ValueError(f"date '{text}' is not valid")
    jalali = y < 1700                                  # Persian (Jalali) calendar
    conv = (lambda *ymd: jalali_to_gregorian(*ymd)) if jalali else (lambda *ymd: ymd)
    if not jalali:
        d = min(d, calendar.monthrange(y, mo)[1])
    a = conv(y, mo, d)
    if unit == "d":
        b = time.localtime(time.mktime((*a, 12, 0, 0, 0, 0, -1)) + 86400)[:3]
    else:
        b = conv(*_next_period(y, mo, unit))
    ts = lambda ymd: time.mktime((*ymd, 0, 0, 0, 0, 0, -1))
    return ts(a), ts(b)


def _tokens(q):
    return re.findall(r'(\S+?:"[^"]*"|"[^"]*"|\S+)', q)


class Query:
    def __init__(self, text, now=None):
        self.words, self.kinds, self.exts, self.folders = [], set(), set(), []
        self.min_size = self.max_size = self.after = self.before = None
        self.errors = []
        for tok in _tokens(text or ""):
            key, sep, val = tok.partition(":")
            k = KEYS.get(normalize(key)) if sep and val else None
            val = val.strip('"')
            try:
                if k is None:
                    self._word(tok)
                elif k == "type":
                    for v in val.split(","):
                        kind = TYPE_ALIASES.get(normalize(v))
                        if kind is None:
                            raise ValueError(f"type '{v}': use video, image, audio, doc, archive or other")
                        self.kinds.add(kind)
                elif k == "ext":
                    self.exts.update("." + normalize(v).lstrip(".") for v in val.split(",") if v)
                elif k == "in":
                    self.folders.append(_forms(val)[0])
                elif k == "size":
                    self._size(val)
                else:
                    start, end = parse_period(val, now)
                    if k in ("after", "date"):
                        self.after = start if self.after is None else max(self.after, start)
                    if k == "before":
                        self.before = start if self.before is None else min(self.before, start)
                    if k == "date":
                        self.before = end if self.before is None else min(self.before, end)
            except ValueError as e:
                self.errors.append(str(e))

    def _word(self, tok):
        spaced, _ = _forms(tok.strip('"'))
        if tok.startswith('"') and tok.endswith('"') and len(tok) > 1:
            if spaced:
                self.words.append(spaced)
        else:
            self.words += [w for w in spaced.split(" ") if w]

    def _size(self, v):
        v = normalize(v).replace(" ", "")
        if ".." in v:
            a, b = v.split("..", 1)
            if a:
                self.min_size = parse_size(a)
            if b:
                self.max_size = parse_size(b)
            return
        m = re.match(r"(>=|<=|>|<|=)?(.*)", v)
        op, n = m.group(1) or ">=", parse_size(m.group(2))
        if op in (">", ">="):
            self.min_size = n + (op == ">")
        elif op in ("<", "<="):
            self.max_size = n - (op == "<")
        else:
            self.min_size, self.max_size = int(n * 0.95), int(n * 1.05)

    @property
    def empty(self):
        return not (self.words or self.kinds or self.exts or self.folders or self.min_size is not None
                    or self.max_size is not None or self.after is not None or self.before is not None)

    def match(self, path, size, mtime):
        if self.min_size is not None and size < self.min_size:
            return False
        if self.max_size is not None and size > self.max_size:
            return False
        if self.after is not None and mtime < self.after:
            return False
        if self.before is not None and mtime >= self.before:
            return False
        name = posixpath.basename(path)
        if self.exts and normalize(posixpath.splitext(name)[1]) not in self.exts:
            return False
        if self.kinds and kind_of(path) not in self.kinds:
            return False
        if self.folders:
            folder = _forms(posixpath.dirname(path))[0]
            if not all(f in folder for f in self.folders):
                return False
        if self.words:
            spaced, joined = _forms(name)
            for w in self.words:
                if w not in spaced and w.replace(" ", "") not in joined:
                    return False
        return True
