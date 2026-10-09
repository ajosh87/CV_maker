"""Keep personal details on this computer.

Before any text goes to the LLM, personal details are swapped for placeholders such as [[NAME_1]], and
the reply is put back together locally, so documents come out exactly as if nothing had been hidden.

Nothing here relies on a model guessing what a name looks like:
- your own details are known exactly once your profile exists, so they are matched in any language or script;
- emails, links and phone numbers are found by their format, in every request;
- when a CV is read, everything found in it is listed for you to check before anything is sent.
Restoring is exact, so hiding something unnecessarily costs nothing in the finished documents.
"""
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from cv_maker.events import estimate_tokens, feed
from cv_maker.llm.chat import parse_json_reply

KINDS = ("NAME", "EMAIL", "PHONE", "LINK", "ADDRESS", "DETAIL")
KIND_LABELS = {"NAME": "Name", "EMAIL": "Email", "PHONE": "Phone", "LINK": "Link", "ADDRESS": "Address", "DETAIL": "Other"}
_PLURALS = {"NAME": ("name", "names"), "EMAIL": ("email", "emails"), "PHONE": ("phone number", "phone numbers"),
            "LINK": ("link", "links"), "ADDRESS": ("address", "addresses"), "DETAIL": ("other detail", "other details")}

def _note(mapping: dict) -> str:
    examples = " or ".join(f"[[{key}]]" for key in list(mapping)[:2])
    return (f"\n\nNote: personal details in this request were replaced with placeholders such as {examples}. "
            "Where one of those details belongs in your answer, write its placeholder exactly as given.")

_PLACEHOLDER = re.compile(r"[\[{]{1,2}\s*(NAME|EMAIL|PHONE|LINK|ADDRESS|DETAIL)_(\d+)\s*[\]}]{1,2}", re.IGNORECASE)
PLACEHOLDER_TEXT = re.compile(r"\[\[(?:NAME|EMAIL|PHONE|LINK|ADDRESS|DETAIL)_\d+\]\]")


@dataclass(frozen=True)
class Secret:
    kind: str  # one of KINDS
    text: str


# ---- formats, found in every request ----------------------------------------------------------

_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'|]+")
# A bare domain counts only with a path (github.com/you) and a lower-case ending, so "ASP.NET/C#" is left alone.
_BARE_LINK = re.compile(r"\b(?:[\w-]+\.)+(?:com|net|org|io|dev|me|in|co|ai|app|xyz|tech|site|page|info|biz|uk|de|fr|eu|us|ca|au)/[^\s<>\"'|]*")
_TECH_NAMES = ("socket.io/", "asp.net/", "ado.net/", "vb.net/")
_PHONE = re.compile(r"(?<![\w+])(?:\+|00)?\(?\d[\d \t ().\-–]{6,}\d(?!\w)")
_TRAILING = ".,;:!?)]}'\""


