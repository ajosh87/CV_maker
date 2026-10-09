from cv_maker import skills as sk
from cv_maker.jobs.assess import still_there
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
                 optional: bool = False, context: str = "", hint: str = "", need: str = "") -> None:
        self.term = term
        self.prompt = prompt
        self.kind = kind  # "level": none / beginner / intermediate / expert (older jobs: "yesno")
        self.required = required  # a must-have of the posting
        self.why = why  # what was found, so you can see why it's asked
        self.score = score
        self.optional = optional  # your profile already shows it: a level only sharpens the wording
        self.context = context  # the posting's own words, when they say more than the skill's name
        self.hint = hint  # what a useful answer contains (from the assessment)
        self.need = need  # what the job uses it for (from the assessment)

    def to_dict(self) -> dict:
        return {"term": self.term, "prompt": self.prompt, "kind": self.kind, "required": self.required, "why": self.why,
                "score": self.score, "optional": self.optional, "context": self.context, "hint": self.hint,
                "need": self.need}


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


JUDGED_SHOWN, JUDGED_ADJACENT = 40, 10


def _with_reading(ev: dict, reading: dict | None, profile: Profile) -> dict:
    """Add the assessment's reading (assess.py) to the counted evidence, as one labelled line: a bullet it read as
    this very skill (+40, enough for "partial" on its own), or related work (+10). A reading whose bullet you've
    since edited away no longer counts. When it reads the keyword hits as something else ("Go" in
    "go-to-market"), they stop counting as the skill itself until you answer."""
    if not reading:
        return ev
    found = list(ev["found"])
    same = ev["same"]
    shown = [e for e in reading.get("evidence") or [] if still_there(e, profile)]
    adjacent = [e for e in reading.get("adjacent") or [] if still_there(e, profile)]
    reason = reading.get("reasoning") or ""
    known = {f["text"] for f in found}
    if reading.get("verdict") == "shown" and shown:
        top = next((e for e in shown if e["text"] not in known), None)
        if top is not None:
            found.append({"where": top["where"], "text": top["text"], "judged": True, "points": JUDGED_SHOWN,
                          "why": f"read as this skill{': ' + reason if reason else ''}"})
        same = True
    elif reading.get("verdict") == "adjacent" and adjacent and not same:
        top = adjacent[0]
        found.append({"where": top["where"], "text": top["text"], "judged": True, "points": JUDGED_ADJACENT,
                      "why": f"related work{': ' + reason if reason else ''}"})
    elif reading.get("verdict") == "absent" and not ev["level"] and ev["score"] < sk.STRONG and             not any(f["where"] in ("Skills", "Your answer") for f in found):
        found = [{**f, "points": 0, "why": f"{f['why']} (read as something else{': ' + reason if reason else ''})"}
                 for f in found]
        same = False
    score = min(100, sum(f["points"] for f in found))
    if not same:
        score = min(score, sk.PARTIAL - 1)
    if ev["level"] == "beginner":
        score = min(score, sk.STRONG - 1)
    strength = "strong" if score >= sk.STRONG else "partial" if score >= sk.PARTIAL else "weak" if score > 0 else "none"
    return {**ev, "found": sorted(found, key=lambda f: -f["points"]), "score": score, "same": same, "strength": strength}


def _prompt(g: Gap, reading: dict | None) -> str:
    return (reading or {}).get("question") or _ask(g.term, g.original, g.required)


def _why_read(g: Gap, reading: dict | None) -> str:
    return (reading or {}).get("reasoning") or _why(g)


def match_profile(profile: Profile, reqs: JobRequirements, levels: dict | None = None,
                  assessment: dict | None = None) -> MatchResult:
    """`levels`: the level you gave for a requirement, by term (from your answers). `assessment`: the reading of
    each requirement from assess.py, by casefolded term."""
    levels = {k.casefold(): v for k, v in (levels or {}).items()}
    assessment = assessment or {}
    gaps: list[Gap] = []
    for items, required in ((reqs.must_have, True), (reqs.nice_to_have, False)):
        for item in items:
            ev = sk.evidence(item.term, profile, levels.get(item.term.casefold(), ""))
            ev = _with_reading(ev, assessment.get(item.term.casefold()), profile)
            gaps.append(Gap(term=item.term, status=sk.STATUS[ev["strength"]], score=ev["score"], strength=ev["strength"],
                            found=ev["found"], required=required, level=ev["level"], same=ev["same"],
                            original=item.original))
    # Ask about what the evidence doesn't settle: must-haves with little or no evidence first (weakest first), then
    # nice-to-haves with little or none. Must-haves your profile already shows, though thinly, come last and are
    # optional: a level only sharpens how they're worded.
    unsure = sorted((g for g in gaps if g.required and g.score < sk.PARTIAL and not g.level), key=lambda g: g.score)
    unsure += sorted((g for g in gaps if not g.required and g.score < sk.PARTIAL and not g.level), key=lambda g: g.score)
    thin = sorted((g for g in gaps if g.required and sk.PARTIAL <= g.score < sk.STRONG and not g.level), key=lambda g: g.score)
    def question(g: Gap, optional: bool = False) -> Question:
        reading = assessment.get(g.term.casefold())
        return Question(term=g.term, prompt=_prompt(g, reading), kind="level", required=g.required, why=_why_read(g, reading),
                        score=g.score, optional=optional, context=_context(g.term, g.original),
                        hint=(reading or {}).get("hint", ""), need=(reading or {}).get("need", ""))

    questions = [question(g) for g in unsure[:MAX_QUESTIONS]] + [question(g, True) for g in thin[:MAX_OPTIONAL]]
    return MatchResult(gaps=gaps, questions=questions)


def ranked(gaps: list[dict]) -> list[dict]:
    """Strongest first; must-haves before nice-to-haves at the same score."""
    return sorted(gaps, key=lambda g: (-int(g.get("score") or 0), not g.get("required", True), str(g.get("term", "")).lower()))

