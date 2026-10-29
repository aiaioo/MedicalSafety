"""Which consumer cases concern medical negligence?

A cause list gives only the parties and the kind of case (complaint, execution, review), not what
the complaint is about. So a case is kept when one of its respondents -- the party complained
against -- is a medical provider by name: a hospital, clinic or nursing home, pharmacy or chemist,
diagnostic centre or lab, pharma firm, or an individual doctor. Complainants are not checked
(a doctor can be a complainant against a builder or an insurer).

The match is deliberately by provider nouns, not "health", which would also catch health insurers
and gyms ("Care Health Insurance", "Cult Fit Health Care"). A name the patterns miss means a missed case, so
extend PROVIDER_PATTERNS when a real provider turns up unrecorded; a patient's complaint against
an insurer alone is not medical negligence and stays out.
"""

import re

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


def is_medical_case(case):
    return any(PROVIDER_RE.search(name) for name in respondents(case))


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
