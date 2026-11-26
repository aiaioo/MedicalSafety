"""Writes report exports (annexure PDF, report PDF, Word) a piece at a time, so their pages and pictures are never all in
memory together.

  * PdfJoin    -- writes one PDF from the pages of several pikepdf documents, each object as soon as its page is handed
                  over, so the caller can open, write and close a few pages at a time. qpdf (pikepdf) parses and
                  serialises every object; what is done here is numbering them and writing the file around them.
                  Identical streams (fonts, pictures with their masks) are written once.
  * PdfImages  -- a report PDF's pictures. It is laid out with a flat grey JPEG standing in for each, with the real
                  picture's pixel size, so the layout engine places it exactly as it would the picture (and stores a
                  JPEG as it is: a few KB, against several MB for the decoded picture). PdfJoin loads each real picture
                  when it reaches its stand-in.
  * DocxImages -- the stand-in is a 2 x 2 picture (the caller states the size to show it at); the .docx, a zip, is
                  rewritten entry by entry with the real pictures put in.

A loader is a function returning a picture's file bytes. It runs while the response is being sent, outside the
request, so whatever permission it needs must have been checked before it was handed over.
"""

import decimal
import hashlib
import io
import re
import zipfile

import fitz
import pikepdf
from PIL import Image

CHUNK = 64 * 1024
_MARK = "/MSStandIn"  # on a stand-in's image object: which picture it stands for
# In an object as qpdf serialises it: a literal string (stepped over, whatever it says) or a reference "12 0 R".
_TOKEN = re.compile(rb"\((?:\\.|[^\\()])*\)|(?<![0-9A-Za-z#.+-])(\d+) (\d+) R(?![A-Za-z0-9#])", re.DOTALL)
_LENGTH = re.compile(rb"/Length \d+(?: \d+ R)?(?![0-9A-Za-z])")
_PLACEHOLDER = re.compile(rb"/MS(Link|Page)(\d+)(?![0-9])")  # stands for a reference to an object of the file being written
PAGE_NAME = "/MSPage%d"  # in a page handed to PdfJoin: a link to the page whose number is given in `targets`
_NOT_CARRIED = ("/Page", "/Pages", "/Catalog")  # a reference to one that is not being written becomes null


