from typing import TypedDict


class TailorState(TypedDict, total=False):
    run_id: str
    profile: dict
    jd_text: str
    job_meta: str
    requirements: dict
    gaps: list
    questions: list
    answers: list
    generate_letter: bool
    banned_terms: list
    version: int
    cv_document: dict
    fact_check: dict
    ats: dict
    job_title: str
    improve_focus: list
    assessment: dict
    tailoring: dict
    selection: list
    tailoring_summary: dict
    company: str
    letter_text: str
    letter_fact_check: dict
    output_cv_path: str
    letter_path: str
    warning: str
    error: str