def _is_phone(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    if not 9 <= len(digits) <= 15:
        return False
    groups = re.findall(r"\d+", candidate)
    if sum(len(g) == 4 and g[:2] in ("19", "20") for g in groups) >= 2:
        return False  # "2016 2017 2018", "01.2019 – 03.2021": dates, not a number to call
    if len(groups) == 1 and not candidate.startswith(("+", "00")):
        return 10 <= len(digits) <= 13  # an unbroken run of digits is a phone number only at phone length
    return True


def _format_spans(text: str) -> list[tuple[int, int, str]]:
    spans = [(m.start(), m.end(), "EMAIL") for m in _EMAIL.finditer(text)]
    for m in _URL.finditer(text):
        spans.append((m.start(), m.start() + len(m.group().rstrip(_TRAILING)), "LINK"))
    for m in _BARE_LINK.finditer(text):
        if not m.group().casefold().startswith(_TECH_NAMES):
            spans.append((m.start(), m.start() + len(m.group().rstrip(_TRAILING)), "LINK"))
    spans += [(m.start(), m.end(), "PHONE") for m in _PHONE.finditer(text) if _is_phone(m.group())]
    return spans


# ---- details known exactly --------------------------------------------------------------------

def _letters_only(word: str) -> bool:
    return all(unicodedata.category(ch)[0] in "LM" or ch in "-'’" for ch in word)


def _flexible(text: str) -> str:
    """Pattern for `text` as a whole word or phrase, allowing other spacing and line breaks."""
    words = text.split()
    body = r"\s+".join(re.escape(w) for w in words)
    start = r"(?<!\w)" if re.match(r"\w", words[0][0]) else ""
    end = r"(?!\w)" if re.match(r"\w", words[-1][-1]) else ""
    return start + body + end


@lru_cache(maxsize=512)
def _patterns(kind: str, text: str) -> tuple[re.Pattern, ...]:
    text = unicodedata.normalize("NFC", text.strip())
    if not text:
        return ()
    patterns = [re.compile(_flexible(text), re.IGNORECASE)]
    if kind == "PHONE":
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 6:  # the same number written with other separators
            patterns.append(re.compile(r"(?<!\d)\+?" + r"[\s().\-–]*".join(digits) + r"(?!\d)"))
    if kind == "NAME":
        # Each part of the name on its own ("Priya" in a sign-off), as written, capitalised or in capitals.
        parts = {p.strip(".") for p in re.split(r"[\s,]+", text)}
        for part in sorted(p for p in parts if len(p) >= 3 and _letters_only(p)):
            variants = sorted({part, part.upper(), part[:1].upper() + part[1:]})
            patterns.append(re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, variants)) + r")(?!\w)"))
    return tuple(patterns)


def identity_secrets(profile, never_send=()) -> list[Secret]:
    """Your own details from the saved profile, plus the words you never want sent."""
    secrets = []
    if profile is not None:
        secrets += [Secret("NAME", profile.name), Secret("EMAIL", profile.email),
                    Secret("PHONE", profile.phone), Secret("ADDRESS", profile.location)]
        secrets += [Secret("LINK", url) for url in (profile.links or {}).values()]
    secrets += [Secret("DETAIL", term) for term in never_send]
    return [s for s in secrets if s.text and s.text.strip()]


# ---- masking ----------------------------------------------------------------------------------

def _merged(spans) -> list[tuple[int, int, str]]:
    """Overlapping finds become one span, so no part of a detail is ever left showing."""
    out: list[list] = []
    for start, end, kind in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
        if out and start < out[-1][1]:
            last = out[-1]
            if end - start > last[1] - last[0]:
                last[2] = kind
            last[1] = max(last[1], end)
        else:
            out.append([start, end, kind])
    return [tuple(s) for s in out]


def mask(text: str, secrets=(), *, allow=(), mapping: dict | None = None) -> tuple[str, dict]:
    """Swap personal details in `text` for placeholders.

    Returns the masked text and {placeholder: original}. Every distinct original gets its own placeholder,
    so putting them back is exact. `allow` lists texts (any case) to send as they are.
    """
    mapping = {} if mapping is None else mapping
    # One spelling for accented letters ("ü" can be one character or two), so a name matches however it was typed.
    text = unicodedata.normalize("NFC", text)
    allowed = {unicodedata.normalize("NFC", a).casefold() for a in allow}
    spans = _format_spans(text)
    for secret in secrets:
        for rx in _patterns(secret.kind, secret.text):
            spans += [(m.start(), m.end(), secret.kind) for m in rx.finditer(text) if m.end() > m.start()]
    spans = [s for s in spans if text[s[0]:s[1]].casefold() not in allowed]
    by_text = {original: key for key, original in mapping.items()}
    counts = Counter(key.rsplit("_", 1)[0] for key in mapping)
    out, pos = [], 0
    for start, end, kind in _merged(spans):
        original = text[start:end]
        key = by_text.get(original)
        if key is None:
            counts[kind] += 1
            key = f"{kind}_{counts[kind]}"
            mapping[key], by_text[original] = original, key
        out += [text[pos:start], f"[[{key}]]"]
        pos = end
    out.append(text[pos:])
    return "".join(out), mapping


