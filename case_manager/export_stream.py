"""Writes report exports (PDF and Word) a piece at a time, so their pictures are never all in memory together.

Both work the same way: the file is laid out with a small stand-in for each picture, and the real picture is loaded only
when its turn comes to be written.

  * PdfImages  -- the stand-in is a flat grey JPEG with the real picture's pixel size, so the layout engine places it
                  exactly as it would the picture (and stores a JPEG as it is: a few KB, against several MB for the
                  decoded picture). qpdf writes the PDF out, and each picture is then appended as an incremental update
                  that redefines its stand-in's object (the stand-ins stay in the file, unused).
  * DocxImages -- the stand-in is a 2 x 2 picture (the caller states the size to show it at); the .docx, a zip, is
                  rewritten entry by entry with the real pictures put in.

A loader is a function returning a picture's file bytes. It runs while the response is being sent, outside the
request, so whatever permission it needs must have been checked before it was handed over.
"""

import contextlib
import decimal
import hashlib
import io
import re
import zipfile

import fitz
import pikepdf
from PIL import Image

from pdf_stream import stream_pikepdf

CHUNK = 64 * 1024
_MARK = pikepdf.Name("/MSStandIn")  # on a stand-in's image object: which picture it stands for
_MARKED = re.compile(rb"(?<![0-9])(\d+) 0 obj\n<<[^\n]*?/MSStandIn (\d+)[ />]")


class PdfImages:
    def __init__(self):
        self._loaders = []
        self._stand_ins = {}  # stand-in file bytes -> picture number
        self._by_digest = {}  # digest of a picture's bytes -> its stand-in: identical pictures go in the file once

    def __len__(self):
        return len(self._loaders)

    def stand_in(self, loader, data):
        """The stand-in (JPEG bytes) for the picture `data`, which `loader` will load again when it is written;
        None if this picture has to go in as it is. Identical pictures get the same stand-in."""
        digest = hashlib.sha256(data).digest()
        if digest in self._by_digest:
            return self._by_digest[digest]
        try:
            with Image.open(io.BytesIO(data)) as picture:
                size = picture.size
                if picture.format not in ("PNG", "JPEG") or picture.getexif().get(0x0112, 1) != 1:  # a turned photo
                    return None
        except (OSError, ValueError, Image.DecompressionBombError):
            return None
        buf = io.BytesIO()
        Image.new("L", size, 200).save(buf, "JPEG", optimize=True, comment=b"stand-in %d" % len(self._loaders))
        stand_in = self._by_digest[digest] = buf.getvalue()
        self._stand_ins[stand_in] = len(self._loaders)
        self._loaders.append(loader)
        return stand_in

    def mark(self, pdf):
        """Marks the stand-ins in the laid-out pikepdf document `pdf`. False unless every one was found, in which case
        the document must not be streamed (it would keep a grey box)."""
        found = set()
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream) and obj.stream_dict.get("/Subtype") == "/Image":
                number = self._stand_ins.get(obj.read_raw_bytes())
                if number is not None:
                    obj.stream_dict[_MARK] = number
                    found.add(number)
        return len(found) == len(self._loaders)

    def stream(self, pdf):
        """Yields the bytes of `pdf` (marked, see mark()) with the real pictures in place of the stand-ins."""
        objects = {}  # picture number -> object number in the file as qpdf wrote it
        written, tail = 0, b""
        # Without object streams every object's header, and the trailer, is there to be read in the output.
        with contextlib.closing(stream_pikepdf(pdf, object_stream_mode=pikepdf.ObjectStreamMode.disable)) as chunks:
            for chunk in chunks:
                window = tail + chunk
                for found in _MARKED.finditer(window):
                    objects[int(found[2])] = int(found[1])
                tail = window[-2048:]
                written += len(chunk)
                yield chunk
        if len(objects) != len(self._loaders):
            raise RuntimeError(f"found {len(objects)} of {len(self._loaders)} pictures in the written PDF")
        trailer = tail[tail.rindex(b"trailer"):]
        size = int(re.search(rb"/Size (\d+)", trailer)[1])
        previous = int(re.search(rb"startxref\s+(\d+)", trailer)[1])
        kept = b"".join(b" " + m[0] for m in (re.search(rb"/Root \d+ 0 R", trailer), re.search(rb"/Info \d+ 0 R", trailer),
                                              re.search(rb"/ID \[[^\]]*\]", trailer)) if m)
        update = _Update(written, size)
        yield update.add(b"\n")
        for number, object_number in sorted(objects.items()):
            yield from update.picture(object_number, self._loaders[number]())
            fitz.TOOLS.store_shrink(100)
        yield update.finish(kept, previous)


