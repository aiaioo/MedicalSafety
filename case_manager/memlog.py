"""Logs how much memory the app's process is using, to find what makes it grow, and gives freed memory back.

Two kinds of line go to stderr (the journal under systemd), each starting "memory ", besides the "trim" lines below:
  * request -- one per request (static files left out), written when the response has been sent
  * sample  -- from a background thread: when the total has moved by SAMPLE_STEP_MB since the last sample logged, and
               at least every HEARTBEAT seconds

Sizes are in MB: rss is what is in RAM, swap what has been swapped out, total their sum (the figure to follow: a rise in
swap alone may only be other processes pushing this one out of RAM), change the difference in total over the request. The figures are for the whole process, so work done at the same time by other threads is counted too.

    journalctl -u case-manager | grep ' memory '

After a request that has left the process TRIM_STEP_MB bigger, trim() empties MuPDF's cache and has the C
library return its free memory to the system, which by itself it largely doesn't; a "trim" line says what that freed.

It reads /proc/self/status, so it does nothing where there is none (macOS).
"""

import ctypes
import logging
import sys
import threading
import time

import fitz
from flask import g, request

SAMPLE_EVERY = 5       # seconds between the background thread's looks
SAMPLE_STEP_MB = 50
HEARTBEAT = 600        # seconds
TRIM_STEP_MB = 32      # growth since the last trim that calls for another

log = logging.getLogger("memory")
log.setLevel(logging.INFO)
log.propagate = False
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("memory %(message)s"))
log.addHandler(_handler)


def usage():
    """(rss, swap) of this process in MB, or None if /proc doesn't say."""
    try:
        with open("/proc/self/status") as f:
            kb = {line.split(":")[0]: int(line.split()[1]) for line in f if line.startswith(("VmRSS:", "VmSwap:"))}
        return kb["VmRSS"] / 1024, kb.get("VmSwap", 0) / 1024
    except (OSError, KeyError):
        return None


def _write(kind, before, started, detail):
    after = usage()
    if after is None or before is None:
        return
    log.info("%s %s rss=%.0f swap=%.0f total=%.0f change=%+.0f secs=%.1f", kind, detail, *after, sum(after),
             sum(after) - sum(before), time.monotonic() - started)


_malloc_trim = getattr(ctypes.CDLL(None), "malloc_trim", None)  # glibc only
_trim_lock = threading.Lock()
_floor = 0.0  # the process's size after the last trim (or smaller, if it has since shrunk)


def trim():
    """Gives memory that is free but still held by the process back to the system."""
    fitz.TOOLS.store_shrink(100)  # MuPDF keeps decoded images and fonts, up to 256 MB
    if _malloc_trim is not None:
        _malloc_trim(0)


def trim_if_grown():
    global _floor
    with _trim_lock:
        before, started = usage(), time.monotonic()
        if before is None:
            return
        _floor = min(_floor, sum(before))
        if sum(before) - _floor < TRIM_STEP_MB:
            return
        trim()
        _write("trim", before, started, "freed")
        _floor = sum(usage() or before)


def init_app(app):
    """Logs every request of `app`, and starts the sampling thread. Call it before anything else registers a
    before_request, so requests those turn away are logged too."""
    if usage() is None:
        return

    @app.before_request
    def note_memory():
        g.memlog = usage(), time.monotonic()

    @app.after_request
    def log_memory(response):
        start = g.get("memlog")
        if start is None or (request.endpoint or "").endswith("static"):
            return response
        # The route's pattern, not the path: no ids, names or search words in the log.
        detail = f"{request.method} {request.url_rule.rule if request.url_rule else '(no route)'} status={response.status_code}"
        def finish():
            _write("request", *start, detail)
            trim_if_grown()

        if response.direct_passthrough:  # a file handed straight to the server, which then never closes the response
            finish()
        else:
            response.call_on_close(finish)  # after a streamed body has been sent
        return response

    threading.Thread(target=_sample, daemon=True, name="memory-log").start()


def _sample():
    last, logged = None, 0.0
    while True:
        now = usage()
        if now is not None and (last is None or abs(sum(now) - last) >= SAMPLE_STEP_MB
                                or time.monotonic() - logged >= HEARTBEAT):
            log.info("sample rss=%.0f swap=%.0f total=%.0f change=%+.0f", *now, sum(now), 0 if last is None else sum(now) - last)
            last, logged = sum(now), time.monotonic()
        time.sleep(SAMPLE_EVERY)