def redact(text: str, secrets=()) -> str:
    """For the log: your details and any email address become [[KIND]]. Unlike `mask`, numbers, dates and links stay
    readable (a log is for finding what went wrong), and nothing needs putting back."""
    text = unicodedata.normalize("NFC", text)
    for secret in secrets:
        for rx in _patterns(secret.kind, secret.text):
            text = rx.sub(f"[[{secret.kind}]]", text)
    return _EMAIL.sub("[[EMAIL]]", text)


def unmask(text: str, mapping: dict, *, json_escape: bool = False) -> str:
    """Put the originals back. Tolerates [NAME_1] or {{NAME_1}}; placeholders the model made up are dropped."""
    def put_back(m: re.Match) -> str:
        value = mapping.get(f"{m.group(1).upper()}_{m.group(2)}", "")
        return json.dumps(value, ensure_ascii=False)[1:-1] if json_escape else value

    return _PLACEHOLDER.sub(put_back, text)


def describe_hidden(keys) -> str:
    """["NAME_1", "EMAIL_1", "EMAIL_2"] -> "name, 2 emails"."""
    counts = Counter(k.rsplit("_", 1)[0] for k in keys)
    return ", ".join(_PLURALS[k][0] if counts[k] == 1 else f"{counts[k]} {_PLURALS[k][1]}" for k in KINDS if counts[k])


# ---- reading a CV -----------------------------------------------------------------------------

def _norm(line: str) -> str:
    """'2. WORK EXPERIENCE:' -> 'work experience'."""
    line = re.sub(r"^[\d\W_]+", "", line.casefold())
    return " ".join(re.sub(r"[^\w\s]", " ", line).split())


_HEADINGS = {_norm(h) for h in (
    "summary", "professional summary", "profile", "professional profile", "personal profile", "about", "about me",
    "objective", "career objective", "experience", "work experience", "professional experience", "employment",
    "employment history", "work history", "career history", "education", "academic background", "qualifications",
    "skills", "technical skills", "key skills", "core skills", "core competencies", "competencies", "projects",
    "certifications", "certificates", "courses", "training", "languages", "awards", "achievements", "honors", "honours",
    "publications", "interests", "hobbies", "volunteering", "volunteer experience", "activities", "personal details",
    "personal information", "contact", "contact details", "additional information", "strengths",
    "extracurricular activities", "references", "referees", "declaration", "berufserfahrung", "ausbildung", "kenntnisse",
    "sprachen", "projekte", "expérience", "expérience professionnelle", "formation", "compétences", "langues", "projets",
    "experiencia", "experiencia laboral", "educación", "formación", "habilidades", "idiomas", "proyectos",
)}
_DOC_TITLES = {_norm(t) for t in ("curriculum vitae", "resume", "résumé", "cv", "c.v.", "bio-data", "biodata",
                                  "bio data", "lebenslauf", "currículum vitae", "curriculum")}
_UNUSED_SECTIONS = {_norm(s) for s in ("references", "reference", "referees", "referenzen", "références",
                                       "referencias", "referências", "referenties", "declaration")}
_ROLE_WORDS = {
    "engineer", "engineering", "developer", "development", "manager", "management", "analyst", "designer", "consultant",
    "scientist", "specialist", "architect", "lead", "intern", "director", "officer", "administrator", "executive",
    "assistant", "associate", "coordinator", "student", "graduate", "professional", "senior", "junior", "head", "founder",
    "teacher", "lecturer", "professor", "nurse", "accountant", "technician", "programmer", "researcher", "writer", "editor",
    "marketing", "sales", "data", "software", "product", "project", "business", "operations", "finance", "devops",
    "frontend", "backend", "full-stack", "fullstack", "cloud", "security", "support", "customer", "web", "mobile",
    "chief", "president", "partner", "owner", "freelance", "freelancer", "contractor", "trainee", "fresher", "page",
    "learning", "machine", "artificial", "intelligence", "analytics", "science", "design", "consulting", "services",
    "solutions", "technologies", "technology", "systems", "university", "college", "institute", "school", "limited",
    "ltd", "inc", "llc", "gmbh", "pvt", "private", "company", "corporation", "corp", "group",
    "ingenieur", "entwickler", "berater", "développeur", "ingénieur", "ingeniero", "desarrollador", "gerente",
} | _HEADINGS | _DOC_TITLES
_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "da", "di", "du", "la", "le", "bin", "binti", "bint",
              "al", "el", "y", "e", "ibn", "ben", "dos", "das"}
