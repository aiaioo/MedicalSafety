"""Read the Delhi Medical Council's orders (pdfs/dmc/orders/, downloaded by courts/dmc.py): their text and what they decided.

The register says only "Disposed". The order says why: the Council's finding in the case, and what it did about a doctor it
found at fault. A typical order ends "...no medical negligence can be attributed ... Complaint stands disposed", or "...the
license of Dr. X is suspended for six weeks. Censure to be recorded in the State Medical Register".

  order_text(path)     the text of an order file, or None if it cannot be read (a scanned PDF has no text layer). Extracted text is
                       kept in pdfs/dmc/orders_text/<file name>.txt beside the originals, which are never changed; a file is read
                       once. .docx is read directly; .doc by antiword, catdoc or (macOS) textutil, whichever is installed; .pdf by
                       pdftotext.
  classify(text)       {"finding", "sanctions", "date", "decision"}, where finding is
                         sanctioned   the Council found fault and acted (suspension, removal from the register, censure, warning, penalty)
                         not_negligent the Council found no negligence or misconduct
                         settled      the parties settled, the complainant withdrew, or the doctor gave an undertaking
                         dismissed    dismissed without the order saying on what ground ("no cause of action arises")
                         administrative the matter was administrative, outside its remit, or without a prima facie case
                         referred     sent to another body
                         unclear      none of these could be told from the decision
                       `decision` is the decision paragraph(s), for display; `date` is the order's date (YYYY-MM-DD) or "".
                       The rules are patterns, applied only to the Council's decision (the narrative quotes the complainant's
                       prayers and the doctors' defences, which would mislead them); a mixed order (one doctor cleared, another
                       warned) is "sanctioned". Check them against orders when the Council's wording changes.
"""

import os
import re
import shutil
import subprocess
import zipfile
from xml.etree import ElementTree

from dmc_dates import find_date

OLE_MAGIC, ZIP_MAGIC, PDF_MAGIC, RTF_MAGIC = b"\xd0\xcf\x11\xe0", b"PK", b"%PDF", b"{\\rtf"
MIN_WORDS = 30  # fewer words than this is not an order's text (a scan's empty text layer)
TIMEOUT = 90
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else ""


def docx_text(path):
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    return "\n".join("".join(t.text or "" for t in p.iter(W + "t")) for p in root.iter(W + "p"))


def doc_text(path):
    """A legacy Word document: the first of antiword, catdoc, textutil (macOS) that is installed and reads it."""
    for cmd in (["antiword", "-w", "0", path], ["catdoc", "-w", path], ["textutil", "-convert", "txt", "-stdout", path]):
        if shutil.which(cmd[0]):
            text = run(cmd)
            if len(text.split()) >= MIN_WORDS:
                return text
    return ""


def extract(path):
    """The text of an order file by what it is (its first bytes), not what it is called: some ".doc" files are .docx or RTF."""
    with open(path, "rb") as f:
        head = f.read(8)
    try:
        if head.startswith(ZIP_MAGIC):
            return docx_text(path)
        if head.startswith(PDF_MAGIC):
            return run(["pdftotext", "-layout", path, "-"]) if shutil.which("pdftotext") else ""
        if head.startswith(OLE_MAGIC) or head.startswith(RTF_MAGIC):
            return doc_text(path)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        return ""
    return ""