class _Update:
    """An incremental update being appended to a PDF: new definitions of some objects, then their cross-reference."""

    def __init__(self, written, next_number):
        self.written, self.next_number = written, next_number
        self.offsets = {}

    def add(self, data):
        self.written += len(data)
        return data

    def picture(self, object_number, data):
        """Yields the picture `data` as the image object `object_number` (and any objects it needs, e.g. its mask)."""
        with fitz.open() as doc:  # MuPDF makes the image object, as it does when it lays the picture out itself
            page = doc.new_page()
            page.insert_image(page.rect, stream=data)
            one = doc.tobytes(deflate=True, deflate_images=True)
        with pikepdf.open(io.BytesIO(one)) as pdf:
            (_, image), = pdf.pages[0].obj.Resources.XObject.items()
            numbers, todo = {image.objgen: object_number}, [image]
            while todo:
                obj = todo.pop()
                number = numbers[obj.objgen]
                self.offsets[number] = self.written
                yield self.add(b"%d 0 obj\n" % number)
                if isinstance(obj, pikepdf.Stream):
                    body = obj.read_raw_bytes()
                    entries = [(k, v) for k, v in obj.stream_dict.items() if k != "/Length"]
                    yield self.add(b"<<" + self._entries(entries, numbers, todo) + b" /Length %d>>\nstream\n" % len(body))
                    for start in range(0, len(body), CHUNK):
                        yield self.add(body[start:start + CHUNK])
                    yield self.add(b"\nendstream")
                else:
                    yield self.add(self._value(obj, numbers, todo, top=True))
                yield self.add(b"\nendobj\n")

    def _entries(self, entries, numbers, todo):
        return b" ".join(pikepdf.Name(k).unparse() + b" " + self._value(v, numbers, todo) for k, v in entries)

    def _value(self, value, numbers, todo, top=False):
        """`value` as PDF syntax, with references renumbered for the file being appended to."""
        if not top and isinstance(value, pikepdf.Object) and value.is_indirect:
            if value.objgen not in numbers:
                numbers[value.objgen] = self.next_number
                self.next_number += 1
                todo.append(value)
            return b"%d 0 R" % numbers[value.objgen]
        if value is None:
            return b"null"
        if isinstance(value, bool):
            return b"true" if value else b"false"
        if isinstance(value, int):
            return b"%d" % value
        if isinstance(value, decimal.Decimal):
            return format(value, "f").encode()
        if isinstance(value, pikepdf.Dictionary):
            return b"<<" + self._entries(value.items(), numbers, todo) + b">>"
        if isinstance(value, pikepdf.Array):
            return b"[" + b" ".join(self._value(v, numbers, todo) for v in value) + b"]"
        return value.unparse()

    def finish(self, kept, previous):
        """The cross-reference table and trailer closing the update."""
        table = [b"xref\n0 1\n0000000000 65535 f \n"]
        table += [b"%d 1\n%010d 00000 n \n" % (number, offset) for number, offset in sorted(self.offsets.items())]
        table.append(b"trailer\n<< /Size %d%s /Prev %d >>\nstartxref\n%d\n%%%%EOF\n"
                     % (self.next_number, kept, previous, self.written))
        return self.add(b"".join(table))


class DocxImages:
    def __init__(self):
        self._loaders = {}  # stand-in file bytes -> loader
        self._by_key = {}

    def stand_in(self, key, loader, kind):
        """The stand-in for a "PNG" or "JPEG" picture that `loader` will load when it is written. The same `key` gets the
        same stand-in, so the picture is stored once."""
        if key not in self._by_key:
            n = len(self._loaders)
            buf = io.BytesIO()
            colour = (n & 255, (n >> 8) & 255, (n >> 16) & 255)
            Image.new("RGB", (2, 2), colour).save(buf, kind, **({"comment": b"stand-in %d" % n} if kind == "JPEG" else {}))
            self._by_key[key] = buf.getvalue()
            self._loaders[buf.getvalue()] = loader
        return self._by_key[key]

    def stream(self, skeleton):
        """Yields the .docx `skeleton` (bytes) with the real pictures in place of the stand-ins."""
        out = _Window()
        with zipfile.ZipFile(io.BytesIO(skeleton)) as source:
            target = zipfile.ZipFile(out, "w")
            for info in source.infolist():
                data = source.read(info)
                loader = self._loaders.get(data) if info.filename.startswith("word/media/") else None
                if loader is not None:
                    data = loader()
                entry = zipfile.ZipInfo(info.filename, info.date_time)
                entry.compress_type, entry.external_attr = zipfile.ZIP_DEFLATED, info.external_attr
                target.writestr(entry, data)
                del data
                yield out.take()
            target.close()
            yield out.take()


class _Window(io.RawIOBase):
    """A seekable output that holds only what has not been taken yet: zipfile goes back to fill in an entry's header
    once it has written the entry, and never further back than that."""

    def __init__(self):
        super().__init__()
        self._start, self._pos, self._buf = 0, 0, bytearray()

    def writable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        pos = offset + {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._start + len(self._buf)}[whence]
        if pos < self._start:
            raise io.UnsupportedOperation("that part has been sent")
        self._pos = pos
        return pos

    def write(self, data):
        at, size = self._pos - self._start, memoryview(data).nbytes
        self._buf[at:at + size] = data
        self._pos += size
        return size

    def take(self):
        data = bytes(self._buf)
        self._start += len(data)
        self._buf.clear()
        return data
