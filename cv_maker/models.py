from dataclasses import dataclass, field
from typing import Literal

RunStatus = Literal["fetching", "needs_paste", "analyzing", "needs_answers", "generating", "ready", "failed"]
SkillSource = Literal["cv", "clarification", "user"]  # user = added or edited by hand on the Profile page

# Statuses where background work is in flight; the UI polls while any run is in one.
ACTIVE_STATUSES = frozenset({"fetching", "analyzing", "generating"})


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
    level: str = ""  # beginner | intermediate | expert, when you said (questions for a job); "" when not asked


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
    title: str = ""
    company: str = ""
    step: str = ""
    error: str = ""
    failed_stage: str = ""
    warning: str = ""
    archived: bool = False
    last_version: int = 0  # highest version ever written, so deleted numbers are never reused
    apply_url: str = ""  # where the application starts, if the posting links to the employer's own page
    easy_apply: bool = False  # the job board itself takes the application (e.g. LinkedIn Easy Apply)
    offsite_apply: bool = False  # the posting's Apply button leads to the employer's own site (link shown only signed in)
    scan: dict = field(default_factory=dict)  # what the application needs, from a read-only look (apply.scout)
    apply_alongside: bool = False  # start filling in the application while the CV is being written
    letter_required: bool = False  # the application form requires a cover letter
    prep: dict = field(default_factory=dict)  # interview prep notes (prep.py)
    # Improving a version from its ATS check (improve.py): {"version", "items", "status": checking | ready | writing
    # | done, "plan", "focus", "error"}; once written, {"status": "done", "version": new, "base_version": old}.
    improve: dict = field(default_factory=dict)
    # A recruiter's reading of each requirement the keyword search didn't settle (jobs/assess.py), by term.
    assessment: dict = field(default_factory=dict)


@dataclass
class Document:
    """One generated file. Every generation of a job adds a new version; nothing is overwritten."""

    id: str
    created_at: str
    run_id: str
    kind: str  # "cv" | "letter"
    version: int
    path: str
    title: str = ""
    company: str = ""
    job_url: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Application:
    """One supervised application for a job (see cv_maker.apply). Everything here stays on this computer."""

    id: str
    run_id: str
    created_at: str
    updated_at: str
    version: int  # the CV / letter version being sent
    start_url: str
    status: str = "starting"  # starting | working | needs_you | submitted | stopped | failed
    waiting: dict = field(default_factory=dict)  # what the assistant needs from you right now
    filled: list = field(default_factory=list)  # every field it filled, for your review before submitting
    current_url: str = ""
    error: str = ""
    submitted_at: str = ""
    submitted_by: str = ""  # "assistant" (after your approval) | "you"
    show_window: bool = False
    allowed_sites: list = field(default_factory=list)


@dataclass
class CvUpload:
    id: str
    created_at: str
    updated_at: str
    filename: str
    path: str
    text: str
    replace_profile: bool = False
    status: str = "parsing"  # review (waiting for the user to check what is sent) | parsing | ready | failed
    error: str = ""
    summary: dict = field(default_factory=dict)
    # Choices made on the check-before-sending page (see privacy.cv_request).
    also_hide: list = field(default_factory=list)
    send_as_is: list = field(default_factory=list)
    keep_unused: bool = False
