"""Which consumer cases concern medical negligence?

A cause list gives only the parties and the kind of case (complaint, execution, review), not what
the complaint is about. So a case is kept when one of its respondents -- the party complained
against -- is a medical provider by name: a hospital, clinic or nursing home, pharmacy or chemist,
diagnostic centre or lab, pharma firm, or an individual doctor. Complainants are not checked
(a doctor can be a complainant against a builder or an insurer). In an appeal or revision the
appellant is listed as the complainant, and a hospital or doctor who lost below appeals, so for those
the complainants are checked too (which also lets in some doctors appealing non-medical disputes).

The match is deliberately by provider nouns, not "health", which would also catch health insurers
and gyms ("Care Health Insurance", "Cult Fit Health Care"). A name the patterns miss means a missed case, so
extend PROVIDER_PATTERNS when a real provider turns up unrecorded; a patient's complaint against
an insurer alone is not medical negligence and stays out.
"""

import re

FILTER_VERSION = 2  # bump when the rules change; courts/consumer.py then filters saved lists again
APPEAL_RE = re.compile(r"appeal|revision", re.IGNORECASE)  # case types where the appellant is listed as complainant

PROVIDER_PATTERNS = [
    # hospitals, clinics, nursing homes, medical colleges and centres
    r"hospitals?", r"\w*clinics?", r"nursing\s+homes?", r"nethralaya|netralaya",
    r"medical\s+(?:college|cent(?:re|er)|institute|institutions?|trust|foundation|research|complex|mission)",
    r"institutes?\s+of\s+medical", r"medical\s+sciences",
    # pharmacies and chemists
    r"pharmac(?:y|ies|ist|ists)", r"chemists?", r"druggists?", r"medicals?", r"medical\s+(?:stores?|shops?|halls?)",
    # diagnostic centres and labs
    r"diagnostics?", r"labs?", r"laborator(?:y|ies)", r"patholog\w*", r"radiolog\w*", r"scan\s+cent(?:re|er)s?",
    # pharma firms
    r"pharma\w*", r"drugs?", r"biotech\w*", r"lifesciences?|life\s+sciences?",
    # specialist care
    r"dental", r"dentists?", r"ivf", r"fertility", r"maternity", r"ayurved\w*", r"homoeo\w*|homeo\w*",
    # individual doctors
    r"dr", r"doctors?",
]
PROVIDER_RE = re.compile(r"\b(?:" + "|".join(PROVIDER_PATTERNS) + r")\b", re.IGNORECASE)


def respondents(case):
    return [r.get("respondant_name") or "" for r in case.get("respondantList") or []]


def complainants(case):
    return [r.get("complainant_name") or "" for r in case.get("complainantList") or []]


def is_medical_case(case):
    names = respondents(case)
    if APPEAL_RE.search(case.get("caseTypeName") or ""):
        names += complainants(case)
    return any(PROVIDER_RE.search(name) for name in names)


def keep_medical(rows):
    """Reduce the rows of a cause list (see ejagriti.fetch_causelist) to their medical cases and
    drop rows left with none. Returns (kept rows, number of cases listed before filtering)."""
    listed = sum(len(r["cases"]) for r in rows)
    kept = []
    for row in rows:
        cases = [c for c in row["cases"] if is_medical_case(c)]
        if cases:
            kept.append({**row, "cases": cases})
    return kept, listed


# ---------------------------------------------------------------------------------------------
# Second look, once a case's details are downloaded (courts/ejagriti_case.py). The name filter above is
# deliberately generous; classify() uses the cleaner party names and the text of the court's orders to
# label each kept case. Nothing is dropped on the strength of a label -- it is stored beside the case.

CLASSIFIER_VERSION = 1  # bump when the rules below change; cases are then labelled again

# What follows these words is the person acting for a party ("through its managing director Dr. X"),
# not the party itself, so it is cut off before the name is matched.
REPRESENTATIVE_RE = re.compile(
    r"\b(?:through|thro'?|represented|rep\.?\s*by|r/?\s*by|by\s+its|its\s+(?:managing|authori[sz]ed|proprietor|partner|"
    r"director|chief|chairman|ceo|manager)|(?:the\s+)?(?:managing\s+director|authori[sz]ed\s+(?:signatory|officer)))\b.*$",
    re.IGNORECASE)
