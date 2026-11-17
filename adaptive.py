"""
Adaptive upload speed.

Telegram answers "too fast, wait N seconds" (FloodWait) when an account sends more
than it likes. Telethon waits those out by itself and logs each one; SpiDrive
listens to that log and to FloodWait errors, and:

  - on a FloodWait: halves the upload speed and runs one upload fewer at a time;
  - after a calm minute: raises the speed by a quarter and allows one more upload,
    until it is unlimited again.

So uploads run as fast as Telegram allows, and an account that keeps hitting the
limit backs off before it looks like abuse. A manual speed limit (Settings or the
upload panel) replaces this.
"""
import collections
import logging
import time
import weakref

MIN_RATE = 64 * 1024                  # never slower than this
START_RATE = 1024 * 1024              # guess when nothing was uploading at the time
MAX_RATE = 64 * 1024 * 1024           # faster than this counts as "no limit"
CALM_SECONDS = 60


class AdaptiveSpeed:
    def __init__(self, max_parallel):
        self.max_parallel = max(1, int(max_parallel))
        self.parallel = self.max_parallel
        self.rate = None                  # bytes per second, None = no limit
        self.floods = collections.deque(maxlen=100)    # (time, seconds, request)
        self._sent = collections.deque()               # (time, bytes) of the last 20 s
        self._calm_since = time.time()
        self.on_flood_hook = None         # called with (seconds, request), e.g. to log an event

    def measured(self, now=None):
        """Bytes per second actually uploaded over the last 20 seconds."""
        now = time.time() if now is None else now
        while self._sent and self._sent[0][0] < now - 20:
            self._sent.popleft()
        return sum(b for _, b in self._sent) / 20

    def on_bytes(self, n, now=None):
        self._sent.append((time.time() if now is None else now, n))

    def on_flood(self, seconds, request="", now=None):
        now = time.time() if now is None else now
        self.floods.append((now, int(seconds), request))
        current = self.rate or max(self.measured(now), START_RATE)
        self.rate = max(MIN_RATE, int(current / 2))
        self.parallel = max(1, self.parallel - 1)
        self._calm_since = now + seconds              # the calm minute starts after the wait
        if self.on_flood_hook:
            self.on_flood_hook(int(seconds), request)

    def tick(self, now=None):
        """Call regularly: speeds back up after a calm minute."""
        now = time.time() if now is None else now
        if now - self._calm_since < CALM_SECONDS:
            return
        self._calm_since = now
        if self.parallel < self.max_parallel:
            self.parallel += 1
        if self.rate is not None:
            self.rate = int(self.rate * 1.25)
            if self.rate >= MAX_RATE:
                self.rate = None

    def status(self, now=None):
        now = time.time() if now is None else now
        last = self.floods[-1] if self.floods else None
        return {"rate": self.rate, "parallel": self.parallel, "measured": int(self.measured(now)),
                "floods_today": sum(1 for t, _, _ in self.floods if t > now - 86400),
                "last_flood": {"t": last[0], "seconds": last[1], "request": last[2]} if last else None}


# ---------------------------------------------------------------- Telethon's flood-wait log

_listeners = weakref.WeakSet()


class _FloodLog(logging.Handler):
    """Telethon logs 'Sleeping for 17s (0:00:17) on SendMediaRequest flood wait'."""

    def emit(self, record):
        try:
            if "flood wait" not in str(record.msg):
                return
            args = record.args or ()
            seconds, request = int(args[1]), str(args[3])
        except (IndexError, TypeError, ValueError):
            return
        for listener in list(_listeners):
            try:
                listener.on_flood(seconds, request)
            except Exception:
                pass


_handler = _FloodLog(level=logging.INFO)


def watch(listener):
    """Send Telethon's flood waits to listener.on_flood(seconds, request)."""
    log = logging.getLogger("telethon.client.users")
    if _handler not in log.handlers:
        log.addHandler(_handler)
    if log.level == logging.NOTSET or log.level > logging.INFO:
        log.setLevel(logging.INFO)
    _listeners.add(listener)
