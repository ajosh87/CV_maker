from copy import deepcopy

from cv_maker.models import Education, Experience, Profile, Skill


def _role_key(exp: Experience) -> tuple:
    return (exp.company.lower(), exp.title.lower(), exp.start)


def merge_profile(existing: Profile | None, incoming: Profile) -> Profile:
    if existing is None:
        incoming.experiences = [
            Experience(
                company=e.company,
                title=e.title,
                location=e.location,
                start=e.start,
                end=e.end,
                current=e.current,
                bullets=list(e.bullets),
                confirmed=True,
            )
            for e in incoming.experiences
        ]
        return deepcopy(incoming)

    merged = deepcopy(existing)

    if not merged.name:
        merged.name = incoming.name
    if not merged.email:
        merged.email = incoming.email
    if not merged.phone:
        merged.phone = incoming.phone
    if not merged.location:
        merged.location = incoming.location
    merged.links = {**merged.links, **incoming.links}
    if incoming.target_role:
        merged.target_role = incoming.target_role

    incoming_by_key = {_role_key(e): e for e in incoming.experiences}
    merged_experiences = []
    seen = set()

    for existing_exp in merged.experiences:
        key = _role_key(existing_exp)
        seen.add(key)
        if key in incoming_by_key:
            inc = incoming_by_key[key]
            merged_exp = Experience(
                company=existing_exp.company,
                title=existing_exp.title,
                location=inc.location or existing_exp.location,
                start=existing_exp.start,
                end=inc.end if inc.end else existing_exp.end,
                current=inc.current if not inc.end else False,
                bullets=list(existing_exp.bullets) if existing_exp.confirmed else list(inc.bullets),
                confirmed=existing_exp.confirmed,
            )
            merged_experiences.append(merged_exp)
        else:
            merged_experiences.append(deepcopy(existing_exp))

    for inc in incoming.experiences:
        key = _role_key(inc)
        if key not in seen:
            merged_experiences.append(
                Experience(
                    company=inc.company,
                    title=inc.title,
                    location=inc.location,
                    start=inc.start,
                    end=inc.end,
                    current=inc.current,
                    bullets=list(inc.bullets),
                    confirmed=True,
                )
            )

    merged.experiences = merged_experiences

    existing_skills = {s.name.lower(): s for s in merged.skills}
    for s in incoming.skills:
        if s.name.lower() not in existing_skills:
            merged.skills.append(deepcopy(s))

    existing_education = {(e.school.lower(), e.credential.lower()): e for e in merged.education}
    for e in incoming.education:
        key = (e.school.lower(), e.credential.lower())
        if key not in existing_education:
            merged.education.append(deepcopy(e))

    merged.extras = {**merged.extras, **incoming.extras}

    return merged