_SEPARATORS = re.compile(r"\s*(?:[|•·▪●◦∙✉☎📞📧📱🏠📍🔗-]|\t|\s{3,})\s*")
_CONTACT_WORDS = re.compile(r"(?i)(?<!\w)(?:mobile|mob|phone|ph|tel|telephone|cell|email|e-mail|mail|contact|whatsapp)\.?\s*[:：]?\s*")
_ADDRESS_WORDS = re.compile(
    r"(?i)(?<!\w)(?:street|st|road|rd|avenue|ave|lane|ln|drive|boulevard|blvd|highway|nagar|colony|layout|cross|main|"
    r"sector|block|phase|apartments?|apt|flat|floor|house|villa|residency|towers?|plot|door|po box|straße|strasse|str|"
    r"weg|platz|gasse|allee|rue|chemin|calle|avenida|carrera|via|viale|piazza|rua|ulica|ul|laan|straat|gatan|vägen|"
    r"gade|vej|sokak|mahallesi|cad)(?!\w)"
    r"|\w(?:straße|strasse|str\.|gasse|allee|platz|weg|damm|ufer|laan|straat|gatan|vägen|gade|vej)(?!\w)"  # Hauptstraße 5
)
_CJK_ADDRESS = re.compile(r"[路街号號區区市省县縣丁目番地동로길구]")
# 4-6 digits that aren't a year, a UK or Canadian postcode, or the Japanese postal mark.
_POSTCODE = re.compile(r"(?<!\d)(?!(?:19|20)\d\d(?!\d))\d{4,6}(?!\d)|\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b|\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b|〒")
_CREDENTIALS = {"msc", "bsc", "ma", "ba", "ms", "bs", "mba", "phd", "dphil", "mphil", "mres", "md", "mbbs", "jd", "llb",
                "llm", "cpa", "cfa", "ca", "acca", "cima", "frm", "pmp", "csm", "cissp", "cism", "pe", "peng", "beng",
                "meng", "btech", "mtech", "be", "me", "bcom", "mcom", "bca", "mca", "rn", "mph", "mpa", "edd"}
_LABEL = re.compile(
    r"(?i)^[\s•\-*]*(?P<label>(?:full\s)?name|(?:permanent |current |residential |home |postal |mailing )?address|"
    r"(?:current\s)?location|linkedin|github|gitlab|portfolio|website|twitter|skype|telegram|behance|dribbble|kaggle|"
    r"date of birth|d\.?\s?o\.?\s?b\.?|birth\s?date|born|nationality|citizenship|passport(?:\s(?:no\.?|number))?|"
    r"marital status|gender|sex|religion|caste|father'?s name|mother'?s name|husband'?s name|wife'?s name|"
    r"spouse(?:'s name)?|national id|aadhaa?r(?:\s(?:no\.?|number))?|pan(?:\s(?:no\.?|number))?|ssn|"
    r"social security(?: number)?)(?:\s*[:：]|\s+[-–]\s)\s*(?P<value>.*)$"
)
_LINK_LABELS = {"linkedin", "github", "gitlab", "portfolio", "website", "twitter", "skype", "telegram", "behance",
                "dribbble", "kaggle"}


def _label_kind(label: str) -> str:
    """Names, addresses and profile links are hidden; anything else a label introduces (birth date, ID numbers,
    marital status...) is never used by the app, so its line is left out altogether."""
    label = " ".join(label.casefold().split())
    if label in ("name", "full name"):
        return "NAME"
    if label.endswith(("address", "location")):
        return "ADDRESS"
    return "LINK" if label in _LINK_LABELS else "UNUSED"


def _is_heading(line: str) -> bool:
    return _norm(line) in _HEADINGS


