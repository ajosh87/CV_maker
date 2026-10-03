"""Checks on what you type, before anything is saved, fetched or sent to the LLM.

Each check returns problems in plain words (an empty list means fine), so a page can say exactly what to fix and keep
what you typed. The pipeline has its own layers after these: the privacy check before sending, schema checks on every
LLM reply, the honesty filter and fact check on what's written, and the apply assistant's guards.
"""
import re
from urllib.parse import urlparse

from cv_maker.llm.chat import _PROVIDERS

MAX_LINKS = 25  # job links per batch: more would just queue behind the polite pace anyway
JD_MIN, JD_SHORT, JD_MAX = 40, 150, 30_000  # characters of a pasted job description (the analysis reads 20,000)
CV_MIN = 100  # characters of text a CV needs before it's worth sending (a scanned PDF has almost none)
ANSWER_MAX = 600  # characters of detail in one answer
TERM_MIN, TERM_MAX, TERMS_MAX = 2, 120, 300  # the never-send list
FIELD_MAX = 200  # one application detail
UPLOAD_MAX_MB = 10

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_REGION = re.compile(r"[a-z]{2}(-gov)?-[a-z]+-\d")


def http_url(value: str) -> bool:
    parsed = urlparse((value or "").strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and " " not in value.strip()


def llm_settings(values: dict) -> list[str]:
    problems = []
    provider = (values.get("LLM_PROVIDER") or "").strip().lower()
    model = (values.get("LLM_MODEL") or "").strip()
    key = (values.get("LLM_API_KEY") or "").strip()
    if provider not in _PROVIDERS:
        problems.append("Choose a provider from the list.")
    if not model:
        problems.append("Enter the model's name, for example gpt-4o-mini, openai/gpt-4o-mini or llama3.")
    elif len(model) > 200 or any(c.isspace() for c in model):
        problems.append("The model name has spaces in it. Copy it exactly as your provider writes it.")
    if key and (any(c.isspace() for c in key) or len(key) > 500):
        problems.append("The API key has spaces or line breaks in it. Copy it again from your provider.")
    if provider == "azure" and not http_url(values.get("AZURE_OPENAI_ENDPOINT", "")):
        problems.append("Enter the Azure endpoint as a link, like https://your-resource.openai.azure.com.")
    if provider == "ollama" and not http_url(values.get("OLLAMA_HOST", "")):
        problems.append("Enter Ollama's address as a link, like http://localhost:11434.")
    if provider == "bedrock" and not _REGION.fullmatch((values.get("AWS_DEFAULT_REGION") or "").strip()):
        problems.append("Enter an AWS region such as us-east-1 or eu-west-2.")
    return problems


def pace(values: dict) -> list[str]:
    problems = []
    for key, label, low, high in (("LLM_RPM", "Requests a minute", 0, 10_000), ("LLM_CONCURRENCY", "At the same time", 1, 16),
                                  ("LLM_DAILY_CAP", "Requests a day", 0, 1_000_000)):
        value = str(values.get(key) or "").strip()
        if value and not (value.isdigit() and low <= int(value) <= high):
            problems.append(f"{label}: enter a whole number from {low} to {high:,}, or leave it empty for the suggestion.")
    return problems


def job_description(text: str) -> tuple[list[str], str]:
    """(problems, a note to show). Clearly not a description is a problem; short or very long is only a note."""
    text = (text or "").strip()
    if len(text) < JD_MIN:
        return [f"That's only {len(text)} characters: paste the job description itself."], ""
    if len(text) < JD_SHORT:
        return [], f"That description is short ({len(text)} characters): the CV can only be tailored to what it says."
    if len(text) > JD_MAX:
        return [], f"The description is long ({len(text):,} characters); only the first {JD_MAX:,} are kept."
    return [], ""


def links(urls: list[str]) -> tuple[list[str], str]:
    """(the links to add, a note when some were left out)."""
    if len(urls) <= MAX_LINKS:
        return urls, ""
    return urls[:MAX_LINKS], (f"Added the first {MAX_LINKS} links; {len(urls) - MAX_LINKS} more were left out so they "
                               "don't queue for a long time. Add them once these are done.")


def terms(text: str, existing: list[str] = ()) -> tuple[list[str], list[str]]:
    """Words never to send: (accepted terms, problems with the ones left out)."""
    accepted, problems, seen = list(existing), [], {t.casefold() for t in existing}
    for line in (text or "").splitlines():
        term = " ".join(line.split())
        if not term or term.casefold() in seen:
            continue
        if len(term) < TERM_MIN:
            problems.append(f"“{term}” is too short to hide: it would be found inside other words.")
        elif len(term) > TERM_MAX:
            problems.append(f"“{term[:40]}…” is longer than {TERM_MAX} characters; split it into the parts to hide.")
        elif len(accepted) >= TERMS_MAX:
            problems.append(f"The list holds {TERMS_MAX} entries at most; the rest were left out.")
            break
        else:
            seen.add(term.casefold())
            accepted.append(term)
    return accepted, problems


def application_details(standard: dict) -> list[str]:
    problems = []
    email, phone = (standard.get("email") or "").strip(), (standard.get("phone") or "").strip()
    if email and not _EMAIL.fullmatch(email):
        problems.append("Email: that doesn't look like an email address.")
    digits = re.sub(r"\D", "", phone)
    if phone and not (6 <= len(digits) <= 20 and re.fullmatch(r"[\d\s+().\-–/]+", phone)):
        problems.append("Phone: use digits, spaces and + ( ) - only, like +44 20 7946 0958.")
    for key, label in (("linkedin", "LinkedIn URL"), ("website", "Website")):
        value = (standard.get(key) or "").strip()
        if value and not http_url(value):
            problems.append(f"{label}: enter the full link, starting with https://.")
    for key, value in standard.items():
        if len(str(value or "")) > FIELD_MAX:
            problems.append(f"{key.replace('_', ' ').capitalize()}: keep it under {FIELD_MAX} characters.")
    return problems


def custom_answers(text: str) -> tuple[list[dict], list[str]]:
    """'question = answer' lines: (answers, problems with lines that couldn't be used)."""
    answers, problems = [], []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        question, sep, answer = line.partition("=")
        if sep and question.strip() and answer.strip():
            answers.append({"question": question.strip()[:FIELD_MAX], "answer": answer.strip()[:FIELD_MAX]})
        else:
            problems.append(f"“{line.strip()[:60]}” has no “question = answer” in it, so it wasn't saved.")
    return answers[-60:], problems


def profile(draft) -> list[str]:
    problems = []
    if not draft.name.strip():
        problems.append("Name: it goes at the top of every CV, so it can't be empty.")
    if draft.email and not _EMAIL.fullmatch(draft.email):
        problems.append("Email: that doesn't look like an email address.")
    for label, url in (draft.links or {}).items():
        if not (http_url(url) or re.fullmatch(r"(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?", url.strip(), re.I)):
            problems.append(f"Link “{url[:60]}”: enter a web address, like https://github.com/you.")
    return problems
