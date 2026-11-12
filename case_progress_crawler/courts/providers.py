"""Which parties are medical providers, and which side of a case they are on.

One place for the name matching that the crawler (courts/medical.py: which cause-list cases are medical),
the case labelling and the viewer (which hospitals a case names, which side of a case is the hospital or
doctor, so whom an advocate represents) all rely on. Use the shared MATCHER instance; extend the patterns
here when a real provider is missed.

A party name is classified by kind() as
  "institution"  a hospital, clinic, nursing home, medical college, pharmacy, lab, pharma firm ... -- or a
                 hospital's officer named only by title ("Chief Medical Officer", "Medical Superintendent")
  "doctor"       an individual doctor ("Dr. X")
  "insurer"      an insurance company or health-insurance TPA
  "organisation" any other business or body that is not a provider (bank, builder, car dealer ...), or an
                 official named by title ("The Branch Manager", "Chairman", "Assistant Executive Engineer")
  "other"        anyone else, typically a private person
The words are looked for in the party's own name, without the person it acts "through" ("Apollo Hospitals
Through its Medical Superintendent Dr. X" is the hospital, whose representative does not make it a doctor).
"""

import re

INSTITUTION_PATTERNS = [
    # hospitals, clinics, nursing homes, medical colleges and centres
    r"hospitals?", r"\w*clinics?", r"nursing\s+homes?", r"nethralaya|netralaya", r"h[ru]+dayalaya|hridayalaya", r"ssims",
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
    # (a hospital's officer named only by title -- "Chief Medical Officer", "Medical Superintendent" -- is
    # caught by "medicals?")
]
DOCTOR_PATTERNS = [r"dr", r"doctors?"]