def _first_char_ok(word: str) -> bool:
    first = word[0]
    return first.isalpha() and (first.isupper() or first.lower() == first.upper())  # capitalised, or a script without case


def _looks_like_name(segment: str) -> bool:
    s = segment.strip(" .:")
    if not 2 <= len(s) <= 50 or any(c.isdigit() for c in s) or any(c in s for c in "@/:,()|&+"):
        return False
    words = s.split()
    if not 1 <= len(words) <= 5 or any(w.casefold().strip(".") in _ROLE_WORDS for w in words):
        return False
    if len(words) == 1 and s[0].lower() != s[0].upper():
        return False  # one capitalised word is a heading or a tool far more often than a full name
    if sum(unicodedata.category(c)[0] in "LM" for c in s) < 0.85 * len(s.replace(" ", "")):
        return False
    return all(_first_char_ok(w) or w.casefold() in _PARTICLES for w in words)


def _name_in(piece: str) -> str | None:
    """The person's name in a header piece: 'Priya Raman', or 'Priya Raman, MSc, PMP' (the credentials stay visible)."""
    head, _, tail = piece.partition(",")
    if tail and all(re.sub(r"[.\s]", "", t).casefold() in _CREDENTIALS for t in tail.split(",") if t.strip()):
        piece = head
    return piece.strip(" .") if _looks_like_name(piece) else None


def _looks_like_address(segment: str) -> bool:
    if sum(c.isalpha() for c in segment) < 3 or not any(c.isdigit() for c in segment):
        return False
    return bool(_ADDRESS_WORDS.search(segment) or _CJK_ADDRESS.search(segment) or _POSTCODE.search(segment)
                or "," in segment or "،" in segment)


def _looks_like_place(segment: str) -> bool:
    """'Chennai, India' or 'الرياض، السعودية': a few capitalised names separated by commas."""
    if any(c.isdigit() for c in segment) or len(segment) > 60:
        return False
    parts = [p.strip() for p in re.split(r"[,،]", segment)]
    if not 2 <= len(parts) <= 4:
        return False
    for part in parts:
        words = part.split()
        if not 1 <= len(words) <= 3 or not _first_char_ok(part) or any(w.casefold() in _ROLE_WORDS for w in words):
            return False
    return True


def _leftovers(segment: str) -> list[str]:
    """What is left of a segment once emails, links and phone numbers (and words like "Mobile:") are taken out."""
    pieces, pos = [], 0
    for start, end, _ in _merged(_format_spans(segment)):
        pieces.append(segment[pos:start])
        pos = end
    pieces.append(segment[pos:])
    return [p for p in (_CONTACT_WORDS.sub("", piece).strip(" ,;:-–") for piece in pieces) if p]


def _header(lines: list[str]) -> list[str]:
    """The lines above the first section heading: where names and contact details live."""
    out = []
    for line in lines:
        if _norm(line) in _DOC_TITLES:
            continue
        if _is_heading(line) or len(out) >= 12:
            break
        out.append(line)
    return out


