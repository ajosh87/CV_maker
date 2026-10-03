"""A quick, read-only look at an application before anything is filled in.

Where it happens, whether it needs an account, what it asks for, and what the posting says you should know.
Nothing is typed, uploaded or submitted, and no LLM is used: it only follows Apply / Continue links, declines
optional cookies, and stops at the first sign-in wall, CAPTCHA or form. It runs in a fresh, throwaway browser
profile, one look at a time, at a polite pace.
"""
import re
import tempfile
import threading
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from cv_maker.apply import page as pg
from cv_maker.apply import planner
from cv_maker.events import feed
from cv_maker.jobs.polite import level, sites
from cv_maker.jobs.urls import site_of

LOOK_LOCK = threading.Lock()  # one browser for looking at a time
MAX_PAGES = 5
_SIGN_IN_OPTIONS = [("Google", r"google"), ("LinkedIn", r"linked ?in"), ("Microsoft", r"microsoft|outlook"),
                    ("Apple", r"apple"), ("Indeed", r"indeed"), ("Facebook", r"facebook")]
_INFO_LINK = re.compile(r"review|glassdoor|indeed|application process|how to apply|hiring process|interview", re.I)


_REDIRECT_PARAMS = ("url", "redirectUrl", "redirect_url", "redirect", "dest", "destination", "target", "u")


def application_link(typed: str) -> str:
    """A pasted application link, made usable: surrounding quotes or brackets dropped, https:// added when it was left
    off ("careers.acme.com/jobs/1"), and a LinkedIn redirect link ("…linkedin.com/redir/redirect?url=https%3A…")
    unwrapped to the company's address inside it. "" when it isn't a web address at all."""
    text = (typed or "").strip().strip("<>\"'“”‘’ ")
    if not text or any(c.isspace() for c in text):
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        if not re.match(r"^(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]|$)", text, re.I):
            return ""
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    if site_of(text) == "linkedin.com":
        query = parse_qs(parsed.query)
        for key in _REDIRECT_PARAMS:
            inner = (query.get(key) or [""])[0]
            if urlparse(inner).scheme in ("http", "https") and urlparse(inner).netloc and site_of(inner) != "linkedin.com":
                return inner
    return text


def _employer_link(url: str) -> bool:
    return bool(url) and site_of(url) != "linkedin.com"


def linkedin_offsite(run) -> bool:
    """A LinkedIn posting whose Apply button leads to the employer's site, when that link isn't known yet."""
    return bool(run.job_url) and site_of(run.job_url) == "linkedin.com" and not run.easy_apply and not _employer_link(run.apply_url)


def start_url(run, assistant: bool = False) -> tuple[str, str]:
    """(where an application for this job starts, why there isn't one). With `assistant`, a LinkedIn posting is a start:
    you click its Apply button yourself (LinkedIn allows no assistant there) and the assistant carries on from the
    company's site. The read-only look never acts on LinkedIn, so for it there's no start."""
    if _employer_link(run.apply_url):
        return run.apply_url, ""
    if run.job_url and site_of(run.job_url) == "linkedin.com":
        if run.easy_apply:
            return "", ("This job uses LinkedIn Easy Apply, so you apply on LinkedIn itself (LinkedIn allows no assistant "
                        "there). Your CV and letter are ready on this page.")
        if assistant:
            return run.job_url, ""
        return "", ("LinkedIn shows the company's application link only after you sign in, so it can't be checked from here. "
                    "Start the assistant: you click Apply on LinkedIn (signing in once if it asks) and it does the rest. "
                    "Or paste the company's link to check it now.")
    return run.job_url or "", "" if run.job_url else "No job link to check (the description was pasted)."


