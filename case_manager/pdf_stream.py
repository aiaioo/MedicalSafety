"""Streams a pikepdf document out as it is saved, so a big PDF is never assembled in memory.

Used by the annotated-PDF/annexure downloads (app.py) and by the document splitter (splitter.py)."""

import io
import queue
import threading


def stream_pikepdf(pdf):
    """Yields the bytes of saving `pdf` as it is written, so a big PDF is never assembled in memory. qpdf
    pulls page data out of its sources as it writes; saving runs in a worker thread feeding a small
    bounded queue, which also gives backpressure when the client is slow."""
    q = queue.Queue(maxsize=8)
    finished = object()
    stop = threading.Event()

    class Writer(io.RawIOBase):
        def __init__(self):
            super().__init__()
            self.buf = bytearray()

        def writable(self):
            return True

        def _put(self, item):
            while True:
                if stop.is_set():
                    raise BrokenPipeError("client went away")
                try:
                    q.put(item, timeout=0.5)
                    return
                except queue.Full:
                    pass

        def write(self, data):
            self.buf += data
            if len(self.buf) >= 64 * 1024:
                self._put(bytes(self.buf))
                self.buf.clear()
            return len(data)

        def flush_all(self):
            if self.buf:
                self._put(bytes(self.buf))
                self.buf.clear()

    def work():
        try:
            w = Writer()
            pdf.save(w)
            w.flush_all()
            q.put(finished)
        except BaseException as exc:  # reaches the consumer below; a stopped stream just ends
            if not stop.is_set():
                q.put(exc)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        while True:
            item = q.get()
            if item is finished:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()
        while worker.is_alive():  # unblock a writer waiting on a full queue
            try:
                q.get(timeout=0.1)
            except queue.Empty:
                pass
        worker.join()
