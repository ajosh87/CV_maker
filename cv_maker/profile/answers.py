from copy import deepcopy
from dataclasses import dataclass

from cv_maker.jobs.match import Question
from cv_maker.models import Experience, Profile, Skill


LEVELS = ("beginner", "intermediate", "expert")


@dataclass
class Answer:
    term: str
    skipped: bool
    text: str
    add_role: dict | None
    level: str = ""  # beginner | intermediate | expert | none (no experience) | "" (skipped, or an older yes/no answer)

    @property
    def is_yes(self) -> bool:
        if self.skipped or self.level == "none":
            return False
        return self.level in LEVELS or self.text.strip().lower().startswith("y")

    @property
    def detail(self) -> str:
        head, _, tail = self.text.partition(":")
        return tail.strip() if head.strip().lower() in ("yes", "y", *LEVELS) else ""


def answers_from_dicts(raw: list) -> list[Answer]:
    return [
        Answer(term=a.get("term", ""), skipped=bool(a.get("skipped", False)), text=a.get("text", ""), add_role=a.get("add_role"),
               level=str(a.get("level") or ""))
        for a in raw or []
        if isinstance(a, dict)
    ]


def levels_of(answers: list[Answer]) -> dict:
    """{term: level} for the answers that gave one."""
    return {a.term: a.level for a in answers if a.level in LEVELS}


def apply_answers(profile: Profile, questions: list[Question], answers: list[Answer]) -> Profile:
    answer_map = {a.term: a for a in answers}
    merged = deepcopy(profile)
    known_skills = {s.name.lower() for s in merged.skills}
    for q in questions:
        a = answer_map.get(q.term)
        if a is None or a.skipped:
            continue
        if q.kind in ("yesno", "level"):
            if a.is_yes:
                level = a.level if a.level in LEVELS else ""
                existing = next((s for s in merged.skills if s.name.lower() == q.term.lower()), None)
                if existing is None:
                    merged.skills.append(Skill(name=q.term, source="clarification", level=level))
                    known_skills.add(q.term.lower())
                elif level:
                    existing.level = level  # the level you gave for this skill, for every CV from now on
                if a.detail:
                    notes = merged.extras.setdefault("clarifications", [])
                    entry = f"{q.term}{f' ({level})' if level else ''}: {a.detail}"
                    notes[:] = [n for n in notes if not n.lower().startswith(q.term.lower() + ":")
                                and not n.lower().startswith(q.term.lower() + " (")]
                    notes.append(entry)
        elif q.kind == "add_role" and a.add_role:
            required = {"company", "title", "start", "end"}
            if not required.issubset(a.add_role.keys()):
                continue
            merged.experiences.append(
                Experience(
                    company=a.add_role["company"],
                    title=a.add_role["title"],
                    location="",
                    start=a.add_role["start"],
                    end=a.add_role["end"],
                    current=False,
                    bullets=[],
                    confirmed=False,
                )
            )
    return merged
