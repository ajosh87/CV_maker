"""Deterministic fact gate applied to everything the LLM writes.

The LLM may reword and reorder, but the output must not claim skills the profile lacks or invent
numbers. Rather than deleting individual words (which mangles prose), offending sentences and
bullets are dropped, and identity, employers, titles, dates and education always come from the profile.
"""
import json
import re
from dataclasses import asdict

from cv_maker import skills as sk
from cv_maker.jobs.match import mentions, profile_corpus
from cv_maker.models import Education, Experience, Profile


class CvDocument:
    def __init__(
        self,
        name: str,
        summary: str,
        experiences: list[Experience],
        education: list,
        skills: list[str],
        contact: list[str] | None = None,
        headline: str = "",
        extras: dict | None = None,
    ) -> None:
        self.name = name
        self.summary = summary
        self.experiences = experiences
        self.education = education
        self.skills = skills
        self.contact = contact or []
        self.headline = headline
        self.extras = extras or {}


SKILLS_SHOWN = 22  # on a CV planned for one job: enough for every relevant skill, few enough to stay focused
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


class FactCheck:
    """Record of what the filter changed and why, shown to the user next to each version."""

    def __init__(self) -> None:
        self.items: list[dict] = []  # {"where", "action", "text", "reason"}
        self.rewrites: list[dict] = []  # {"role", "original", "final"} for every bullet of every role

    def add(self, where: str, action: str, text: str, reason: str) -> None:
        self.items.append({"where": where, "action": action, "text": text, "reason": reason})

    def to_dict(self) -> dict:
        return {"items": self.items, "rewrites": self.rewrites}


def _offending(text: str, banned: list[str]) -> list[str]:
    return [b for b in banned if mentions(b, text)]


_WORD = re.compile(r"[A-Za-z][\w&+#.'-]*")
# Capitalized words that are normal in prose and never a claim about the candidate.
_COMMON_CAPITALS = {
    "I", "I'm", "I've", "I'd", "I'll", "Dear", "Mr", "Ms", "Mrs", "Dr", "Hiring", "Manager", "Team", "Kind",
    "Best", "Regards", "Sincerely", "Yours", "Thank", "Thanks", "CV",
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
    "November", "December", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
}


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def _vocabulary(text: str) -> set[str]:
    return {w.lower().strip(".'-") for w in re.findall(r"[\w&+#.'-]+", text)}


def _unsupported(sentence: str, words: set[str], numbers: set[str]) -> str:
    """Why a free-text sentence makes a claim the allowed facts don't contain ("" if it doesn't)."""
    invented = sorted(_numbers(sentence) - numbers)
    if invented:
        return f"introduced numbers not in your profile ({', '.join(invented)})"
    names = []
    for token in _WORD.findall(sentence)[1:]:  # the first word is capitalized anyway
        word = re.sub(r"'s$", "", token).strip(".'-")
        if word[:1].isupper() and word not in _COMMON_CAPITALS and word.lower() not in words and word not in names:
            names.append(word)
    if names:
        return f"names {', '.join(names)}, which isn't in your profile or the job posting"
    return ""


def filter_text(text: str, banned: list[str], check: FactCheck | None = None, where: str = "summary",
                allowed_text: str | None = None) -> str:
    """Drop every sentence that mentions a banned term or, given `allowed_text`, any number or name not in it."""
    words = _vocabulary(allowed_text) if allowed_text is not None else None
    numbers = _numbers(allowed_text) if allowed_text is not None else None
    kept = []
    for sentence in _SENTENCE.split(text.strip()):
        if not sentence:
            continue
        hits = _offending(sentence, banned)
        reason = f"mentions {', '.join(hits)}, which your profile does not confirm" if hits else ""
        if not reason and words is not None:
            reason = _unsupported(sentence, words, numbers)
        if reason:
            if check is not None:
                check.add(where, "removed", sentence, reason)
        else:
            kept.append(sentence)
    return " ".join(kept).strip()


def allowed_facts_text(profile: Profile, context_text: str = "") -> str:
    """Everything a free-text sentence may draw names and numbers from: the profile plus job context."""
    return json.dumps(asdict(profile), ensure_ascii=False) + "\n" + context_text


def _overclaim(text: str, levels: dict) -> str:
    """Why `text` says more about a skill than the level you gave ("" if it doesn't)."""
    for skill, level in (levels or {}).items():
        if sk.overclaims(text, skill, level):
            return f"describes {skill} beyond the level you gave ({level})"
    return ""


def _within_levels(text: str, levels: dict, check: FactCheck | None, where: str) -> str:
    kept = []
    for sentence in _SENTENCE.split(text.strip()):
        reason = _overclaim(sentence, levels) if sentence else ""
        if reason:
            if check is not None:
                check.add(where, "removed", sentence, reason)
        elif sentence:
            kept.append(sentence)
    return " ".join(kept).strip()


