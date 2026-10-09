"""Improving a CV version from its ATS check: you pick items, the LLM checks them against your profile, you answer.

The ATS check (ats.py) is counted from the CV's text; nothing here changes how it scores. This module turns its
feedback into improvements you can choose: a must-have keyword your profile doesn't show, one that's only in the
skills list, results without numbers, roles without dates, missing contact details or education, the title, the
length. One LLM request then checks your picks against your profile ("can this be fixed from what's there, or only
with facts from you?") and words the questions. It never writes facts: every answer is yours, it goes into your
profile as a confirmed fact (or into the job's answers, for a skill's level), and the next version is written from
that and checked again.

The LLM's reply is validated before use: questions must point at bullets, roles and keywords that exist, kinds are
fixed by the item, and anything missing or malformed falls back to the standard question.
"""
import logging
import re
from copy import deepcopy

from cv_maker import skills as sk
from cv_maker.llm.chat import complete_json
from cv_maker.models import Education, Experience, Profile, Skill

logger = logging.getLogger("cv_maker.improve")

MAX_NUMBER_QUESTIONS = 4
VERDICTS = ("fixable", "ask", "unlikely")
LEVELS = ("none", "beginner", "intermediate", "expert", "skip")
TEXT_MAX = 400


# ---- what can be improved, from an ATS check ------------------------------------------------------------------

def improvable(check: dict) -> list[dict]:
    """The check's feedback as items you can pick: {"id", "label", "detail", "gain", "default", "group"}. `group` is
    the check it belongs to (keyword items sit under "must", "nice" or "context"); `gain` is the most it can add."""
    if not check:
        return []
    by_key = {c["key"]: c for c in check.get("checks") or []}
    keywords = check.get("keywords") or []
    must = [k for k in keywords if k.get("required")]
    nice = [k for k in keywords if not k.get("required")]
    found_must = [k for k in must if k.get("in_cv")]
    items = []
    for group, words, weight in (("must", must, 45), ("nice", nice, 10)):
        for k in words:
            if k.get("in_cv"):
                continue
            share = round(weight / len(words), 1) if words else 0
            if k.get("have"):
                items.append({"id": f"kw:{k['term']}", "group": group, "label": k["term"], "gain": share, "default": True,
                              "detail": "in your profile, missing from this CV: the next version uses it"})
            else:
                items.append({"id": f"kw:{k['term']}", "group": group, "label": k["term"], "gain": share,
                              "default": group == "must", "detail": "not in your profile: you're asked whether you have it"})
    for k in found_must:
        if not k.get("in_context"):
            items.append({"id": f"ctx:{k['term']}", "group": "context", "label": k["term"], "default": True,
                          "gain": round(10 / len(found_must), 1), "detail": "only in the skills list: show it in a role"})
    for key in ("title", "sections", "contact", "dates", "numbers", "length"):
        c = by_key.get(key)
        if c and not c.get("ok"):
            items.append({"id": key, "group": key, "label": c["label"], "detail": c.get("detail", ""),
                          "gain": round(c["max"] - c["points"], 1), "default": key in ("numbers", "dates", "contact")})
    return items


# ---- what the questions can point at ----------------------------------------------------------------------------

def _dated(e: Experience) -> str:
    end = "Present" if e.current else (e.end or "")
    return " – ".join(x for x in (e.start, end) if x) or "no dates"


def numbered_profile(profile: Profile) -> str:
    """The profile with every role (R1) and bullet (R1.B2) numbered, for the LLM to point at."""
    lines = []
    for i, e in enumerate(profile.experiences, 1):
        lines.append(f"R{i} {e.title} — {e.company} ({_dated(e)})")
        lines += [f"  R{i}.B{j} {b}" for j, b in enumerate(e.bullets, 1)]
    skills = ", ".join(f"{s.name} ({s.level})" if s.level else s.name for s in profile.skills)
    lines.append(f"Skills: {skills or 'none'}")
    for ed in profile.education:
        lines.append(f"Education: {', '.join(x for x in (ed.credential, ed.field, ed.school, ed.dates) if x)}")
    for key, values in (profile.extras or {}).items():
        if values and key != "clarifications":
            lines.append(f"{key.capitalize()}: {'; '.join(map(str, values))}")
    return "\n".join(lines)


def _numberless(profile: Profile) -> list[tuple[int, int, str]]:
    """(role index, bullet index, text) of bullets without a number, most recent roles first."""
    return [(i, j, b) for i, e in enumerate(profile.experiences) for j, b in enumerate(e.bullets) if not re.search(r"\d", b)]


