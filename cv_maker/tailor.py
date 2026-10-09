"""Choosing what a job needs from your CV, before a word is written.

Writing used to rewrite every bullet of every role, in order, so a long profile came out long and unfocused: the
roles that matter for the job got no more room than the ones that don't. Now one LLM request first plans the CV
(what the job is mostly about → how much each role serves it → which bullets prove it → which skills matter), and
the writing works from that plan.

The plan only chooses and orders; it never words anything. Its choices are checked and bounded here, by rules you
can read: roles are never dropped (your history and dates stay complete; a role that doesn't serve the job keeps
its title and dates with fewer or no bullets), every bullet it names must exist, each role gets at most a set number
of bullets by how much it serves the job, your current role keeps at least two, and a bullet that is the evidence
for a must-have keyword stays even if the plan left it out. A plan that fails or comes back empty falls back to
every bullet, as before.
"""
import logging

from cv_maker import skills as sk
from cv_maker.improve import bullet_ref, numbered_profile, role_ref
from cv_maker.llm.chat import complete_json
from cv_maker.models import Profile

logger = logging.getLogger("cv_maker.tailor")

TIERS = ("core", "supporting", "peripheral")
CAPS = {"core": 6, "supporting": 3, "peripheral": 1}
MAX_BULLETS = 26  # about two pages with the rest of the CV

_PROMPT = """You are planning how to tailor a CV to one job, the way an experienced recruiter would edit it. Nothing
is written yet: you only choose and order. Work through these steps in order and record each in the JSON:

1. needs: what this job is mostly about. List the 4-6 needs that matter most to the hiring manager, most important
   first, from the responsibilities and must-haves (not from boilerplate).
2. roles: for every role (R1, R2, ...) decide how much its work serves those needs: "core" (directly the same kind
   of work), "supporting" (useful, transferable), "peripheral" (little to do with this job). Judge by the work, not
   by seniority or how recent it is.
3. bullets: pick the bullets that prove the needs, each with relevance 3 (direct proof of a top need), 2 (supports a
   need) or 1 (a general strength this job values: scale, ownership, results). Leave out bullets that don't serve
   this job; they stay in the profile, just not on this CV. Name the need each one serves.
4. skills_first: the candidate's skills (exactly as listed) that matter for this job, most relevant first.
   skills_drop: listed skills that are irrelevant to this job and would only dilute it.
5. angle: one sentence on how to position this candidate for this job, from their strongest true evidence.

The job
Title: {title}
What the role mostly does:
{responsibilities}
Must-haves: {must}
Nice-to-haves: {nice}
What the candidate has, from the match: {have}

Job description:
<<<
{jd}
>>>

The candidate's CV, numbered:
{profile}

Return one JSON object:
{{"needs": ["..."],
  "roles": [{{"id": "R1", "tier": "core|supporting|peripheral", "why": "a few words"}}],
  "bullets": [{{"id": "R1.B2", "relevance": 3, "need": "which need it proves"}}],
  "skills_first": ["..."], "skills_drop": ["..."],
  "angle": "..."}}"""


def plan(model, profile: Profile, requirements: dict, gaps: list[dict], jd_text: str, title: str = "") -> dict:
    """The model's plan, validated: {"needs", "angle", "tiers": {role: tier}, "relevance": {(role, bullet): 1-3},
    "serves": {(role, bullet): need}, "skills_first": [...], "skills_drop": [...]}. Raises on an LLM failure."""
    reqs = requirements or {}
    have = [g["term"] for g in gaps or [] if g.get("same") and int(g.get("score") or 0) >= sk.PARTIAL or g.get("level") in sk.LEVELS]
    data = complete_json(model, _PROMPT.format(
        title=title or reqs.get("job_title", "") or "(not stated)",
        responsibilities="\n".join(f"- {r}" for r in reqs.get("responsibilities") or []) or "(not stated)",
        must=", ".join(m.get("term", "") for m in reqs.get("must_have") or []) or "(none)",
        nice=", ".join(m.get("term", "") for m in reqs.get("nice_to_have") or []) or "(none)",
        have=", ".join(have) or "(none yet)", jd=(jd_text or "")[:9000], profile=numbered_profile(profile)))
    return validate(data, profile)


