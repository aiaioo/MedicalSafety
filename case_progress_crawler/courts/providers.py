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
Maharashtra's district commissions write many names in Devanagari (Marathi), so each pattern has the words in that
script too (see deva).

The words are looked for in the party's own name, without the person it acts "through" ("Apollo Hospitals
Through its Medical Superintendent Dr. X" is the hospital, whose representative does not make it a doctor).
"""

import functools
import re

# \b does not work in Devanagari (Python does not count its vowel signs as parts of a word), so a Devanagari word is
# bounded by these instead: no Devanagari letter or sign just before, or just after (the danda and digits don't count)
_LETTER = "\u0900-\u0963\u0971-\u097f"
_JOINERS = "[\u200c\u200d]*"  # invisible characters the lists put inside words
DEVA_START = f"(?<![{_LETTER}\u200c\u200d])"
DEVA_END = f"(?!{_JOINERS}[{_LETTER}])"


def deva(*words, whole=False):
    """A pattern for Devanagari words, each at the start of a word -- and with `whole` the entire word, else any ending
    (case endings are joined on in Marathi). A word is a regex; joiners are allowed around its viramas, where the lists
    put them."""
    spaced = "|".join(w.replace("\u094d", f"(?:{_JOINERS}\u094d{_JOINERS})") for w in words)
    return DEVA_START + "(?:" + spaced + ")" + (DEVA_END if whole else "")


_AW = "(?:ॉ|ाॅ|ाँ|ाॉ|ॅा|ाॕ|ा)"  # the many ways the "o" of "doctor" and "hospital" is typed
_I = "(?:ि|ी)"
DEVA_HOSPITAL = deva(f"ह{_AW}स्प{_I}टल(?![िी]ट)",  # not "hospitality"
                     "र(?:ु|ू)ग्(?:ण|न)ालय", f"नर्स{_I}ंग\\s*होम", "इस्पितळ", "अस्पताल", "दवाखान", "नेत्रालय")
# like the English "lab", "clinic" and "diagnostics": weak signs of a provider
DEVA_WEAK = deva(f"क्ल{_I}न{_I}क", "ल(?:ॅ|ै)ब(?:(?:ो|ॉ)रेटर(?:ी|ि))?", f"डाय(?:ा)?ग्न(?:ो|ॉ)स्ट{_I}क")
DEVA_INSTITUTIONS = [
    DEVA_HOSPITAL, DEVA_WEAK,
    deva(f"मेड{_I}कल", f"वैद्यक{_I}य", "प(?:ॅ|ै|ा)थ(?:ॉ|ो)ल(?:ॉ|ो)ज", "ड(?:े|ॅ)(?:ं|न्)टल", f"फार्म(?:स{_I}|ास्)", f"केम{_I}स्ट",
         "आयुर्वेद", f"होम{_I}(?:ओ|यो)", f"मॅटर्न{_I}ट", "प्रस(?:ु|ू)त(?:ि|ी)गृह", "सोनोग्राफ"),
    deva("फार्मा", "दंत", whole=True),
]
# a doctor: "डॉ", "डॉक्टर" and their spellings; the plain "डा" only as an abbreviation, with its full stop
DEVA_DOCTOR = deva(f"ड(?:ॉ|ाॅ|ाँ|ाॉ|ॅा|ाॕ)(?:क्टर(?:्स)?)?", "डाक्टर", whole=True) + "|" + deva("डा") + r"(?=\s*\.)"
DEVA_NON_MEDICAL_LAB = deva("(?:अ\u200d?|ॲ)ॅ?ग्रो", "कृष(?:ी|ि)", "कलर", "फोटो", "कॉम्प्य(?:ु|ू)टर")
DEVA_VETERINARY = deva("पश(?:ु|ू)")
DEVA_INSURER = [deva("इ(?:ं|न्)श(?:्य)?(?:ु|ू)"), deva("व(?:ि|ी)मा", "बीमा", whole=True)]
DEVA_COMMERCIAL = [deva("सहकार", "पत\\s*संस्था", "पथ\\s*संस्था", "पतपेढ", "स(?:ो|ौ)साय?ट", f"बिल्ड{_I}?(?:ं)?ग?र", "डेव(?:्ह|्)?(?:ल|े)", "कन्स्ट्रक्श",
                        "हा(?:ऊ|उ|ौ)सिंग", "गृह\\s*निर्माण", "मोटर्स", "फायनान्स", "क्रेड(?:ि|ी)ट"),
                   deva("ब(?:ँ|ॅं|ैं|ॅ)क", "को[\\s.\\-]*ऑप")]
DEVA_COMPANY = [deva("कंपनी", "ल(?:ि|ी)म(?:ि|ी)टेड"), deva("कं", "लि", "प्रा", whole=True)]
DEVA_OFFICIAL = [deva("व्यवस्थापक", "अधिकार", "अध्यक्ष", "सच(?:ि|ी)व", "संचालक", "आयुक्त", "अभ(?:ि|ी)यंता", "तहस(?:ि|ी)लदार", "चेअरमन", "म(?:ॅ|ै)नेजर",
                      "महाराष्ट्र शासन", "नगर\\s*(?:पाल|पर(?:ि|ी)षद)", "महानगरपाल(?:ि|ी)का")]


def _either(english, *devanagari):
    return "|".join([english, *devanagari])


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
        "(?:" + _either(
            r"\b(?:through|thro'?|represented|rep\.?\s*by|r/?\s*by|by\s+its|its\s+(?:managing|authori[sz]ed|proprietor|partner|"
            r"director|chief|chairman|ceo|manager)|(?:the\s+)?(?:managing\s+director|authori[sz]ed\s+(?:signatory|officer)))\b",
            deva("तर्फे", "मार्फत", whole=True)) + ").*$", re.IGNORECASE)
    institution_re = re.compile(_either(r"\b(?:" + "|".join(INSTITUTION_PATTERNS) + r")\b", *DEVA_INSTITUTIONS), re.IGNORECASE)
    # "lab", "clinic" and "diagnostics" are weak signs of a provider: with one of these words the name is a computer lab,
    # a car clinic, a vehicle diagnostics firm ... not a medical one
    weak_institution_re = re.compile(_either(r"labs?|laborator(?:y|ies)|clinics?|diagnostics?", DEVA_WEAK), re.IGNORECASE)
    non_medical_lab_re = re.compile(_either(r"\b(?:computers?|software|infotech|information\s+technolog\w*|cyber|photo\w*|printing|printers?|electronics?|"
                                    r"mobiles?|robotics|language|engineering|soil|textiles?|cement|steel|metals?|"
                                    r"auto|cars?|motors?|vehicles?|agri|agro)\b", DEVA_NON_MEDICAL_LAB), re.IGNORECASE)
    doctor_re = re.compile(_either(r"\b(?:" + "|".join(DOCTOR_PATTERNS) + r")\b", DEVA_DOCTOR), re.IGNORECASE)
    veterinary_re = re.compile(_either(r"\bveterinar\w*|\bvets?\b|\banimals?\b|\bpets?\b|\bcattle\b|\bkennel", DEVA_VETERINARY), re.IGNORECASE)
    # businesses that are not medical providers; a "Dr" or "hospital" beside them is incidental
    commercial_re = re.compile(_either(
        r"\b(?:developers?|builders?|properties|property|realty|realtors?|constructions?|infra\w*|housing|apartments?|"
        r"engineering|lifts?|elevators?|motors?|automobiles?|bank|finance|financial|insurance|assurance|"
        r"co-?operative|society|telecom|airlines?|electronics|jewell?ers?|jewellery|computers?|software|infotech|holidays?|resorts?|travels?)\b",
        *DEVA_COMMERCIAL, *DEVA_INSURER), re.IGNORECASE)
    company_re = re.compile(_either(r"\b(?:ltd|limited|pvt|private|company|co|corporation|corp|industries|enterprises|associates|bank|"
                                    r"engineering|finance)\b", *DEVA_COMPANY), re.IGNORECASE)
    insurer_re = re.compile(_either(r"\b(?:insurance|insurer|assurance|tpa|medi\s*assist|third\s+party\s+administrator)\b", *DEVA_INSURER), re.IGNORECASE)
    official_re = re.compile(_either(r"\b(?:manager|commissioner|officer|registrar|secretary|chairman|engineer|inspector|collector|"
                             r"tahsildar|director|president|executive|department|government|govt|board|authority|municipal\w*|corporation|"
                             r"panchayat|state|union\s+of\s+india)\b", *DEVA_OFFICIAL), re.IGNORECASE)
    only_doctor_re = re.compile(_either(r"^(?:\W*\d*\W*)?(?:dr|doctors?)\b", r"^[\s\d.)(\-\u0964-\u096f]*(?:" + DEVA_DOCTOR + ")"), re.IGNORECASE)  # a name that is only "Dr. So-and-so"
    # hospital names for hospital_names()
    hospital_word_re = re.compile(r"\b(?:hospitals?|nursing homes?|clinics?)\b", re.I)
    through_re = re.compile(r"\b(?:through|thro'?|represented\s+by|rep\.?\s*by)\s+(?:its\s+)?(.*)$", re.I)
    hospital_split_re = re.compile(r"[,;–—/]|\bm\s*/\s*s\b\.?|\bc\s*/\s*o\b\.?|\b(?:of|at|and)\b|\(|\)", re.I)
    hospital_title_re = re.compile(r"^(?:the\b|dr\b\.?|(?:medical|managing|unit|chief|executive)?\s*"
                                   r"(?:administrator|administartor|superintendent|directors?|owners?|head|management|ceo|md)\b|[\s/.,&-])+", re.I)

    def clean(self, name):
        """The party's own name, without the person representing it."""
        return self.representative_re.sub("", name or "").strip(" ,.;")

    @functools.lru_cache(maxsize=200000)  # the same party names recur across thousands of cases; the patterns never change
    def has_institution(self, n):
        """Does the (cleaned) name have a word of an institution -- but not only a weak one in a name that is plainly not medical?"""
        words = [m.group() for m in self.institution_re.finditer(n or "")]
        if not words:
            return False
        if all(self.weak_institution_re.fullmatch(w) for w in words) and self.non_medical_lab_re.search(n):
            return False
        return True

    def is_provider(self, name, clean=True):
        """Does the name match a provider (institution or doctor)?"""
        n = self.clean(name) if clean else name or ""
        return bool(self.has_institution(n) or self.doctor_re.search(n))

    def is_commercial(self, name):
        return bool(self.commercial_re.search(name or ""))

    def is_veterinary(self, name):
        return bool(self.veterinary_re.search(name or ""))

    @functools.lru_cache(maxsize=200000)
    def kind(self, name):
        """'institution', 'doctor', 'insurer', 'organisation' or 'other' (see the module docstring)."""
        n = self.clean(name)
        if self.has_institution(n):
            return "institution"
        if self.insurer_re.search(n):
            return "insurer"
        if self.doctor_re.search(n):
            return "doctor"
        if self.commercial_re.search(n) or self.company_re.search(n) or self.official_re.search(n):
            # "Customer Relations Officer Through its St Johns Hospital" is the hospital's officer
            return "institution" if self.has_institution(name or "") and self.official_re.search(n) else "organisation"
        return "other"

    def hospital_names(self, party):
        """Hospital/clinic/nursing-home names mentioned in a party string, e.g.
        'Dr.Tarun Patil, C/o Manipal Hospitals, Bangalore.' -> ['Manipal Hospitals']"""
        return list(self._hospital_names(party))

    @functools.lru_cache(maxsize=200000)
    def _hospital_names(self, party):
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
        return tuple(names)

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