def role_ref(ref: str, profile: Profile) -> int | None:
    m = re.fullmatch(r"R(\d+)", (ref or "").strip(), re.I)
    i = int(m.group(1)) - 1 if m else -1
    return i if 0 <= i < len(profile.experiences) else None


def bullet_ref(ref: str, profile: Profile) -> tuple[int, int] | None:
    m = re.fullmatch(r"R(\d+)\.B(\d+)", (ref or "").strip(), re.I)
    if not m:
        return None
    i, j = int(m.group(1)) - 1, int(m.group(2)) - 1
    if 0 <= i < len(profile.experiences) and 0 <= j < len(profile.experiences[i].bullets):
        return i, j
    return None


def _named_in(term: str, profile: Profile) -> int | None:
    """The role whose bullets name the keyword itself (under any of its names), most recent first."""
    return next((i for i, e in enumerate(profile.experiences) if any(sk.found_in(term, b) for b in e.bullets)), None)


def _likely_role(term: str, profile: Profile) -> int | None:
    """The role whose bullets mention the keyword, or its family (Docker for Kubernetes), most recent first."""
    for i, e in enumerate(profile.experiences):
        if any(sk.found_in(term, b) for b in e.bullets) or sk.found_in(term, e.title):
            return i
    for i, e in enumerate(profile.experiences):
        if any(sk.mentioned(term, b) for b in e.bullets):
            return i
    return None


# ---- the questions --------------------------------------------------------------------------------------------

def _standard(item: dict, profile: Profile, check: dict) -> dict:
    """The item's verdict and questions without the LLM: what the check itself knows."""
    kind, _, term = item["id"].partition(":")
    out = {"id": item["id"], "label": item["label"], "group": item["group"], "verdict": "ask", "why": "", "questions": []}
    if kind == "kw":
        k = next((k for k in check.get("keywords") or [] if k["term"] == term), {})
        if k.get("have"):
            out.update(verdict="fixable", why=f"Your profile shows {term}; the next version uses the posting's wording for it.")
        else:
            out.update(why=f"Nothing in your profile names {term}. It goes on the CV only if you have it.")
            out["questions"].append({"kind": "keyword", "term": term, "ask": f"Do you have experience with {term}? If so, how much, and where?",
                                     "role": _likely_role(term, profile)})
    elif kind == "ctx":
        named = _named_in(term, profile)
        if named is not None:
            e = profile.experiences[named]
            out.update(verdict="fixable", why=f"Your profile already shows {term} as {e.title} at {e.company}; the next version "
                                              "keeps it in that role. Add another line only if there's more to say.")
        else:
            out.update(why=f"{term} is only in your skills list. A line in a role that shows how you used it counts for more.")
        out["questions"].append({"kind": "where", "term": term, "ask": f"In which role did you use {term}, and for what?",
                                 "role": _likely_role(term, profile)})
    elif kind == "numbers":
        out.update(why="Results with numbers rank and read better. Add one only where you know it.")
        for i, j, text in _numberless(profile)[:MAX_NUMBER_QUESTIONS]:
            out["questions"].append({"kind": "bullet", "role": i, "bullet": j, "text": text,
                                     "ask": "Can you put a number on this (how many, how much, how fast)?"})
        if not out["questions"]:
            out.update(verdict="fixable", why="Every bullet in your profile already has a number; the rewrite keeps them.")
    elif kind == "dates":
        missing = [i for i, e in enumerate(profile.experiences) if not e.start]
        out.update(why="Tracking systems look for a start (and end) date on every role.")
        out["questions"] += [{"kind": "dates", "role": i, "ask": f"When did you start (and finish) as {profile.experiences[i].title} at "
                                                                f"{profile.experiences[i].company}?"} for i in missing]
    elif kind == "contact":
        out.update(why="A tracking system that can't find your email and phone may drop the CV.")
        out["questions"].append({"kind": "contact", "ask": "Your email address and phone number, as employers should see them."})
    elif kind == "sections":
        if not profile.education:
            out.update(why="Most tracking systems look for an Education section.")
            out["questions"].append({"kind": "education", "ask": "Your highest qualification: what, in which field, where and when?"})
        else:
            out.update(verdict="fixable", why="The next version writes every standard section your profile has facts for.")
    elif kind == "title":
        out.update(verdict="fixable", why="The next version is headed with the posting's job title.")
    elif kind == "length":
        short = "short" in (item.get("detail") or "")
        if short:
            out.update(why="The CV is short. Only more facts make it longer: add achievements to your roles.")
            for i, e in enumerate(profile.experiences[:3]):
                out["questions"].append({"kind": "add_bullet", "role": i, "ask": f"Another achievement as {e.title} at {e.company}?"})
        else:
            out.update(verdict="fixable", why="The next version is written tighter: shorter bullets, no repetition.")
            out["questions"].append({"kind": "length", "ask": "Write it tighter?"})
    return out


