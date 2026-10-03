"""CV text -> Profile via the LLM, with defensive coercion of whatever shape comes back."""
from cv_maker.llm.chat import ChatModel, complete_json
from cv_maker.models import Education, Experience, Profile, Skill

_PROMPT = """Extract a structured profile from this CV. Copy facts exactly as written; do not invent,
infer or embellish anything. Use "" or [] for anything the CV does not state.
Return one JSON object with exactly these keys:
{{
  "name": "", "email": "", "phone": "", "location": "",
  "links": {{"linkedin": "", "github": "", "portfolio": ""}},
  "experiences": [{{"company": "", "title": "", "location": "", "start": "", "end": "or null if ongoing",
                    "current": false, "bullets": ["achievement lines for this role, verbatim"]}}],
  "education": [{{"school": "", "credential": "", "field": "", "dates": ""}}],
  "skills": ["each skill, tool or technology the CV names"],
  "certifications": [""], "projects": [""], "languages": [""]
}}
List experiences most recent first. Set "current": true and "end": null when a role says Present/Current.

CV text:
<<<
{cv_text}
>>>"""


def _s(value) -> str:
    return "" if value is None else str(value).strip()


def _list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def profile_from_json(data: dict) -> Profile:
    links = data.get("links") or {}
    if isinstance(links, list):
        links = {f"link{i + 1}": _s(v) for i, v in enumerate(links) if _s(v)}
    links = {str(k): _s(v) for k, v in links.items() if _s(v)}

    experiences = []
    for e in _list(data.get("experiences")):
        if not isinstance(e, dict) or not (_s(e.get("company")) or _s(e.get("title"))):
            continue
        end = _s(e.get("end")) or None
        current = bool(e.get("current")) or (end or "").lower() in {"present", "current", "now"}
        experiences.append(Experience(
            company=_s(e.get("company")),
            title=_s(e.get("title")),
            location=_s(e.get("location")),
            start=_s(e.get("start")),
            end=None if current else end,
            current=current,
            bullets=[_s(b) for b in _list(e.get("bullets")) if _s(b)],
            confirmed=bool(e.get("confirmed", True)),
        ))

    education = [
        Education(school=_s(e.get("school")), credential=_s(e.get("credential")), field=_s(e.get("field")), dates=_s(e.get("dates")))
        for e in _list(data.get("education"))
        if isinstance(e, dict) and (_s(e.get("school")) or _s(e.get("credential")))
    ]

    skills, seen = [], set()
    for s in _list(data.get("skills")):
        name = _s(s.get("name")) if isinstance(s, dict) else _s(s)
        source = s.get("source", "cv") if isinstance(s, dict) else "cv"
        if name and name.lower() not in seen:
            seen.add(name.lower())
            skills.append(Skill(name=name, source=source if source in ("cv", "clarification") else "cv"))

    extras = dict(data.get("extras") or {}) if isinstance(data.get("extras"), dict) else {}
    for key in ("certifications", "projects", "languages"):
        values = [_s(v) for v in _list(data.get(key)) if _s(v)]
        if values:
            extras[key] = values

    return Profile(
        name=_s(data.get("name")),
        email=_s(data.get("email")),
        phone=_s(data.get("phone")),
        location=_s(data.get("location")),
        links=links,
        target_role=None,  # only ever set from the UI, never inferred from a CV (spec)
        experiences=experiences,
        education=education,
        skills=skills,
        extras=extras,
    )


def parse_cv_profile(cv_text: str, model: ChatModel) -> Profile:
    return profile_from_json(complete_json(model, _PROMPT.format(cv_text=cv_text[:30000])))


def profile_summary(profile: Profile) -> dict:
    return {
        "name": profile.name,
        "email": profile.email,
        "phone": profile.phone,
        "location": profile.location,
        "roles": [f"{e.title} — {e.company}" for e in profile.experiences],
        "skills": [s.name for s in profile.skills],
        "education": [", ".join(x for x in (e.credential, e.field, e.school) if x) for e in profile.education],
    }