def look(url: str, details: dict, *, throttle=sites, max_pages: int = MAX_PAGES) -> dict:
    """Follow the application from `url` without entering anything, and report what it needs."""
    from playwright.sync_api import sync_playwright

    found = {"checked_at": datetime.now(timezone.utc).isoformat(), "start_url": url, "sites": [], "pages": [],
             "account": None, "captcha": False, "documents": [], "questions": [], "consent": [], "diversity": False,
             "stopped_at": "", "links": [], "ended_on": ""}
    with LOOK_LOCK, tempfile.TemporaryDirectory() as profile, sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(profile, headless=True, viewport={"width": 1280, "height": 860},
                                                        accept_downloads=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            throttle.wait(url)
            feed.emit("step", f"looking at the application on {site_of(url)} (read-only: nothing is entered)")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            pg.settle(page)
            declined = set()
            for _ in range(max_pages):
                info = pg.observe(page)
                site = site_of(page.url)
                if site not in found["sites"]:
                    found["sites"].append(site)
                    if len(found["sites"]) > 1:
                        feed.emit("step", f"the application continues on {site}")
                banner = planner.cookie_banner(info)
                if banner and banner[0] == "reject" and site not in declined:
                    declined.add(site)
                    pg.perform(page, planner.Action("click", banner[1]), None)
                    pg.settle(page)
                    info = pg.observe(page)
                found["pages"].append({"site": site, "title": info.get("title") or "", "headings": info.get("headings", [])[:3]})
                _collect_links(found, info)
                block = planner.blocker(info)
                if block == "account":
                    found["account"] = _account(info, site)
                    found["stopped_at"] = "account"
                    break
                if block in ("human", "verify"):
                    found["captcha"] = block == "human"
                    found["stopped_at"] = "captcha" if block == "human" else "verify"
                    break
                fields = [f for f in info["fields"] if not f.get("filled")]
                if len([f for f in fields if f.get("kind") not in ("checkbox",)]) >= 2 or any(f.get("kind") == "file" for f in fields):
                    _collect_form(found, info, details)
                    found["stopped_at"] = "form"
                    break
                step = planner.navigation(info)
                if step is None or step[0] != "click":
                    found["stopped_at"] = "no next step"
                    break
                pages_before = len(context.pages)
                pg.perform(page, planner.Action("click", step[1]), None)
                page.wait_for_timeout(level()["page_pause_ms"])  # a visitor's pace between pages
                if len(context.pages) > pages_before:
                    page = context.pages[-1]
                pg.settle(page)
            found["ended_on"] = page.url
        finally:
            context.close()
    return found


def _account(info: dict, site: str) -> dict:
    texts = " ".join(b["text"] for b in info["buttons"])
    options = [name for name, pattern in _SIGN_IN_OPTIONS if re.search(rf"\b({pattern})\b", texts, re.I)]
    if any(f.get("kind") in ("email", "text") and re.search(r"e-?mail|user", f.get("label", ""), re.I) for f in info["fields"]):
        options.insert(0, "email")
    heading = " ".join(info.get("headings") or []) + " " + (info.get("title") or "")
    return {"site": site, "options": options,
            "creating": info["password_fields"] >= 2 or bool(re.search(r"create|sign ?up|register", heading, re.I))}


def _collect_form(found: dict, info: dict, details: dict) -> None:
    for f in info["fields"]:
        label = f.get("label") or "(no label)"
        if f.get("kind") == "file":
            what = ("CV" if planner._CV_FILE.search(label) else "Cover letter" if planner._LETTER_FILE.search(label)
                    else label)
            found["documents"].append({"what": what, "label": label, "required": bool(f.get("required"))})
        elif planner.is_eeo(f):
            found["diversity"] = True
        elif planner.is_consent(f):
            found["consent"].append(label)
        elif not planner.is_marketing(f) and f.get("kind") != "password":
            found["questions"].append({"label": label, "required": bool(f.get("required")),
                                       "saved": planner.has_saved_answer(f, details)})


def _collect_links(found: dict, info: dict) -> None:
    for b in info["buttons"]:
        safe = (b.get("href") or "").startswith(("https://", "http://"))  # never a javascript: link from a website
        if safe and _INFO_LINK.search(b["text"]) and b["href"] not in {x["href"] for x in found["links"]}:
            found["links"].append({"text": b["text"], "href": b["href"]})


def summary(found: dict) -> list[str]:
    """The scan in plain sentences, for the nerdbar."""
    lines = []
    if len(found.get("sites", [])) > 1:
        lines.append("moves to " + " → ".join(found["sites"]))
    if found.get("account"):
        options = found["account"]["options"]
        lines.append(f"needs an account on {found['account']['site']}" + (f" ({', '.join(options)})" if options else ""))
    if found.get("captcha"):
        lines.append("has a CAPTCHA")
    if found.get("documents"):
        lines.append("asks for " + ", ".join(d["what"] + (" (required)" if d["required"] else "") for d in found["documents"]))
    unsaved = [q["label"] for q in found.get("questions", []) if not q["saved"]]
    if unsaved:
        lines.append(f"{len(unsaved)} question{'s' if len(unsaved) != 1 else ''} without a saved answer")
    return lines