_PROMPT = """You are checking which improvements to a CV can be made honestly, before asking the candidate anything.
An applicant tracking system check (counted from the CV's text, not by you) scored the CV for this job: {title}.
The candidate picked these items to improve:
{items}

The candidate's profile: the only facts there are. Roles are numbered R1, R2…; bullets R1.B1, R1.B2…
{profile}

Lines of the job posting that mention the keywords:
{context}

For each item, decide:
- "fixable": it can be fixed from the profile as it is (say how, in one sentence);
- "ask": it needs facts only the candidate can give;
- "unlikely": the profile gives no sign of it (say so plainly; the candidate is still asked, never assumed to have it).
Then write the question to ask, where one is needed: short, specific, answerable in a line, about real facts (a number,
where a skill was used, dates). Never suggest an answer, never put words in the candidate's mouth.

Return one JSON object:
{{"items": [{{"id": "the item's id, exactly as given", "verdict": "fixable" | "ask" | "unlikely",
  "why": "one sentence grounded in the profile",
  "questions": [{{"target": "R1.B2 for a bullet, R1 for a role, or the keyword", "role": "R1 (optional: the role it most likely belongs to)",
                  "ask": "the question"}}]}}]}}
For "numbers", pick at most {max_numbers} bullets (R#.B#) where a number would fit naturally."""


def _context(items: list[dict], jd_text: str) -> str:
    lines = []
    for item in items:
        kind, _, term = item["id"].partition(":")
        if kind in ("kw", "ctx"):
            for line in (jd_text or "").splitlines():
                if line.strip() and sk.found_in(term, line):
                    lines.append(f"- {term}: {line.strip()[:240]}")
                    break
    return "\n".join(lines) or "(none)"


def _clean(text, limit: int = 240) -> str:
    return " ".join(str(text or "").split())[:limit]


def plan(model, profile: Profile, items: list[dict], check: dict, *, title: str = "", jd_text: str = "") -> dict:
    """Ask the LLM to check the picked items, then keep only what holds up: {"items": [...], "checked": bool}."""
    standard = {item["id"]: _standard(item, profile, check) for item in items}
    listing = "\n".join(f"[{item['id']}] {item['label']} — {item['detail']}" for item in items)
    prompt = _PROMPT.format(title=title or "the posting's role", items=listing, profile=numbered_profile(profile),
                            context=_context(items, jd_text), max_numbers=MAX_NUMBER_QUESTIONS)
    data = complete_json(model, prompt)
    replies = {str(r.get("id")): r for r in data.get("items") or [] if isinstance(r, dict)} if isinstance(data, dict) else {}
    out, dropped = [], 0
    for item in items:
        base = standard[item["id"]]
        reply = replies.get(item["id"])
        if not reply:
            out.append(base)
            continue
        merged = dict(base)
        verdict = str(reply.get("verdict") or "").lower()
        if verdict in _allowed_verdicts(item, base):
            merged["verdict"] = verdict
        if _clean(reply.get("why"), 300):
            merged["why"] = _clean(reply.get("why"), 300)
        asks = [q for q in reply.get("questions") or [] if isinstance(q, dict) and _clean(q.get("ask"))]
        questions, kept = [], 0
        if item["id"] == "numbers":
            for q in asks:
                ref = bullet_ref(str(q.get("target", "")), profile)
                if ref is None or re.search(r"\d", profile.experiences[ref[0]].bullets[ref[1]]) or kept >= MAX_NUMBER_QUESTIONS:
                    dropped += 1
                    continue
                questions.append({"kind": "bullet", "role": ref[0], "bullet": ref[1], "ask": _clean(q["ask"]),
                                  "text": profile.experiences[ref[0]].bullets[ref[1]]})
                kept += 1
            if questions:
                merged["questions"] = questions
        elif item["id"].startswith(("kw:", "ctx:")):
            # The kind of question is fixed by the item; the LLM words it and may name the likely role.
            for q in base["questions"]:
                reply_q = next((a for a in asks if sk.norm(str(a.get("target", ""))) == sk.norm(q["term"])), asks[0] if asks else None)
                if reply_q:
                    q = {**q, "ask": _clean(reply_q["ask"])}
                    role = role_ref(str(reply_q.get("role") or ""), profile)
                    if role is not None:
                        q["role"] = role
                questions.append(q)
            merged["questions"] = questions
        elif item["id"] == "dates":
            wording = {role_ref(str(a.get("target", "")), profile): _clean(a["ask"]) for a in asks}
            merged["questions"] = [{**q, "ask": wording.get(q["role"]) or q["ask"]} for q in base["questions"]]
        if merged["verdict"] == "fixable" and base["questions"] and not merged["questions"]:
            merged["questions"] = base["questions"]
        out.append(merged)
    if dropped:
        logger.info("improve: dropped %d question(s) that pointed at no bullet without a number", dropped)
    return {"items": out, "checked": True}


