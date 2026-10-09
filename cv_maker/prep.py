"""Interview prep notes for one job: what to brush up on, likely questions (and which of your own experiences
to draw on), questions to ask them, and things to watch out for.

One LLM request per job, built from what the app already has (the requirements it read, how your profile
matches them, the posting's notes, what the application form asked, and the company research), so nothing is
read or paid for twice. Company facts come only from the research agent (research.py), with its sources.
"""
from datetime import datetime, timezone

from cv_maker.llm.chat import complete_json


def company_section(found: dict | None) -> str:
    """What the research agent found, with sources, for the prompt."""
    if not found:
        return ""
    parts = []
    if found.get("basics"):
        parts.append(f"(Wikipedia) {found['basics']['summary'][:800]}")
    if found.get("facts"):
        parts.append("(Wikidata) " + "; ".join(f"{k}: {v}" for k, v in found["facts"].items()))
    for point in ((found.get("summary") or {}).get("points") or [])[:6]:
        parts.append(f"({point['source']}) {point['point']}")
    return "\n".join(parts)


_PROMPT = """You are helping a candidate prepare for interviews for one job. Be specific to this job and this candidate.
Use only the candidate profile for anything about the candidate: never invent experience, numbers or skills.
State company facts only from the "Company" section; if it's empty, give no company facts.

Job: {title} at {company}
Must-haves and how the candidate matches them: {gaps}
Nice to have: {nice}
From the posting: {notes}
The application form asks about: {asked}
Company: {company_info}

Job description (shortened):
<<<
{jd}
>>>

Candidate profile:
{profile}

Return one JSON object:
{{"role_in_brief": "2-3 sentences on what this job is really about",
  "brush_up": [{{"topic": "...", "why": "...", "status": "strong | refresh | gap"}}],
  "likely_questions": [{{"question": "...", "kind": "technical | behavioural | about a gap",
                         "draw_on": "which experience from the profile to use, or how to answer honestly about a gap"}}],
  "ask_them": ["questions the candidate could ask the interviewers"],
  "watch_outs": ["things to clarify or be ready for, only from the posting or the application"],
  "company_points": ["facts worth knowing, only from the Company section"]}}
At most 8 brush_up, 10 likely_questions, 5 ask_them, 5 watch_outs, 5 company_points."""

REVIEWS_LIMIT = 8000  # characters of pasted reviews summarised in the one request

_REVIEWS_PROMPT = """Summarise these reviews of {company} that the candidate pasted. Report only what the reviews say;
note how many reviews mention each point when you can.

Reviews:
<<<
{reviews}
>>>

Return one JSON object:
{{"overall": "one or two sentences", "pros": ["..."], "cons": ["..."], "interview_experiences": ["..."],
  "ask_about": ["things worth asking in an interview because of these reviews"]}}
At most 6 items per list."""


def _strings(value, limit: int, length: int = 300) -> list[str]:
    return [str(v).strip()[:length] for v in (value or []) if str(v).strip()][:limit]


def _items(value, keys: tuple, limit: int) -> list[dict]:
    out = []
    for item in (value or [])[:limit]:
        if isinstance(item, dict) and str(item.get(keys[0]) or "").strip():
            out.append({k: str(item.get(k) or "").strip()[:400] for k in keys})
    return out


def write_notes(run, profile_text: str, model, research: dict | None) -> dict:
    reqs = run.requirements or {}
    labels = {"covered": "covered", "partial": "partly", "missing": "not in the profile"}
    gaps = "; ".join(f"{g['term']} ({labels.get(g['status'], g['status'])})" for g in run.gaps) or "none listed"
    asked = [q["label"] for q in (run.scan or {}).get("questions", [])][:15]
    asked += [d["what"] for d in (run.scan or {}).get("documents", [])]
    prompt = _PROMPT.format(
        title=run.title or reqs.get("job_title") or "the role", company=run.company or reqs.get("company") or "the company",
        gaps=gaps, nice=", ".join(t["term"] for t in reqs.get("nice_to_have", [])) or "none listed",
        notes="; ".join(reqs.get("notes") or []) or "nothing in particular",
        asked=", ".join(asked) or "not checked yet",
        company_info=company_section(research) or "(nothing)",
        jd=(run.jd_text or "")[:6000], profile=profile_text,
    )
    data = complete_json(model, prompt)
    return {
        "role_in_brief": str(data.get("role_in_brief") or "").strip()[:800],
        "brush_up": _items(data.get("brush_up"), ("topic", "why", "status"), 8),
        "likely_questions": _items(data.get("likely_questions"), ("question", "kind", "draw_on"), 10),
        "ask_them": _strings(data.get("ask_them"), 5),
        "watch_outs": _strings(data.get("watch_outs"), 5),
        "company_points": _strings(data.get("company_points"), 5) if company_section(research) else [],
    }


def summarise_reviews(text: str, company: str, model) -> dict:
    used = text[:REVIEWS_LIMIT]
    data = complete_json(model, _REVIEWS_PROMPT.format(company=company or "the company", reviews=used))
    return {"overall": str(data.get("overall") or "").strip()[:500],
            **{k: _strings(data.get(k), 6) for k in ("pros", "cons", "interview_experiences", "ask_about")},
            "summarised_at": datetime.now(timezone.utc).isoformat(), "chars": len(used), "pasted": len(text)}