class PdfJoin:
    """One PDF being written. Get a number for each page with reserve(), hand the pages over with pages() in any order
    and any grouping, then call finish() with the numbers in page order. head(), pages() and finish() yield the file's
    bytes. `images` (a marked PdfImages) supplies the pictures its stand-ins stand for."""

    def __init__(self, images=None):
        self._images = images
        self._written = 0
        self._next = 3  # 1 is the catalog, 2 the page tree
        self._offsets = {}
        self._same = {}  # digest of a stream and what it refers to -> its object number

    def reserve(self):
        number = self._next
        self._next += 1
        return number

    def _add(self, data):
        self._written += len(data)
        return data

    def head(self):
        yield self._add(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")

    def pages(self, pdf, pages, links=None, targets=None):
        """Writes `pages`, [(page object of the pikepdf document `pdf`, its number)], and everything they refer to.
        `links`: {page number: [annotation dictionaries as bytes]} to add to those pages. `targets`: {n: page number}
        for the names PAGE_NAME % n found in them. The page objects are changed in the process."""
        known = {page.objgen: number for page, number in pages}  # object of `pdf` -> number in the file
        todo = []
        for page, number in pages:
            _inherit(page)
            extra = (links or {}).get(number, [])
            if extra:
                page.Annots = pikepdf.Array(list(page.get("/Annots", [])) + [pikepdf.Name("/MSLink%d" % i) for i in range(len(extra))])
            added = [self.reserve() for _ in extra]
            if "/Parent" in page:
                del page["/Parent"]
            body = self._renumber(pdf, page.unparse(resolved=True), known, todo, added, targets)
            yield from self._object(number, b"<< /Parent 2 0 R " + body[2:])
            for annotation_number, annotation in zip(added, extra):
                yield from self._object(annotation_number, annotation)
        yield from self._drain(pdf, known, todo, targets)

    def _drain(self, pdf, known, todo, targets):
        """Writes the objects in `todo`, [(object, number)], and those they refer to in turn."""
        while todo:
            obj, number = todo.pop()
            if isinstance(obj, pikepdf.Stream):
                if self._images is not None and _MARK in obj.stream_dict:
                    yield from self._picture(self._images.load(int(obj.stream_dict[_MARK])), number)
                    continue
                head = self._renumber(pdf, _LENGTH.sub(b"", obj.stream_dict.unparse()), known, todo, (), targets)
                data = obj.read_raw_bytes()
                self._offsets[number] = self._written
                yield self._add(b"%d 0 obj\n" % number + head[:-2] + b"/Length %d >>\nstream\n" % len(data))
                for start in range(0, len(data), CHUNK):
                    yield self._add(data[start:start + CHUNK])
                yield self._add(b"\nendstream\nendobj\n")
            else:
                yield from self._object(number, self._renumber(pdf, _serialised(obj), known, todo, (), targets))

    def _object(self, number, body):
        self._offsets[number] = self._written
        yield self._add(b"%d 0 obj\n" % number + body + b"\nendobj\n")

    def _renumber(self, pdf, body, known, todo, added, targets):
        """The serialised object `body` with its references renumbered for this file, and the objects it refers to
        that are not yet written or queued put in `todo`."""
        def reference(found):
            if found[1] is None:  # a string
                return found[0]
            key = int(found[1]), int(found[2])
            number = known.get(key)
            if number is None:
                obj = pdf.get_object(key)
                if obj is None or (isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") in _NOT_CARRIED):
                    return b"null"
                number = known[key] = self._number(pdf, obj, todo)
            return b"%d 0 R" % number

        body = _TOKEN.sub(reference, body)
        if added or targets:
            body = _PLACEHOLDER.sub(
                lambda m: b"%d 0 R" % (added[int(m[2])] if m[1] == b"Link" else targets[int(m[2])]), body)
        return body

    def _number(self, pdf, obj, todo):
        """The number for an object met for the first time, which is queued to be written -- unless it is a stream
        identical, with all it refers to, to one already numbered."""
        digest = _digest(pdf, obj) if isinstance(obj, pikepdf.Stream) else None
        if digest is not None and digest in self._same:
            return self._same[digest]
        number = self.reserve()
        if digest is not None:
            self._same[digest] = number
        todo.append((obj, number))
        return number

    def _picture(self, data, number):
        """Writes the picture `data` as the image object `number` (and any objects it needs, e.g. its mask)."""
        with fitz.open() as doc:  # MuPDF makes the image object, as it does when it lays the picture out itself
            page = doc.new_page()
            page.insert_image(page.rect, stream=data)
            one = doc.tobytes(deflate=True, deflate_images=True)
        with pikepdf.open(io.BytesIO(one)) as pdf:
            (_, image), = pdf.pages[0].obj.Resources.XObject.items()
            yield from self._drain(pdf, {image.objgen: number}, [(image, number)], None)
        fitz.TOOLS.store_shrink(100)

    def finish(self, page_numbers):
        kids = b" ".join(b"%d 0 R" % n for n in page_numbers)
        yield from self._object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        yield from self._object(2, b"<< /Type /Pages /Count %d /Kids [ %s ] >>" % (len(page_numbers), kids))
        start = self._written
        table = [b"xref\n0 %d\n0000000000 65535 f \n" % self._next]
        table += [b"%010d 00000 n \n" % self._offsets[n] for n in range(1, self._next)]
        table.append(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (self._next, start))
        yield self._add(b"".join(table))


def _digest(pdf, obj, within=()):
    """A digest of an object of `pdf` and everything it refers to (a picture and its mask or colour profile), equal for
    equal ones wherever they come from; None if it leads to a page, back to itself or very deep, or to a stand-in."""
    if isinstance(obj, pikepdf.Stream):
        if _MARK in obj.stream_dict:
            return None
        head, data = _LENGTH.sub(b"", obj.stream_dict.unparse()), obj.read_raw_bytes()
    else:
        head, data = _serialised(obj), b""
    parts, end = [], 0
    for found in _TOKEN.finditer(head):
        if found[1] is None:  # a string
            continue
        key = int(found[1]), int(found[2])
        if key in within or len(within) >= 8:
            return None
        child = pdf.get_object(key)
        if isinstance(child, pikepdf.Dictionary) and child.get("/Type") in _NOT_CARRIED:
            return None
        digest = b"null" if child is None else _digest(pdf, child, (*within, key))
        if digest is None:
            return None
        parts += [head[end:found.start()], b"<", digest, b">"]
        end = found.end()
    return hashlib.sha256(b"".join(parts) + head[end:] + b"\0" + data).hexdigest().encode()


def _serialised(obj):
    """An object that is not a stream as PDF syntax (pikepdf gives numbers and booleans as Python's own)."""
    if isinstance(obj, pikepdf.Object):
        return obj.unparse(resolved=True)
    if isinstance(obj, bool):
        return b"true" if obj else b"false"
    return format(obj, "f").encode() if isinstance(obj, decimal.Decimal) else b"%d" % obj


def _inherit(page):
    """Puts on `page` what it inherits from the page tree, which is left behind."""
    for key in ("/Resources", "/MediaBox", "/CropBox", "/Rotate"):
        node = page.get("/Parent")
        while key not in page and node is not None:
            if key in node:
                page[key] = node[key]
            node = node.get("/Parent")


def in_chunks(parts, size=CHUNK):
    """The bytes of `parts` gathered into pieces of at least `size` (but for the last), for sending."""
    held = bytearray()
    for part in parts:
        held += part
        if len(held) >= size:
            yield bytes(held)
            held.clear()
    if held:
        yield bytes(held)


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

    def load(self, number):
        return self._loaders[number]()

    def stream(self, pdf):
        """Yields the bytes of `pdf` (marked, see mark()) with the real pictures in place of the stand-ins."""
        join = PdfJoin(self)
        pages = [(page.obj, join.reserve()) for page in pdf.pages]

        def parts():
            yield from join.head()
            yield from join.pages(pdf, pages)
            yield from join.finish([number for _, number in pages])
        return in_chunks(parts())


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