def validate(data: dict, profile: Profile) -> dict:
    tiers: dict[int, str] = {}
    for r in data.get("roles") or []:
        if isinstance(r, dict) and (i := role_ref(str(r.get("id", "")), profile)) is not None:
            tier = str(r.get("tier") or "").strip().lower()
            if tier in TIERS:
                tiers.setdefault(i, tier)
    relevance: dict[tuple[int, int], int] = {}
    serves: dict[tuple[int, int], str] = {}
    for b in data.get("bullets") or []:
        if not isinstance(b, dict) or (ref := bullet_ref(str(b.get("id", "")), profile)) is None:
            continue
        try:
            level = max(1, min(3, int(b.get("relevance") or 1)))
        except (TypeError, ValueError):
            level = 1
        relevance[ref] = max(level, relevance.get(ref, 0))
        if b.get("need"):
            serves[ref] = " ".join(str(b["need"]).split())[:120]
    names = {sk.norm(s.name): s.name for s in profile.skills}

    def skill_list(key: str) -> list[str]:
        out = []
        for name in data.get(key) or []:
            real = names.get(sk.norm(str(name)))
            if real and real not in out:
                out.append(real)
        return out

    first = skill_list("skills_first")
    drop = [s for s in skill_list("skills_drop") if s not in first]
    return {"needs": [" ".join(str(n).split())[:160] for n in (data.get("needs") or []) if str(n).strip()][:6],
            "angle": " ".join(str(data.get("angle") or "").split())[:300], "tiers": tiers, "relevance": relevance,
            "serves": serves, "skills_first": first, "skills_drop": drop}


def _evidence_bullets(profile: Profile, gaps: list[dict]) -> list[tuple[int, int]]:
    """For each must-have you have, the bullet that best shows it: these keep the keyword in context."""
    out = []
    for g in gaps or []:
        if not g.get("required", True):
            continue
        for f in g.get("found") or []:
            hit = next(((i, j) for i, e in enumerate(profile.experiences) for j, b in enumerate(e.bullets) if b == f.get("text")), None)
            if hit is not None:
                if hit not in out:
                    out.append(hit)
                break
    return out


def select(profile: Profile, tailoring: dict | None, gaps: list[dict] | None = None,
           keep: list[str] | None = None) -> list[list[int]]:
    """Which bullets of each role go on this CV, most relevant first. Without a usable plan: all of them, in order.
    `keep`: bullet texts that must stay (facts you just confirmed to improve a version)."""
    relevance = (tailoring or {}).get("relevance") or {}
    if not relevance:
        return [list(range(len(e.bullets))) for e in profile.experiences]
    tiers = (tailoring or {}).get("tiers") or {}
    chosen: list[list[int]] = []
    for i, exp in enumerate(profile.experiences):
        picked = sorted((j for j in range(len(exp.bullets)) if (i, j) in relevance), key=lambda j: (-relevance[(i, j)], j))
        tier = tiers.get(i) or ("supporting" if picked else "peripheral")
        if tier == "peripheral":
            picked = [j for j in picked if relevance[(i, j)] >= 2]
        picked = picked[:CAPS[tier]]
        if i == 0 and len(picked) < 2:  # your latest role is read first: never leave it bare
            picked += [j for j in range(len(exp.bullets)) if j not in picked][:2 - len(picked)]
        chosen.append(picked)
    kept = {(i, j) for i, e in enumerate(profile.experiences) for j, b in enumerate(e.bullets) if b in set(keep or [])}
    must_keep = set(_evidence_bullets(profile, gaps or [])) | kept
    for i, j in sorted(must_keep):
        if j not in chosen[i]:
            chosen[i].append(j)
    # Over the length budget: drop the least relevant bullets of the oldest roles first.
    total = sum(len(c) for c in chosen)
    while total > MAX_BULLETS:
        candidates = [(relevance.get((i, j), 0), -i, i, j) for i, c in enumerate(chosen) for j in c
                      if (i, j) not in must_keep and not (i == 0 and len(c) <= 2)]
        if not candidates:
            break
        _, _, i, j = min(candidates)
        chosen[i].remove(j)
        total -= 1
    return chosen


def summary(profile: Profile, tailoring: dict | None, chosen: list[list[int]]) -> dict:
    """What this version emphasises, for the page: the needs, the angle and how much of each role is used."""
    tiers = (tailoring or {}).get("tiers") or {}
    roles = [{"role": f"{e.title} — {e.company}".strip(" —"), "tier": tiers.get(i, ""), "kept": len(chosen[i]),
              "of": len(e.bullets)} for i, e in enumerate(profile.experiences)]
    return {"needs": (tailoring or {}).get("needs") or [], "angle": (tailoring or {}).get("angle") or "", "roles": roles,
            "kept": sum(len(c) for c in chosen), "of": sum(len(e.bullets) for e in profile.experiences),
            "planned": bool((tailoring or {}).get("relevance"))}


def numbered_selection(profile: Profile, chosen: list[list[int]], tailoring: dict | None) -> str:
    """The bullets to write, by id, with the need each one serves."""
    serves = (tailoring or {}).get("serves") or {}
    tiers = (tailoring or {}).get("tiers") or {}
    lines = []
    for i, exp in enumerate(profile.experiences):
        tier = f" [{tiers[i]}]" if i in tiers else ""
        lines.append(f"R{i + 1} {exp.title} — {exp.company}{tier}")
        if not chosen[i]:
            lines.append("  (no bullets: title and dates only)")
        for j in chosen[i]:
            need = f"  ← {serves[(i, j)]}" if (i, j) in serves else ""
            lines.append(f"  R{i + 1}.B{j + 1} {exp.bullets[j]}{need}")
    return "\n".join(lines)
