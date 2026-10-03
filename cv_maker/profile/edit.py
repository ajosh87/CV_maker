"""Turn the Profile edit form back into a Profile. Anything the user writes here becomes a confirmed fact."""
import re

from cv_maker.models import Education, Experience, Profile, Skill

EXTRA_LISTS = [
    ("certifications", "Certifications"),
    ("projects", "Projects"),
    ("languages", "Languages"),
    ("clarifications", "Facts you confirmed in answers"),
]


def _lines(text: str) -> list[str]:
    return [line.strip(" •-\t") for line in (text or "").splitlines() if line.strip(" •-\t")]


def _links(text: str) -> dict:
    links = {}
    for i, line in enumerate(_lines(text), 1):
        label, sep, url = line.partition(": ")
        if sep and not re.match(r"^https?$", label, re.I):
            links[label.strip().lower()] = url.strip()
        else:
            links[f"link{i}"] = line
    return links


def links_text(links: dict) -> str:
    return "\n".join(v if re.fullmatch(r"link\d+", k) else f"{k}: {v}" for k, v in links.items())


def _indices(form, prefix: str) -> list[int]:
    found = {int(m.group(1)) for key in form for m in [re.match(rf"{prefix}-(\d+)-", key)] if m}
    return sorted(found)


def profile_from_form(form, existing: Profile | None) -> Profile:
    old_sources = {s.name.lower(): s.source for s in (existing.skills if existing else [])}
    skills, seen = [], set()
    for name in _lines(form.get("skills", "")):
        if name.lower() not in seen:
            seen.add(name.lower())
            skills.append(Skill(name=name, source=old_sources.get(name.lower(), "user")))

    experiences = []
    for i in _indices(form, "exp"):
        get = lambda field: (form.get(f"exp-{i}-{field}") or "").strip()  # noqa: E731
        if form.get(f"exp-{i}-remove") or not (get("company") or get("title")):
            continue
        current = bool(form.get(f"exp-{i}-current"))
        experiences.append(Experience(
            company=get("company"), title=get("title"), location=get("location"), start=get("start"),
            end=None if current else (get("end") or None), current=current,
            bullets=_lines(form.get(f"exp-{i}-bullets", "")), confirmed=True,
        ))

    education = []
    for i in _indices(form, "edu"):
        get = lambda field: (form.get(f"edu-{i}-{field}") or "").strip()  # noqa: E731
        if form.get(f"edu-{i}-remove") or not (get("school") or get("credential")):
            continue
        education.append(Education(school=get("school"), credential=get("credential"), field=get("field"), dates=get("dates")))

    extras = {k: v for k, v in (existing.extras if existing else {}).items() if k not in dict(EXTRA_LISTS)}
    for key, _ in EXTRA_LISTS:
        values = _lines(form.get(f"extra-{key}", ""))
        if values:
            extras[key] = values

    return Profile(
        name=form.get("name", "").strip(),
        email=form.get("email", "").strip(),
        phone=form.get("phone", "").strip(),
        location=form.get("location", "").strip(),
        links=_links(form.get("links", "")),
        target_role=(form.get("target_role") or "").strip() or None,
        experiences=experiences,
        education=education,
        skills=skills,
        extras=extras,
    )
