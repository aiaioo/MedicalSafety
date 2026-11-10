"""Advocate names from the consumer courts' free-text advocate fields.

The portal prints whatever the clerk typed, so one field can hold several advocates, the parties each acts for
("Dilip.N (R-1), S.V.Joga Rao (R-2)"), notes ("svd absent", "Exparte"), or the court's own shorthand ("C.T.P").
advocate_names() pulls the names out; advocate_key() gives the slug that merges spelling variants of one
advocate ("S V JOGA RAO", "Dr. S.V.Joga Rao" -> same key); advocate_display() picks the name to show.
"""

import re

BY_WRAPPER = re.compile(r"\(\s*by\s+shri\s*/\s*smt\s*(.*?)\)\s*$", re.I)  # "(By Shri/Smt Sumanth Bharadwaj )"
PARTY_REF = re.compile(r"\(\s*(?:(?:r|op|c|moa|proposed\s+op|for\s+r)\s*[-.]?\s*)?\d[^)]*\)", re.I)  # "(R-1 to 3)", "(1,2)"
NOTE = re.compile(r"\([^)]*\b(?:mou|noc|ip|rtd|expired|absent|svd)\b[^)]*\)", re.I)
IN_PERSON = re.compile(r"^\s*i\.?\s*p\.?\s*\(", re.I)  # "I.P(Ramya R)": the party appears in person
FIRM_WORD = re.compile(r"\b(?:co|associates?|assts?|assistants?|law|legal|advocates?|partners|attorneys|chambers|inc)\b", re.I)
SPLIT = re.compile(r"\s*(?:[,;&]|\band\b|(?<![mM])/(?!s\b))\s*", re.I)
SPLIT_COMMA = re.compile(r"\s*(?:[,;]|(?<![mM])/(?!s\b))\s*")  # a firm name keeps its "&" / "and"
PARTY_NUM = re.compile(r"(?:\bfor\s+)?(?:\b(?:r|op|c|moa)\s*[-.]?\s*)?\d+(?:\s*(?:to|[-,&])\s*\d+)*[a-z]?\s*$", re.I)
TITLE = re.compile(r"^(?:m\s*/\s*s\b\.?|mr\b\.?|mrs\b\.?|ms\b\.?|smt\b\.?|sri\b\.?|shri\b\.?|dr\b\.?|adv\b\.?|advocate\b\.?|[\s.,-])+", re.I)
JUNK = re.compile(r"\b(?:exparte|ex-parte|disp\w*|unclaimed|svd|served|absent|expired|others|noc|mou|proposed|pldr|govt|"
                  r"party in person|in[- ]?person|unrepresented|nil|none|present|steps|taken|deleted|dis|by shri|by)\b", re.I)

# Variants too far apart to merge by rule: slug -> slug. Add to this when the advocates page shows one person twice.
ALIASES = {
    "joga-rao": "joga-rao-s-v",
    "joga-rao-sripada-venkat": "joga-rao-s-v",
}


def advocate_names(raw):
    """The advocates named in one advocate field, without the parties they act for or the clerk's notes.
    'B.S.Raghu Prasad (R-1), Dr. S.V.Joga Rao (R-2)' -> ['B.S.Raghu Prasad', 'S.V.Joga Rao']"""
    s = " ".join((raw or "").split())
    m = BY_WRAPPER.search(s)
    if m:
        s = m.group(1)
    if IN_PERSON.match(s):
        return []
    s = NOTE.sub(" ", PARTY_REF.sub(" ", s))
    out = []
    for seg in (SPLIT_COMMA if FIRM_WORD.search(s) else SPLIT).split(s):
        seg = TITLE.sub("", PARTY_NUM.sub("", seg)).strip(" .-()")
        seg = re.sub(r"\s+", " ", seg)
        letters = re.sub(r"[^A-Za-z]", "", seg)
        if len(letters) < 3 or JUNK.search(seg) or re.search(r"\d", seg):  # a digit left over is a party number, not a name
            continue
        # the court's shorthand ("C.T.P", "BDV") names nobody we can identify
        if (" " not in seg and len(letters) <= 4 and seg == seg.upper()) or re.fullmatch(r"(?:[A-Za-z]\.\s*)+[A-Za-z]?\.?", seg):
            continue
        if seg not in out:
            out.append(seg)
    return out


def advocate_key(name):
    """Slug that ignores case, punctuation and the order of the name: 'KUMARASWAMY BN' and 'B.N. Kumaraswamy' share one."""
    tokens = []
    for t in re.findall(r"[A-Za-z]+", name):
        tokens += list(t.lower()) if len(t) == 2 and t.isupper() else [t.lower()]  # "BN" is two initials
    slug = "-".join(sorted(tokens))
    return ALIASES.get(slug, slug)


def advocate_display(names):
    """The name to show for a group of variants: a mixed-case spelling, the commonest, then the fullest."""
    best = max(names, key=lambda n: (not n.isupper(), names[n], len(n)))
    if best.isupper():
        best = " ".join(w if len(w) <= 2 else w.title() for w in best.split(" "))
    return best