class ProviderMatcher:
    # What follows these words is the person acting for a party ("through its managing director Dr. X"),
    # not the party itself, so it is cut off before the name is matched.
    representative_re = re.compile(
        r"\b(?:through|thro'?|represented|rep\.?\s*by|r/?\s*by|by\s+its|its\s+(?:managing|authori[sz]ed|proprietor|partner|"
        r"director|chief|chairman|ceo|manager)|(?:the\s+)?(?:managing\s+director|authori[sz]ed\s+(?:signatory|officer)))\b.*$",
        re.IGNORECASE)
    institution_re = re.compile(r"\b(?:" + "|".join(INSTITUTION_PATTERNS) + r")\b", re.IGNORECASE)
    doctor_re = re.compile(r"\b(?:" + "|".join(DOCTOR_PATTERNS) + r")\b", re.IGNORECASE)
    veterinary_re = re.compile(r"\bveterinar\w*|\bvets?\b|\banimals?\b|\bpets?\b|\bcattle\b|\bkennel", re.IGNORECASE)
    # businesses that are not medical providers; a "Dr" or "hospital" beside them is incidental
    commercial_re = re.compile(
        r"\b(?:developers?|builders?|properties|property|realty|realtors?|constructions?|infra\w*|housing|apartments?|"
        r"engineering|lifts?|elevators?|motors?|automobiles?|bank|finance|financial|insurance|assurance|"
        r"co-?operative|society|telecom|airlines?|electronics|jewell?ers?|jewellery)\b", re.IGNORECASE)
    company_re = re.compile(r"\b(?:ltd|limited|pvt|private|company|co|corporation|corp|industries|enterprises|associates|bank|"
                            r"engineering|finance)\b", re.IGNORECASE)
    insurer_re = re.compile(r"\b(?:insurance|insurer|assurance|tpa|medi\s*assist|third\s+party\s+administrator)\b", re.IGNORECASE)
    official_re = re.compile(r"\b(?:manager|commissioner|officer|registrar|secretary|chairman|engineer|inspector|collector|"
                             r"tahsildar|director|president|executive|department|government|govt|board|authority|municipal\w*|corporation|"
                             r"panchayat|state|union\s+of\s+india)\b", re.IGNORECASE)
    only_doctor_re = re.compile(r"^(?:\W*\d*\W*)?(?:dr|doctors?)\b", re.IGNORECASE)  # a name that is only "Dr. So-and-so"
    # hospital names for hospital_names()
    hospital_word_re = re.compile(r"\b(?:hospitals?|nursing homes?|clinics?)\b", re.I)
    through_re = re.compile(r"\b(?:through|thro'?|represented\s+by|rep\.?\s*by)\s+(?:its\s+)?(.*)$", re.I)
    hospital_split_re = re.compile(r"[,;–—/]|\bm\s*/\s*s\b\.?|\bc\s*/\s*o\b\.?|\b(?:of|at|and)\b|\(|\)", re.I)
    hospital_title_re = re.compile(r"^(?:the\b|dr\b\.?|(?:medical|managing|unit|chief|executive)?\s*"
                                   r"(?:administrator|administartor|superintendent|directors?|owners?|head|management|ceo|md)\b|[\s/.,&-])+", re.I)

    def clean(self, name):
        """The party's own name, without the person representing it."""
        return self.representative_re.sub("", name or "").strip(" ,.;")

    def is_provider(self, name, clean=True):
        """Does the name match a provider (institution or doctor)?"""
        n = self.clean(name) if clean else name or ""
        return bool(self.institution_re.search(n) or self.doctor_re.search(n))

    def is_commercial(self, name):
        return bool(self.commercial_re.search(name or ""))

    def is_veterinary(self, name):
        return bool(self.veterinary_re.search(name or ""))

    def kind(self, name):
        """'institution', 'doctor', 'insurer', 'organisation' or 'other' (see the module docstring)."""
        n = self.clean(name)
        if self.institution_re.search(n):
            return "institution"
        if self.insurer_re.search(n):
            return "insurer"
        if self.doctor_re.search(n):
            return "doctor"
        if self.commercial_re.search(n) or self.company_re.search(n) or self.official_re.search(n):
            # "Customer Relations Officer Through its St Johns Hospital" is the hospital's officer
            return "institution" if self.institution_re.search(name or "") and self.official_re.search(n) else "organisation"
        return "other"

    def hospital_names(self, party):
        """Hospital/clinic/nursing-home names mentioned in a party string, e.g.
        'Dr.Tarun Patil, C/o Manipal Hospitals, Bangalore.' -> ['Manipal Hospitals']"""
        names = []
        for seg in self.hospital_split_re.split(party):
            # "Karnataka Health Ltd through its St Johns Hospital": what comes before "through its" is not the hospital's name
            t = self.through_re.search(seg)
            if t and self.hospital_word_re.search(t.group(1)):
                seg = t.group(1)
            m = self.hospital_word_re.search(seg)
            if not m:
                continue
            name = self.hospital_title_re.sub("", seg[:m.end()]).strip(" .-")
            if len(name.split()) > 1:  # a bare "Hospital" names nothing
                names.append(re.sub(r"\s+", " ", name).strip())
        return names

    def party_of(self, complainants, respondents, role):
        """Whom an advocate on `role`'s side acts for, judged from that side's own parties and the case's provider side:
        'hospital' (hospital or doctor), 'patient', 'insurer', 'organisation' (a business or official, not a patient),
        'mixed' (the side has an insurer or organisation beside a provider or a private person) or '' (cannot be told)."""
        own = complainants if role == "complainant" else respondents
        kinds = {self.kind(p) for p in own}
        provider = kinds & {"institution", "doctor"}
        non = kinds & {"insurer", "organisation"}
        side = self.provider_side(complainants, respondents)
        if provider:
            if non:
                return "mixed"
            return "hospital" if side == role else ("patient" if side else "")
        if non:
            if "other" in kinds:
                return "mixed"
            return "insurer" if non == {"insurer"} else "organisation"
        return "patient" if side and side != role else ""

    def provider_side(self, complainants, respondents):
        """Which side of a case is the hospital/doctor: 'complainant', 'respondent', or '' when it cannot be told.
        An institution decides it (also a hospital officer named by title only, like "Chief Medical Officer");
        failing that a doctor, unless the other side is a business (a doctor in a dispute with a bank is not a
        medical case); if neither side has either, the respondent is assumed (a patient complaining), unless
        the complainant is a business or official, which is not a patient. Both sides having one is unclear."""
        kinds = [{self.kind(p) for p in side} for side in (complainants, respondents)]
        for want in ("institution", "doctor"):
            c, r = (want in k for k in kinds)
            if c != r:
                other = kinds[1] if c else kinds[0]
                if want == "doctor" and other & {"insurer", "organisation"} and "institution" not in other:
                    return ""
                return "complainant" if c else "respondent"
            if c and r:
                return ""
        return "" if kinds[0] & {"insurer", "organisation"} else "respondent"


MATCHER = ProviderMatcher()
