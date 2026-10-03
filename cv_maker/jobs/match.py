from cv_maker import skills as sk
from cv_maker.jobs.requirements import JobRequirements
from cv_maker.models import Profile

MAX_QUESTIONS = 10
MAX_OPTIONAL = 6


class Gap:
    def __init__(self, term: str, status: str, score: int = 0, strength: str = "", found: list | None = None,
                 required: bool = True, level: str = "", same: bool = False, original: str = "") -> None:
        self.term = term
        self.status = status  # covered | partial | missing (kept for older code and saved jobs)
        self.score = score  # 0-100, from the evidence below
        self.strength = strength or {"covered": "strong", "partial": "partial"}.get(status, "none")
        self.found = found or []  # [{"where", "text", "why", "points"}]
        self.required = required
        self.level = level
        self.same = same  # found as this very skill (not only something related)
        self.original = original or term

    def to_dict(self) -> dict:
        return {"term": self.term, "status": self.status, "score": self.score, "strength": self.strength,
                "found": self.found, "required": self.required, "level": self.level, "same": self.same,
                "original": self.original}


class Question:
    def __init__(self, term: str, prompt: str, kind: str, required: bool, why: str = "", score: int = 0,
                 optional: bool = False, context: str = "") -> None:
        self.term = term
        self.prompt = prompt
        self.kind = kind  # "level": none / beginner / intermediate / expert (older jobs: "yesno")
        self.required = required  # a must-have of the posting
        self.why = why  # what was found, so you can see why it's asked
        self.score = score
        self.optional = optional  # your profile already shows it: a level only sharpens the wording
        self.context = context  # the posting's own words, when they say more than the skill's name

    def to_dict(self) -> dict:
        return {"term": self.term, "prompt": self.prompt, "kind": self.kind, "required": self.required, "why": self.why,
                "score": self.score, "optional": self.optional, "context": self.context}


class MatchResult:
    def __init__(self, gaps: list[Gap], questions: list[Question]) -> None:
        self.gaps = gaps
        self.questions = questions


def profile_corpus(profile: Profile) -> str:
    parts = [s.name for s in profile.skills]
    for exp in profile.experiences:
        parts.append(exp.title)
        parts.extend(exp.bullets)
    for edu in profile.education:
        parts.extend([edu.credential, edu.field])
    for value in profile.extras.values():
        parts.extend(value if isinstance(value, list) else [str(value)])
    return "\n".join(str(p) for p in parts).lower()


def mentions(term: str, text: str) -> bool:
    """Whole-word, case-insensitive match that copes with terms like C++, C# or .NET (and other names for it)."""
    return bool(term.strip()) and bool(sk.found_in(term, text))


def _ask(term: str, original: str, required: bool) -> str:
    if len(term.split()) > 4:  # a competency, not a single skill
        return f"How well does this describe you: {original[0].upper()}{original[1:]}?"
    return f"How much experience do you have with {term}?"


def _context(term: str, original: str) -> str:
    return original if original and sk.norm(original) != sk.norm(term) and len(term.split()) <= 4 else ""


def _why(gap: Gap) -> str:
    if not gap.found:
        return "Not found in your profile."
    top = gap.found[0]
    return f"Found: {top['why']}" + (f" — “{top['text'][:80]}”" if top["where"] != "Skills" else "") + f" ({gap.score}/100)."


def match_profile(profile: Profile, reqs: JobRequirements, levels: dict | None = None) -> MatchResult:
    """`levels`: the level you gave for a requirement, by term (from your answers)."""
    levels = {k.casefold(): v for k, v in (levels or {}).items()}
    gaps: list[Gap] = []
    for items, required in ((reqs.must_have, True), (reqs.nice_to_have, False)):
        for item in items:
            ev = sk.evidence(item.term, profile, levels.get(item.term.casefold(), ""))
            gaps.append(Gap(term=item.term, status=sk.STATUS[ev["strength"]], score=ev["score"], strength=ev["strength"],
                            found=ev["found"], required=required, level=ev["level"], same=ev["same"],
                            original=item.original))
    # Ask about what the evidence doesn't settle: must-haves with little or no evidence first (weakest first), then
    # nice-to-haves with little or none. Must-haves your profile already shows, though thinly, come last and are
    # optional: a level only sharpens how they're worded.
    unsure = sorted((g for g in gaps if g.required and g.score < sk.PARTIAL and not g.level), key=lambda g: g.score)
    unsure += sorted((g for g in gaps if not g.required and g.score < sk.PARTIAL and not g.level), key=lambda g: g.score)
    thin = sorted((g for g in gaps if g.required and sk.PARTIAL <= g.score < sk.STRONG and not g.level), key=lambda g: g.score)
    questions = [Question(term=g.term, prompt=_ask(g.term, g.original, g.required), kind="level", required=g.required,
                          why=_why(g), score=g.score, context=_context(g.term, g.original)) for g in unsure[:MAX_QUESTIONS]]
    questions += [Question(term=g.term, prompt=_ask(g.term, g.original, g.required), kind="level", required=g.required,
                           why=_why(g), score=g.score, optional=True, context=_context(g.term, g.original))
                  for g in thin[:MAX_OPTIONAL]]
    return MatchResult(gaps=gaps, questions=questions)


def ranked(gaps: list[dict]) -> list[dict]:
    """Strongest first; must-haves before nice-to-haves at the same score."""
    return sorted(gaps, key=lambda g: (-int(g.get("score") or 0), not g.get("required", True), str(g.get("term", "")).lower()))