def _safe_bullets(original: list[str], rewritten: list[str], banned: list[str], check: FactCheck | None, role: str,
                  levels: dict | None = None) -> list[str]:
    if len(rewritten) != len(original):
        # Cannot pair rewrites with source facts; keep the facts as written.
        if check is not None and rewritten != original:
            check.add("experience", "kept original", role,
                      f"the model returned {len(rewritten)} bullets for {len(original)} facts, so your original wording was kept")
        out = list(original)
    else:
        out = []
        for source, new in zip(original, rewritten):
            new = (new or "").strip()
            hits = _offending(new, banned) if new else []
            invented = sorted(_numbers(new) - _numbers(source)) if new else []
            level_reason = _overclaim(new, levels) if new and not _overclaim(source, levels) else ""
            if not new:
                out.append(source)
            elif hits or invented or level_reason:
                reason = (f"mentions {', '.join(hits)}, which your profile does not confirm" if hits
                          else f"introduced numbers not in your CV ({', '.join(invented)})" if invented else level_reason)
                if check is not None:
                    check.add("experience", "kept original", new, reason)
                out.append(source)
            else:
                out.append(new)
    if check is not None:
        check.rewrites += [{"role": role, "original": o, "final": f} for o, f in zip(original, out)]
    return out


def contact_line(profile: Profile) -> list[str]:
    return [v for v in [profile.email, profile.phone, profile.location, *profile.links.values()] if v]


def filter_cv_document(doc: CvDocument, allowed: Profile, banned_terms: list[str] | None = None,
                       check: FactCheck | None = None, context_text: str = "", selection: list[list[int]] | None = None,
                       drop_skills: list[str] | None = None) -> CvDocument:
    """`context_text` (the job title, company and description) may supply names the summary uses. `selection`
    (tailor.py): which bullets of each role this CV uses, in order; `doc.experiences` then lines up with your roles
    one for one, each with one rewrite per chosen bullet ("" keeps your wording). `drop_skills`: skills that only
    dilute this job's CV, left off unless the model ranked them."""
    allowed_skills = {_norm(s.name): s.name for s in allowed.skills}
    skills: list[str] = []
    invented: list[str] = []
    for s in doc.skills:
        name = str(s.get("name", "")) if isinstance(s, dict) else str(s)
        key = _norm(name)
        if key in allowed_skills:
            if allowed_skills[key] not in skills:
                skills.append(allowed_skills[key])
        elif key and any(sk.variants(name) & sk.variants(p.name) or sk.found_in(name, p.name) for p in allowed.skills):
            if name not in skills:
                skills.append(name)  # your skill under the posting's name ("RAG" for "multimodal RAG")
        elif key:
            invented.append(name)
            if check is not None:
                check.add("skills", "removed", name, "not a skill in your profile")
    # Skills the model left out still belong on the CV, after the ones it ranked (when the CV is planned for this
    # job: not the ones that only dilute it, and not so many that the relevant ones get lost).
    dropped = {_norm(d) for d in drop_skills or []}
    rest = [s.name for s in allowed.skills if s.name not in skills and _norm(s.name) not in dropped]
    if selection is not None and drop_skills is not None:
        rest = rest[:max(0, SKILLS_SHOWN - len(skills))]
    skills += rest

    # Never strip a term the profile itself mentions (e.g. "AWS" when the skill reads "AWS (ECS, S3)").
    corpus = profile_corpus(allowed)
    banned, seen = [], set()
    for term in [*(banned_terms or []), *invented]:
        if term and _norm(term) not in seen and not mentions(term, corpus):
            seen.add(_norm(term))
            banned.append(term)

    # Skills you said you know at a beginner or intermediate level are never described as more.
    levels = {s.name: s.level for s in allowed.skills if s.level in ("beginner", "intermediate")}
    rewritten = {(_norm(e.company), _norm(e.title)): e for e in doc.experiences}
    experiences = []
    for i, exp in enumerate(allowed.experiences):
        role = f"{exp.title} — {exp.company}"
        if selection is not None:
            sources = [exp.bullets[j] for j in selection[i]] if i < len(selection) else list(exp.bullets)
            match = doc.experiences[i] if i < len(doc.experiences) else None
        else:
            sources = exp.bullets
            match = rewritten.get((_norm(exp.company), _norm(exp.title)))
        bullets = _safe_bullets(sources, match.bullets if match else sources, banned, check, role, levels)
        experiences.append(Experience(exp.company, exp.title, exp.location, exp.start, exp.end, exp.current, bullets, exp.confirmed))

    summary = filter_text(doc.summary, banned, check, "summary", allowed_facts_text(allowed, context_text))
    return CvDocument(
        name=allowed.name or doc.name,
        summary=_within_levels(summary, levels, check, "summary"),
        experiences=experiences,
        education=[Education(e.school, e.credential, e.field, e.dates) for e in allowed.education],
        skills=skills,
        contact=contact_line(allowed),
        headline=allowed.target_role or (allowed.experiences[0].title if allowed.experiences else ""),
        extras={k: v for k, v in allowed.extras.items() if k in ("certifications", "projects", "languages") and v},
    )
