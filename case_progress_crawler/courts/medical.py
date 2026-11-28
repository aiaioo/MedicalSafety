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
extend INSTITUTION_PATTERNS (courts/providers.py) when a real provider turns up unrecorded; a patient's complaint against
an insurer alone is not medical negligence and stays out.
"""

import re

from .providers import MATCHER, deva

FILTER_VERSION = 4  # bump when the rules change; courts/consumer.py then filters saved lists again
APPEAL_RE = re.compile(r"appeal|revision", re.IGNORECASE)  # case types where the appellant is listed as complainant

M = MATCHER


def respondents(case):
    return [r.get("respondant_name") or "" for r in case.get("respondantList") or []]


def complainants(case):
    return [r.get("complainant_name") or "" for r in case.get("complainantList") or []]


def is_medical_case(case):
    names = respondents(case)
    if APPEAL_RE.search(case.get("caseTypeName") or ""):
        names += complainants(case)
    return any(M.is_provider(name, clean=False) for name in names)


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

CLASSIFIER_VERSION = 4  # bump when the rules below change; cases are then labelled again, from what is stored

NEGLIGENCE_RE = re.compile(
    r"negligen|\btreatment|\bsurger(?:y|ies)|\boperat(?:ed|ion)\b|\bpatients?\b|\bdelivery\b|\bdeceased\b|"
    r"\bmedical\s+(?:negligence|bills?|records?|expenses)|\bdiagnos|\bprescri|\bdoctor|\bdeficiency\s+in\s+(?:medical|service\s+by\s+(?:the\s+)?(?:hospital|doctor))",
    re.IGNORECASE)
# Businesses whose disputes are not about treatment, though a "Dr" may be among the parties: a credit society's
# chairman, a developer's director, a doctor who bought a flat or a car. Insurers are left out: they are often
# parties to a negligence case.
OTHER_BUSINESS_RE = re.compile(
    deva("सहकार", "पत\\s*संस्था", "पथ\\s*संस्था", "पतपेढ", "क्रेड(?:ि|ी)ट", "को[\\s.\\-]*ऑप", f"बिल्ड(?:ि|ी)?(?:ं)?ग?र", "डेव(?:्ह|्)?(?:ल|े)",
         "कन्स्ट्रक्श", "मोटर्स", "गृह\\s*निर्माण", "हा(?:ऊ|उ|ौ)सिंग", "फायनान्स") + "|" +
    "(?<!ब्लड )" + deva("ब(?:ँ|ॅं|ैं|ॅ)क") + "|" +
    r"\b(?:credit|s[ao]uh\w*ar\w*|sah[a]?k[a]?r\w*|co[\s.-]*op(?:e?rative)?|(?<!blood )(?<!eye )bank|finance|financiers?|"
    r"chit\s+funds?|securities|devel\w{0,3}p(?:e{0,2}rs?|s)|builders?|buildwell|buildcon|properties|realty|realtech|realtors?|constructions?|"
    r"infrastructures?|infratech|townships?|townscape|promoters?|projects|"
    r"estates?|motors|automobiles?|cars|airlines?|airways|railways?|telecom|airtel|vodafone|electricity|couriers?|electronics|"
    r"computers?|software|jewell?ers?|jewellery|furniture|hotels?|resorts?|travels?|holidays?|housing|apartments?|concretes|seeds|"
    r"academy)\b", re.IGNORECASE)
# a party described as a clinician, a health body or a hospital chain, in words INSTITUTION_PATTERNS does not have: such
# a case is not given up for the business beside it ("District Co-operative" and a "Consultant General Surgeon")
CLINICAL_RE = re.compile(
    r"\b(?:health\w*|apollo|fortis|cradle|medanta|medicity|scans?|surgeons?|surgery|surgical|physicians?|gyn\w*colog\w*|obstetric\w*|orthop\w*|an\w?esth\w*|"
    r"cardio\w*|neuro\w*|p\w?ediatric\w*|ophthalm\w*|urolog\w*|oncolog\w*|mbbs)\b", re.IGNORECASE)
# offices a hospital's parties are not named by, but a credit society's or company's are, often without its name
SOCIETY_OFFICE_RE = re.compile(r"\bvice[\s-]*(?:chairman|president)\b", re.IGNORECASE)
# People styled "Dr" who are sued as officers of such a body, in cases that do not always name it (added by hand):
# the chairman of the Kittur Rani Channamma Urban Credit Souharda Sahakari, in its several spellings
KNOWN_OFFICERS_RE = re.compile(r"\b(?:vir[uo]\w+|v[\s.]*s)\b[\s.]*(?:s(?:/o)?\W+shivalingappa\W+|s(?:hivalingappa)?[\s.]+)?s\w{0,2}dh?\w{0,2}n\w{0,2}v\w?ar", re.IGNORECASE)
# words of an order that do mean treatment (NEGLIGENCE_RE's "operation" or "deceased" turn up in money disputes too)
TREATMENT_RE = re.compile(r"(medical\s+negligence)|\b(surger)(?:y|ies)\b|\b(patient)s?\b|\b(treatment)\b|\b(diagnos)", re.IGNORECASE)
PROPERTY_LOAN_RE = re.compile(r"\b(?:flat|plot|site|villa|possession|sale\s+deed|builder|loan|emi|mortgage|rera)\b", re.IGNORECASE)


def shows_treatment(text):
    """Do a case's orders show it is about treatment? "Medical negligence", or two different treatment words."""
    found = {next(g for g in m.groups() if g).lower().split()[0] for m in TREATMENT_RE.finditer(text)}
    return "medical" in found or len(found) >= 2


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

    if any(M.is_veterinary(n) for n in respondent_names + complainant_names):
        return {"label": "not_medical", "reason": "veterinary party"}

    # no hospital, clinic, clinician or other medical party anywhere, but a business of another kind: any doctor named
    # is there as its officer or its customer. The whole name is searched, with whom the party acts "through".
    parties = respondent_names + (complainant_names if appeal else [])
    if any(M.doctor_re.search(n) for n in parties) and not any(M.has_institution(n) or CLINICAL_RE.search(n) for n in parties):
        business = next((m.group() for r in (OTHER_BUSINESS_RE, SOCIETY_OFFICE_RE, KNOWN_OFFICERS_RE)
                         for m in map(r.search, parties) if m), None)
        if business and not shows_treatment(text):
            return {"label": "not_medical", "reason": f"a doctor beside another business ({business.lower()!r}), no treatment in the orders"}

    cleaned = [M.clean(n) for n in respondent_names]
    provider = next((n for n in cleaned if M.is_provider(n, clean=False)), None)
    # a company or bank suing a lone "Dr. X" is a debt or business dispute, not a patient's complaint
    companies_only = bool(complainant_names) and all(
        (M.company_re.search(n) or M.commercial_re.search(n)) and not M.is_provider(n) for n in complainant_names)
    if provider and M.only_doctor_re.match(provider) and companies_only and not text_hits:
        return {"label": "not_medical", "reason": f"company complainant against {provider!r}, no treatment in the orders"}
    if provider and not (M.commercial_re.search(provider) and not text_hits):
        return {"label": "medical", "reason": f"respondent {provider!r}"}

    if appeal:
        complainant = next((n for n in map(M.clean, complainant_names) if M.is_provider(n, clean=False)), None)
        if complainant:
            if any(M.commercial_re.search(n) for n in cleaned) and not text_hits:
                return {"label": "not_medical", "reason": f"appeal by {complainant!r} against a commercial party"}
            return {"label": "medical", "reason": f"appellant {complainant!r}"}

    if any(M.is_provider(n, clean=False) for n in respondent_names):  # matched only in the representative's clause
        return {"label": "not_medical", "reason": "provider word only in a representative's title"}
    if any(M.institution_re.search(n) and not M.has_institution(n) for n in cleaned) and not text_hits:
        return {"label": "not_medical", "reason": "a clinic, lab or diagnostics firm of another trade"}
    if text_hits >= 2:
        return {"label": "medical", "reason": f"order text mentions treatment ({text_hits} terms)"}
    if PROPERTY_LOAN_RE.search(text) or any(M.commercial_re.search(n) for n in cleaned):
        return {"label": "not_medical", "reason": "commercial or property dispute"}
    return {"label": "unclear", "reason": "no provider among the parties"}