def _allowed_verdicts(item: dict, base: dict) -> tuple:
    """What the LLM may call an item, given the facts: a keyword your profile shows is fixable, full stop; one it
    doesn't can only be asked about ("unlikely" says the profile gives no sign of it); the rest are its call."""
    if item["id"].startswith("kw:"):
        return ("fixable",) if base["verdict"] == "fixable" else ("ask", "unlikely")
    if item["id"].startswith("ctx:"):
        return ("fixable",) if base["verdict"] == "fixable" else ("ask", "unlikely")
    if item["id"] in ("numbers", "dates", "contact"):
        return ("ask", "unlikely") if base["questions"] else ("fixable",)
    return VERDICTS


def standard_plan(profile: Profile, items: list[dict], check: dict) -> dict:
    """The questions without the LLM's check (when it fails, or you'd rather skip it)."""
    return {"items": [_standard(item, profile, check) for item in items], "checked": False}


def numbered_questions(the_plan: dict) -> list[tuple[str, dict, dict]]:
    """(field name, question, item) for every question, in order: q0, q1…"""
    out = []
    for item in the_plan.get("items") or []:
        for q in item.get("questions") or []:
            out.append((f"q{len(out)}", q, item))
    return out


# ---- your answers, into your profile ------------------------------------------------------------------------

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def apply(profile: Profile, the_plan: dict, form) -> tuple[Profile, list[dict], list[str], list[str]]:
    """Your answers as confirmed facts: (updated profile, skill levels for the job's answers, notes for the writer,
    problems). Bullets you edit replace the old wording; lines you add go into the role you chose; nothing you
    leave empty changes anything."""
    updated = deepcopy(profile)
    levels: list[dict] = []
    focus: list[str] = []
    problems: list[str] = []

    def text(name: str) -> str:
        value = " ".join(str(form.get(name, "")).split())
        if len(value) > TEXT_MAX:
            problems.append(f"Kept the first {TEXT_MAX} characters of one of your answers.")
            value = value[:TEXT_MAX]
        return value

    def role_of(name: str) -> int | None:
        raw = str(form.get(name, ""))
        return int(raw) if raw.isdigit() and int(raw) < len(updated.experiences) else None

    added_to = set()
    for name, q, item in numbered_questions(the_plan):
        kind = q["kind"]
        if kind == "bullet":
            new = text(name)
            i, j = q["role"], q["bullet"]
            if new and i < len(updated.experiences) and j < len(updated.experiences[i].bullets) and new != updated.experiences[i].bullets[j]:
                updated.experiences[i].bullets[j] = new
                updated.experiences[i].confirmed = True
                focus.append(f"The candidate added detail to this fact; keep its numbers exactly: “{new}”")
        elif kind in ("keyword", "where"):
            term = q["term"]
            level = str(form.get(f"{name}_level", "")) if kind == "keyword" else ""
            line, role = text(f"{name}_text"), role_of(f"{name}_role")
            if kind == "keyword" and level in LEVELS and level != "skip":
                levels.append({"term": term, "level": level})
            has_it = kind == "where" or level in ("beginner", "intermediate", "expert")
            if line and role is not None and has_it:
                if not sk.found_in(term, line):
                    line = f"{line} ({term})"
                updated.experiences[role].bullets.append(line)
                updated.experiences[role].confirmed = True
                added_to.add(role)
                focus.append(f"Show {term} in the {updated.experiences[role].title} role, from this confirmed fact: “{line}”")
            elif line and role is None and has_it:
                problems.append(f"Pick the role where you used {term}, so the line can go there.")
            if kind == "keyword" and level in ("beginner", "intermediate", "expert") and not any(
                    sk.variants(term) & sk.variants(s.name) for s in updated.skills):
                updated.skills.append(Skill(name=term, source="user", level=level))
        elif kind == "add_bullet":
            line = text(name)
            if line and q["role"] < len(updated.experiences):
                updated.experiences[q["role"]].bullets.append(line)
                updated.experiences[q["role"]].confirmed = True
        elif kind == "dates":
            i = q["role"]
            start, end = text(f"{name}_start")[:40], text(f"{name}_end")[:40]
            current = bool(form.get(f"{name}_current"))
            if start and i < len(updated.experiences):
                e = updated.experiences[i]
                e.start, e.current = start, current
                e.end = None if current else (end or None)
        elif kind == "contact":
            email, phone = text(f"{name}_email"), text(f"{name}_phone")
            if email and not _EMAIL.match(email):
                problems.append(f"“{email}” doesn't look like an email address, so it wasn't saved.")
            elif email:
                updated.email = email
            if phone and len(re.sub(r"\D", "", phone)) < 7:
                problems.append(f"“{phone}” doesn't look like a phone number, so it wasn't saved.")
            elif phone:
                updated.phone = phone
        elif kind == "education":
            credential, field_, school, dates = (text(f"{name}_{k}")[:120] for k in ("credential", "field", "school", "dates"))
            if credential or school:
                updated.education.append(Education(school=school, credential=credential, field=field_, dates=dates))
        elif kind == "length":
            if form.get(name) == "tighter":
                focus.append("Write it tighter: shorter bullets, no repeated points, the most relevant roles in most detail.")
        if kind == "keyword" and item.get("verdict") == "unlikely" and level in ("intermediate", "expert"):
            logger.info("improve: %s marked %s although the profile gave no sign of it", q["term"], level)
    return updated, levels, focus, problems


