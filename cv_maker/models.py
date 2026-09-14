from dataclasses import dataclass
from typing import Literal

RunStatus = Literal["needs_paste", "needs_answers", "ready", "failed"]
SkillSource = Literal["cv", "clarification"]


@dataclass
class Experience:
    company: str
    title: str
    location: str
    start: str
    end: str | None
    current: bool
    bullets: list[str]
    confirmed: bool


@dataclass
class Education:
    school: str
    credential: str
    field: str
    dates: str


@dataclass
class Skill:
    name: str
    source: SkillSource


@dataclass
class Profile:
    name: str
    email: str
    phone: str
    location: str
    links: dict
    target_role: str | None
    experiences: list[Experience]
    education: list[Education]
    skills: list[Skill]
    extras: dict


@dataclass
class JobRun:
    id: str
    created_at: str
    updated_at: str
    status: str
    job_url: str | None
    jd_text: str
    fetch_ok: bool
    fetch_reason: str
    requirements: dict
    gaps: list
    questions: list
    answers: list
    generate_letter: bool
    source_cv_path: str
    output_cv_path: str
    letter_path: str
    draft: dict