def find_in_cv(text: str) -> list[Secret]:
    """Names, addresses and labelled contact details in a CV, for the user to confirm before sending."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found: list[Secret] = []
    name = None
    for line in _header(lines):
        for segment in _SEPARATORS.split(line):
            for piece in _leftovers(segment):
                if _LABEL.match(piece):
                    continue  # labelled values are handled below, wherever they are
                if name is None and (name := _name_in(piece)):
                    found.append(Secret("NAME", name))
                elif not (name and piece.startswith(name)) and (_looks_like_address(piece) or _looks_like_place(piece)):
                    found.append(Secret("ADDRESS", piece))
    for i, line in enumerate(lines):
        m = _LABEL.match(line)
        kind = _label_kind(m.group("label")) if m else ""
        if kind in ("", "UNUSED"):
            continue  # unused lines are left out altogether (see leave_out_unused)
        value = next(iter(_leftovers(_SEPARATORS.split(m.group("value").strip())[0])), "")
        if value:
            found.append(Secret(kind, value))
        elif kind == "ADDRESS":  # "Address:" on its own line, the address below it
            for nxt in lines[i + 1:i + 4]:
                if _is_heading(nxt) or _LABEL.match(nxt) or not (_looks_like_address(nxt) or _looks_like_place(nxt)):
                    break
                found.append(Secret("ADDRESS", nxt))
    unique, seen = [], set()
    for s in found:
        if s.text.casefold() not in seen:
            seen.add(s.text.casefold())
            unique.append(s)
    return unique


def leave_out_unused(text: str) -> tuple[str, list[str]]:
    """Drop what the app never uses and that only adds personal data: references, declarations, and lines
    such as "Date of birth:" or "Passport no:". Returns the remaining text and a description of what was left out."""
    out, left_out, section = [], [], None  # section = [title, line count] while inside an unused section

    def close() -> None:
        title, count = section
        left_out.append(f"{title} ({count} line{'' if count == 1 else 's'})")

    for raw in text.splitlines():
        line, key = raw.strip(), _norm(raw)
        if key in _UNUSED_SECTIONS:
            if section is not None:
                close()
            section = [line.strip(" :").title(), 0]
            continue
        if section is not None:
            if line and _is_heading(line):
                close()
                section = None
            else:
                section[1] += bool(line)
                continue
        if key.startswith(("references available", "references on request", "references upon request")):
            left_out.append("References note")
            continue
        m = _LABEL.match(line)
        if m and _label_kind(m.group("label")) == "UNUSED":
            left_out.append(" ".join(m.group("label").split()).capitalize())
            continue
        out.append(raw)
    if section is not None:
        close()
    return "\n".join(out), left_out


def cv_request(text: str, *, keep_unused: bool = False, also_hide=(), send_as_is=(), base=()) -> dict:
    """Everything about sending one CV: the text that goes out, what is hidden in it, and what was left out.

    The check-before-sending page and the request itself both use this, so the preview is what is sent.
    `base` is the details hidden from every request (your profile, the never-send list).
    """
    trimmed, left_out = leave_out_unused(text)
    sendable = text if keep_unused else trimmed
    found = find_in_cv(sendable) + [Secret("DETAIL", t) for t in also_hide if t.strip()]
    secrets = list(base) + found
    _, everything = mask(sendable, secrets)
    allowed = {t.casefold() for t in send_as_is}
    preview, _ = mask(sendable, secrets, allow=send_as_is)
    items = [{"kind": key.rsplit("_", 1)[0], "text": original, "hidden": original.casefold() not in allowed}
             for key, original in everything.items()]
    return {"text": sendable, "preview": preview, "found": found, "items": items, "left_out": left_out}


def fill_missing_contacts(profile, mapping: dict) -> None:
    """If the model left a contact field empty although the CV had it, take it from what was hidden."""
    for attr, kind in (("name", "NAME"), ("email", "EMAIL"), ("phone", "PHONE"), ("location", "ADDRESS")):
        if not getattr(profile, attr) and f"{kind}_1" in mapping:
            setattr(profile, attr, " ".join(mapping[f"{kind}_1"].split()))


# ---- the model wrapper ------------------------------------------------------------------------

_PURPOSES = (
    ("Extract a structured profile", "Reading your CV"),
    ("Read this job description", "Reading a job description"),
    ("You are tailoring a CV", "Writing a tailored CV"),
    ("Write a cover letter", "Writing a cover letter"),
    ("You are helping a candidate fill in a job application form", "Filling in an application"),
    ("You are helping a candidate prepare for interviews", "Writing interview prep notes"),
    ("Summarise these reviews", "Summarising company reviews"),
    ("Organise what was found about", "Researching the company"),
    ("You are checking which improvements to a CV", "Checking what can be improved"),
    ("You are assessing how well a candidate's CV matches", "Reading the match in depth"),
    ("You are planning how to tailor a CV", "Planning the CV for this job"),
    ("Your last reply was not valid JSON", "Asking again for a valid reply"),
)


def purpose_of(prompt: str) -> str:
    return next((label for prefix, label in _PURPOSES if prompt.startswith(prefix)), "Other request")


CACHEABLE = {"Reading your CV", "Reading a job description"}  # read-only answers that don't change on a re-run


def _usable(reply: str) -> bool:
    try:
        parse_json_reply(reply)
        return True
    except Exception:
        return False


class PrivateModel:
    """Wraps any chat model: requests are masked before they leave, replies are unmasked when they return.

    The app's replies are JSON, so originals are put back JSON-escaped (a name with quotes stays valid JSON).
    Identical read-only requests reuse the earlier answer (`cache`), which holds only masked text.
    """

    def __init__(self, inner, secrets=list, log=None, *, extra=(), allow=(), cache=None, scope: str = "") -> None:
        self.inner = inner
        self._secrets = secrets
        self._log = log
        self._extra = list(extra)
        self._allow = list(allow)
        self._cache = cache
        self._scope = scope
        self.last_mapping: dict = {}

    def with_extra(self, extra, allow=()) -> "PrivateModel":
        """The same wrapper, also hiding `extra` and sending `allow` as it is (used for one CV)."""
        return PrivateModel(self.inner, self._secrets, self._log, extra=extra, allow=allow, cache=self._cache,
                            scope=self._scope)

    def complete(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        secrets = self._extra + list(self._secrets())
        mapping: dict = {}
        sent = []
        for message in messages:
            masked, mapping = mask(message["content"], secrets, allow=self._allow, mapping=mapping)
            sent.append({**message, "content": masked})
        if mapping:
            sent[-1] = {**sent[-1], "content": sent[-1]["content"] + _note(mapping)}
        self.last_mapping = mapping
        purpose, text, keys = purpose_of(messages[-1]["content"]), "\n\n".join(m["content"] for m in sent), sorted(mapping)
        key = None
        if self._cache is not None and purpose in CACHEABLE:
            key = hashlib.sha256(f"{self._scope}\n{json_mode}\n{text}".encode()).hexdigest()
            cached = self._cache.get(key)
            if cached is not None:
                feed.emit("llm", f"→ {purpose} · same request as before, reused the earlier answer (0 tokens)")
                if self._log is not None:
                    self._log(purpose, text, keys, {"tokens_in": 0, "tokens_out": 0, "estimated": False, "seconds": 0,
                                                    "failed": False, "cached": True})
                return unmask(cached, mapping, json_escape=True)
        feed.emit("llm", f"→ {purpose} · {len(text):,} chars" + (f" · hid {describe_hidden(keys)}" if keys else ""))
        started, reply, error = time.monotonic(), "", None
        try:
            reply = self.inner.complete(sent, json_mode=json_mode)
            if key is not None and _usable(reply):  # a broken reply is never reused: a retry asks again
                self._cache.put(key, purpose, reply)
            return unmask(reply, mapping, json_escape=True)
        except Exception as exc:
            error = exc
            raise
        finally:
            self._record(purpose, text, keys, reply, error, time.monotonic() - started)

    def _record(self, purpose: str, text: str, keys: list, reply: str, error, seconds: float) -> None:
        """Token counts and timing for the nerdbar and the record of what was sent (estimated if not reported)."""
        usage = None if error else getattr(self.inner, "last_usage", None)
        estimated = usage is None
        tokens_in = usage["in"] if usage else estimate_tokens(text)
        tokens_out = usage["out"] if usage else (estimate_tokens(reply) if reply else 0)
        feed.record_llm(tokens_in=tokens_in, tokens_out=tokens_out, seconds=seconds, estimated=estimated,
                        chars_sent=len(text), hidden=len(keys))
        approx = "~" if estimated else ""
        if error is None:
            feed.emit("llm", f"← {approx}{tokens_in:,} in · {approx}{tokens_out:,} out · {seconds:.1f}s")
        else:
            feed.emit("error", f"✗ LLM call failed after {seconds:.1f}s ({type(error).__name__})")
        if self._log is not None:
            self._log(purpose, text, keys, {"tokens_in": tokens_in, "tokens_out": tokens_out, "estimated": estimated,
                                            "seconds": round(seconds, 2), "failed": error is not None})
