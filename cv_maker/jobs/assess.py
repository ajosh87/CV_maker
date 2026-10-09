"""A recruiter's reading of the match: for each requirement the keyword search couldn't settle, one LLM request
works through what the posting needs, what in your CV shows it (or comes close), and what to ask you.

The keyword search (skills.py) finds a skill only where it is named. It misses work that plainly shows a skill in
other words ("deployed model services on EKS" is Kubernetes) and can't tell what the job uses a skill for, so its
questions were generic. This step reasons it through, one requirement at a time, in a fixed order (need → look →
verdict → question), and cites the bullets it relied on by id.

Its reply is checked before use: a cited bullet must exist, "shown" needs at least one, and anything malformed is
dropped. What survives is kept with the bullet's text (not its id), so it still applies after you edit your profile,
and only while that bullet is still there. The scores stay counted (match.py): the model's reading adds one
labelled line of evidence, never the whole score.
"""
import logging
import re

from cv_maker import skills as sk
from cv_maker.improve import bullet_ref, numbered_profile, role_ref
from cv_maker.llm.chat import complete_json
from cv_maker.models import Profile

logger = logging.getLogger("cv_maker.assess")

VERDICTS = ("shown", "adjacent", "absent")
MAX_ITEMS = 18
_TEXT_MAX = 300

_PROMPT = """You are assessing how well a candidate's CV matches a job, requirement by requirement, the way a careful
recruiter would before deciding what to ask them. Work through every requirement listed under "To assess" in this
order, and write each step down in the JSON in the same order:

1. need: what the posting actually needs here, in context: which responsibility it serves and how deep it must go.
2. look: search the numbered CV for this very skill, under any name or plainly shown by the work itself (for example
   "deployed model services on EKS" shows Kubernetes). Cite the ids you rely on (R2.B3 for a bullet, R2 for a title).
   Then look for adjacent evidence: related tools or similar work that could transfer. Cite only ids that appear below.
3. verdict: "shown" = a cited bullet or title plainly demonstrates this skill itself; "adjacent" = related experience,
   but not the skill itself; "absent" = nothing relevant. Keyword hits can mislead ("Go" inside "go-to-market"):
   judge what the text means, not whether the word appears.
4. question: unless the verdict is "shown" at the depth the job needs, write ONE question a recruiter would ask this
   candidate. Name what the job uses the skill for, point at the closest evidence by its role and company (never by
   id), and ask for something concrete: where, for what, at what scale, for how long. Never ask a generic
   "How much experience do you have with X?". Under 45 words. Empty string when nothing needs asking.
5. hint: what a useful answer contains, as a short example shape in brackets, e.g. "[where] — [what you built with it]
   — [scale or result]". Never write facts as if they were the candidate's.

The job
Title: {title}
What the role mostly does:
{responsibilities}

Job description (for context):
<<<
{jd}
>>>

The candidate's CV, numbered:
{profile}

To assess (requirement · must-have or nice-to-have · the posting's words · what the keyword search found):
{items}

Return one JSON object:
{{"items": [{{"term": "exactly as listed", "need": "...", "evidence": ["R2.B3"], "adjacent": ["R1.B1"],
             "reasoning": "one sentence: why this verdict", "verdict": "shown|adjacent|absent",
             "question": "...", "hint": "..."}}]}}
One entry per requirement listed, in the same order."""


def _found_line(gap: dict) -> str:
    found = gap.get("found") or []
    if not found:
        return "nothing"
    top = found[0]
    where = f"{top['where']}: “{top['text'][:70]}”" if top.get("where") != "Skills" else f"skills list: {top['text']}"
    return f"{gap.get('score', 0)}/100, {where}"


def needs_assessing(gaps: list[dict]) -> list[dict]:
    """What the keyword search didn't settle: everything short of strong, must-haves first, then weakest first."""
    weak = [g for g in gaps if int(g.get("score") or 0) < sk.STRONG and not g.get("level")]
    return sorted(weak, key=lambda g: (not g.get("required", True), int(g.get("score") or 0)))[:MAX_ITEMS]


def _cited(refs, profile: Profile) -> list[dict]:
    """The bullets and titles the model cited that really exist, as {"where", "text"}."""
    out = []
    for ref in refs if isinstance(refs, list) else []:
        ref = str(ref).strip()
        bullet = bullet_ref(ref, profile)
        if bullet is not None:
            exp = profile.experiences[bullet[0]]
            entry = {"where": f"{exp.title} at {exp.company}".strip(" at"), "text": exp.bullets[bullet[1]]}
        elif (role := role_ref(ref, profile)) is not None:
            exp = profile.experiences[role]
            entry = {"where": f"{exp.title} at {exp.company}".strip(" at"), "text": exp.title, "title": True}
        else:
            continue
        if entry not in out:
            out.append(entry)
    return out[:3]


def _clean(text, limit: int = _TEXT_MAX) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit].rstrip()


def _no_ids(text: str) -> str:
    """Questions are read by you, not the model: no "R2.B3" left in them."""
    return re.sub(r"\s*\(?\bR\d+(?:\.B\d+)?\b\)?", "", text).strip()


def assess(model, profile: Profile, requirements: dict, gaps: list[dict], jd_text: str, title: str = "") -> dict:
    """{term (casefolded): {"term", "verdict", "need", "reasoning", "question", "hint", "evidence", "adjacent"}}.
    Empty when there is nothing to assess. Raises on an LLM failure (the caller keeps the keyword match)."""
    todo = needs_assessing(gaps)
    if not todo:
        return {}
    items = "\n".join(f"- {g['term']} · {'must-have' if g.get('required', True) else 'nice-to-have'} · "
                      f"“{(g.get('original') or g['term'])[:120]}” · {_found_line(g)}" for g in todo)
    responsibilities = "\n".join(f"- {r}" for r in (requirements or {}).get("responsibilities") or []) or "(not stated)"
    data = complete_json(model, _PROMPT.format(title=title or (requirements or {}).get("job_title", "") or "(not stated)",
                                               responsibilities=responsibilities, jd=(jd_text or "")[:9000],
                                               profile=numbered_profile(profile), items=items))
    wanted = {g["term"].casefold(): g for g in todo}
    out: dict = {}
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("term") or "").strip().casefold()
        if key not in wanted or key in out:
            continue
        evidence, adjacent = _cited(raw.get("evidence"), profile), _cited(raw.get("adjacent"), profile)
        verdict = str(raw.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            verdict = "adjacent" if adjacent else "absent"
        if verdict == "shown" and not evidence:  # "shown" must point at something you can check
            verdict = "adjacent" if adjacent else "absent"
        question = _no_ids(_clean(raw.get("question")))
        out[key] = {"term": wanted[key]["term"], "verdict": verdict, "need": _clean(raw.get("need")),
                    "reasoning": _no_ids(_clean(raw.get("reasoning"))), "question": question if question.endswith("?") or len(question) > 12 else "",
                    "hint": _clean(raw.get("hint"), 160), "evidence": evidence if verdict == "shown" else [],
                    "adjacent": (adjacent if verdict != "shown" else []) or (evidence if verdict == "adjacent" else [])}
    logger.info("assess: %d of %d requirements read (%s)", len(out), len(todo),
                ", ".join(f"{v['verdict']}" for v in out.values()))
    return out


def still_there(entry: dict, profile: Profile) -> bool:
    """Whether a cited bullet or title is still in your profile, word for word."""
    for exp in profile.experiences:
        if entry.get("title") and entry["text"] == exp.title:
            return True
        if entry["text"] in exp.bullets:
            return True
    return False