def keyword_focus(the_plan: dict) -> list[str]:
    """Keywords you already have that this version left out: the next one must use them."""
    items = the_plan.get("items") or []
    terms = [i["label"] for i in items if i["id"].startswith("kw:") and i["verdict"] == "fixable"]
    shown = [i["label"] for i in items if i["id"].startswith("ctx:") and i["verdict"] == "fixable"]
    lines = [f"Use these keywords from the posting, in its wording, where the profile supports them: {', '.join(terms)}."] if terms else []
    if shown:
        lines.append(f"Name these in the bullets of the roles where the profile shows them, not only in the skills list: {', '.join(shown)}.")
    return lines


# ---- did it help? -------------------------------------------------------------------------------------------

def compare(before: dict, after: dict) -> dict:
    """Two ATS checks of the same job, check by check: {"before", "after", "delta", "checks": [...]}."""
    if not before or not after:
        return {}
    old = {c["key"]: c for c in before.get("checks") or []}
    rows = []
    for c in after.get("checks") or []:
        prev = old.get(c["key"])
        if prev is None:
            continue
        rows.append({"key": c["key"], "label": c["label"], "before": prev["points"], "after": c["points"], "max": c["max"],
                     "delta": round(c["points"] - prev["points"], 1)})
    return {"before": before.get("score", 0), "after": after.get("score", 0),
            "delta": after.get("score", 0) - before.get("score", 0), "checks": rows}


def outcomes(items: list[dict], after: dict) -> list[dict]:
    """What each pick came to in the new version's check: {"label", "done", "detail"}."""
    keywords = {k["term"]: k for k in (after or {}).get("keywords") or []}
    checks = {c["key"]: c for c in (after or {}).get("checks") or []}
    out = []
    for item in items:
        kind, _, term = item["id"].partition(":")
        if kind == "kw":
            done = bool(keywords.get(term, {}).get("in_cv"))
            detail = "now in the CV" if done else "still missing: not confirmed, so not claimed"
        elif kind == "ctx":
            done = bool(keywords.get(term, {}).get("in_context"))
            detail = "now shown in a role" if done else "still only in the skills list"
        else:
            c = checks.get(item["id"], {})
            done = bool(c.get("ok"))
            detail = c.get("detail", "") if c else ""
        out.append({"label": item["label"], "done": done, "detail": detail})
    return out