VETERINARY_RE = re.compile(r"\bveterinar\w*|\bvets?\b|\banimals?\b|\bpets?\b|\bcattle\b|\bkennel", re.IGNORECASE)
# businesses that are not medical providers; a "Dr" or "hospital" beside them is incidental
COMMERCIAL_RE = re.compile(
    r"\b(?:developers?|builders?|properties|property|realty|realtors?|constructions?|infra\w*|housing|apartments?|"
    r"engineering|lifts?|elevators?|motors?|automobiles?|bank|finance|financial|insurance|assurance|"
    r"co-?operative|society|telecom|airlines?|electronics|jewell?ers?|jewellery)\b", re.IGNORECASE)
NEGLIGENCE_RE = re.compile(
    r"negligen|\btreatment|\bsurger(?:y|ies)|\boperat(?:ed|ion)\b|\bpatients?\b|\bdelivery\b|\bdeceased\b|"
    r"\bmedical\s+(?:negligence|bills?|records?|expenses)|\bdiagnos|\bprescri|\bdoctor|\bdeficiency\s+in\s+(?:medical|service\s+by\s+(?:the\s+)?(?:hospital|doctor))",
    re.IGNORECASE)
COMPANY_RE = re.compile(r"\b(?:ltd|limited|pvt|private|company|co|corporation|corp|industries|enterprises|associates|bank|"
                        r"engineering|finance)\b", re.IGNORECASE)
DOCTOR_WORD_RE = re.compile(r"^(?:\W*\d*\W*)?(?:dr|doctors?)\b", re.IGNORECASE)  # a name that is only "Dr. So-and-so"
PROPERTY_LOAN_RE = re.compile(r"\b(?:flat|plot|site|villa|possession|sale\s+deed|builder|loan|emi|mortgage|rera)\b", re.IGNORECASE)


def clean_party(name):
    """The party's own name, without the person representing it."""
    return REPRESENTATIVE_RE.sub("", name or "").strip(" ,.;")


def classify(respondent_names, complainant_names, case_type="", texts=()):
    """Label a case from its parties and the text of its orders. Returns {"label", "reason"} where label is
      "medical"      a medical provider is the party complained against (or the order text is about treatment)
      "not_medical"  the provider-like word is only a representative's title, or the party is a veterinary
                     or commercial (property, bank, insurer ...) business
      "unclear"      neither: no provider in the parties and no telling text
    `respondent_names` / `complainant_names`: party names as on the cause list. In an appeal or revision the
    appellant is the complainant, so a provider among the complainants counts too -- but only if the other side
    is not a commercial business."""
    appeal = bool(APPEAL_RE.search(case_type or ""))
    text = " ".join(t for t in texts if t)
    text_hits = len(NEGLIGENCE_RE.findall(text))

    if any(VETERINARY_RE.search(n) for n in respondent_names + complainant_names):
        return {"label": "not_medical", "reason": "veterinary party"}

    cleaned = [clean_party(n) for n in respondent_names]
    provider = next((n for n in cleaned if PROVIDER_RE.search(n)), None)
    # a company or bank suing a lone "Dr. X" is a debt or business dispute, not a patient's complaint
    companies_only = bool(complainant_names) and all(
        (COMPANY_RE.search(n) or COMMERCIAL_RE.search(n)) and not PROVIDER_RE.search(clean_party(n)) for n in complainant_names)
    if provider and DOCTOR_WORD_RE.match(provider) and companies_only and not text_hits:
        return {"label": "not_medical", "reason": f"company complainant against {provider!r}, no treatment in the orders"}
    if provider and not (COMMERCIAL_RE.search(provider) and not text_hits):
        return {"label": "medical", "reason": f"respondent {provider!r}"}

    if appeal:
        complainant = next((n for n in map(clean_party, complainant_names) if PROVIDER_RE.search(n)), None)
        if complainant:
            if any(COMMERCIAL_RE.search(n) for n in cleaned) and not text_hits:
                return {"label": "not_medical", "reason": f"appeal by {complainant!r} against a commercial party"}
            return {"label": "medical", "reason": f"appellant {complainant!r}"}

    if any(PROVIDER_RE.search(n) for n in respondent_names):  # matched only in the representative's clause
        return {"label": "not_medical", "reason": "provider word only in a representative's title"}
    if text_hits >= 2:
        return {"label": "medical", "reason": f"order text mentions treatment ({text_hits} terms)"}
    if PROPERTY_LOAN_RE.search(text) or any(COMMERCIAL_RE.search(n) for n in cleaned):
        return {"label": "not_medical", "reason": "commercial or property dispute"}
    return {"label": "unclear", "reason": "no provider among the parties"}