def order_text(path, text_dir):
    """The order's text (read once, then from text_dir), or None if it has none or cannot be read."""
    cache = os.path.join(text_dir, os.path.basename(path) + ".txt")
    if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
        with open(cache, encoding="utf-8") as f:
            text = f.read()
        return text or None
    text = extract(path)
    text = text if len(text.split()) >= MIN_WORDS else ""
    os.makedirs(text_dir, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        f.write(text)
    return text or None


# ---------------------------------------------------------------------------------------------
# What the order decided

DECISION_START = re.compile(r"it is the decision of|in view of (?:the )?(?:findings|above|foregoing|facts|observations)|"
                            r"the (?:disciplinary committee|council) (?:is of the|recommends?|decides?|directs?)", re.I)
DECISION_END = re.compile(r"by the order of|\bcopy to\b|\(\s*dr\.?[^)]{3,40}\)\s*secretary", re.I)

SUSPENSION = re.compile(r"suspend\w*|suspension|debar\w*", re.I)
REMOVAL = re.compile(r"removal of (?:his|her|the|their)? ?names?|names? of [^.]{0,80}?(?:be|is|are|shall be) (?:removed|struck off|erased)|"
                     r"removed from the (?:state )?(?:medical )?register|struck off|erased? from the", re.I)
CENSURE = re.compile(r"censure|warning|\bwarned?\b|reprimand|admonish", re.I)
PENALTY = re.compile(r"penalt(?:y|ies)|fine of|\bfined\b|costs? of rs|compensation of rs", re.I)
DIRECTION = re.compile(r"(?:directs?|directed|is directed|advised?|asked) (?:him|her|them|the respondents?|Dr\.)[^.]{0,120}?(?:to|that)\b", re.I)
PERIOD = re.compile(r"(?:for|period of)\s+(?:a\s+period\s+of\s+)?((?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|\d+)"
                    r"\s*(?:\(\d+\)\s*)?(?:days?|weeks?|months?|years?))", re.I)

NOT_NEGLIGENT = re.compile(r"no (?:case of )?(?:medical )?negligence|not (?:be )?(?:attributed|made out|established|proved)|cannot be attributed|"
                           r"in accordance with (?:the )?accepted (?:professional )?(?:medical )?practices?|no (?:lapse|deficiency|professional misconduct|"
                           r"medical negligence)|not (?:found )?(?:guilty|negligent)|no case (?:of|is made out)|no evidence of (?:medical )?negligence|"
                           r"does not (?:constitute|amount to) (?:medical )?negligence|not any? (?:medical )?negligence|"
                           r"(?:does|did) not appear[^.]{0,40}negligen|not committed any (?:medical )?negligence|"
                           r"no (?:allegation|evidence)[^.]{0,60}(?:negligence|misconduct)|conforms? to (?:the )?accepted|did not violate|"
                           r"found to be satisfactory|according to the prescribed norms|performed his obligation", re.I)
SETTLED = re.compile(r"compromise|settle(?:d|ment)|undertaking|withdr[ae]w|withdrawn", re.I)
DISMISSED = re.compile(r"(?:stands|is|be|hereby|hence) dismissed|complaint dismissed|no cause of action", re.I)
ADMINISTRATIVE = re.compile(r"administrative matter|administrative in nature|prima.?facie|not (?:come|fall)s? within (?:the )?(?:purview|jurisdiction|ambit)|"
                            r"(?:beyond|outside) the (?:purview|jurisdiction|scope)|no merit|frivolous|not maintainable|time.?barred|"
                            r"not (?:a )?(?:fit )?case for|no further action|file(?:d)? (?:be )?closed|not possible to examine|"
                            r"not registered with the delhi medical council|requested to (?:state|provide|furnish|supply|confirm)|"
                            r"(?:not|nor) (?:been )?provided the case papers|sub.?judice|pending adjudication|criminal proceedings", re.I)
REFERRED = re.compile(r"referred to you|may be investigated by|for necessary action|take appropriate action|(?:forward\w*|referred?|transferr?ed|sent) to (?:the )?(?:medical council of india|mci|nmc|police|delhi police|director|"
                      r"commissioner|registrar|consumer|haryana|uttar pradesh|other state|state medical council|[a-z ]{0,25}medical council)", re.I)


def decision_part(text):
    """The Council's decision: from its last 'it is the decision of' / 'in view of the findings' to the signature."""
    flat = re.sub(r"[ \t]+", " ", text)
    end = DECISION_END.search(flat, max(0, len(flat) // 2))
    body = flat[: end.start()] if end else flat
    starts = [m.start() for m in DECISION_START.finditer(body)]
    start = starts[-1] if starts else max(0, len(body) - 1800)
    return re.sub(r"\s+", " ", body[start:]).strip()


def sanctions_in(decision):
    found = []
    for kind, rx in (("suspension", SUSPENSION), ("removal", REMOVAL), ("censure", CENSURE), ("penalty", PENALTY)):
        m = rx.search(decision)
        if m:
            period = PERIOD.search(decision[m.start(): m.start() + 200])
            found.append(f"{kind} ({period.group(1).strip()})" if period and kind in ("suspension", "removal") else kind)
    return found


CLASSIFIER_VERSION = 5  # bump when the rules above change; the next analysis_dmc.py run then stores every judgement class again


def judgement_class(finding, mixed=False):
    """(class, reason) of the order as a judgement, in the terms of judgements.py (JUDGEMENT_CLASSES, DISMISSAL_REASONS):
    fault found and a doctor sanctioned is allowed in full (in part if another respondent was cleared); no negligence is a
    dismissal "not proved"; an administrative or no-case order a dismissal for another reason; the rest unclear."""
    if finding == "sanctioned":
        return ("part" if mixed else "full"), ""
    if finding == "not_negligent":
        return "dismissed", "merits"
    if finding == "settled":
        return "dismissed", "withdrawn"
    if finding in ("administrative", "dismissed"):
        return "dismissed", "other"
    return "unclear", ""


def classify(text):
    """What the order decided; see the module docstring."""
    decision = decision_part(text)
    sanctions = sanctions_in(decision)
    mixed = bool(sanctions) and bool(NOT_NEGLIGENT.search(decision))
    if sanctions:
        finding = "sanctioned"
    elif NOT_NEGLIGENT.search(decision):
        finding = "not_negligent"
    elif SETTLED.search(decision):
        finding = "settled"
    elif DISMISSED.search(decision):
        finding = "dismissed"
    elif ADMINISTRATIVE.search(decision):
        finding = "administrative"
    elif REFERRED.search(decision):
        finding = "referred"
    else:
        finding = "unclear"
    return {"finding": finding, "mixed": mixed, "sanctions": sanctions, "date": find_date(re.sub(r"\s+", " ", text[:700])),
            "decision": decision[:1500]}
