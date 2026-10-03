"""The apply assistant: guards that never depend on the LLM, and a full supervised run on a mock job site."""
import json
import re
import time
import types

import pytest

from cv_maker.apply import details as app_details
from cv_maker.apply import planner
from cv_maker.models import Experience, Profile, Skill
from cv_maker.privacy import PrivateModel, identity_secrets
from cv_maker.store import Store

STANDARD = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone": "+44 20 7946 0958",
            "city": "London", "country": "United Kingdom", "how_heard": "LinkedIn", "linkedin": "https://linkedin.com/in/ada-l",
            "work_authorization": "Yes", "sponsorship": "No", "salary": "£70,000"}
DETAILS = {"standard": {key: STANDARD.get(key, "") for key, _, _ in app_details.STANDARD}, "custom": []}


def _field(id, label, kind="text", **kw):
    return {"id": id, "label": label, "kind": kind, "required": kw.pop("required", False), "filled": False, **kw}


# ---- rules and guards (no browser) ----

def test_standard_fields_are_filled_by_rule_without_the_llm():
    page = {"fields": [
        _field("c0", "First Name *"), _field("c1", "Last name"), _field("c2", "Email address", "email"),
        _field("c3", "Confirm email"), _field("c4", "Mobile number", "tel"), _field("c5", "Country *", "select",
                                                                                     options=["Select One", "India", "United Kingdom"]),
        _field("c6", "Are you legally authorised to work in the UK?", "radio", options=["Yes", "No"], ids=["c6", "c7"]),
        _field("c8", "Name of your current employer"), _field("c9", "Resume/CV", "file"), _field("c10", "Cover letter", "file"),
    ], "buttons": []}
    actions, ask = planner.by_rule(page, DETAILS, {"cv": "cv.docx", "letter": "letter.docx"})
    got = {a.id: (a.op, a.value) for a in actions}
    assert got == {"c0": ("fill", "Ada"), "c1": ("fill", "Lovelace"), "c2": ("fill", "ada@example.com"),
                   "c3": ("fill", "ada@example.com"), "c4": ("fill", "+44 20 7946 0958"), "c5": ("choose", "United Kingdom"),
                   "c6": ("choose", "Yes"), "c9": ("upload", "cv.docx"), "c10": ("upload", "letter.docx")}
    assert ask == []  # "Name of your current employer" is left for the LLM, not mistaken for your name
    assert all("Ada" not in a.shown and "@" not in a.shown for a in actions)  # the log never shows personal details


def test_diversity_questions_are_declined_or_asked_and_never_reach_the_llm():
    page = {"fields": [
        _field("c0", "Gender", "select", options=["Female", "Male", "Decline to self-identify"]),
        _field("c1", "Are you a protected veteran?", "select", options=["Yes", "No"], required=True),
        _field("c2", "Ethnicity", "radio", options=["Asian", "White", "Prefer not to say"], ids=["c2", "c3", "c4"]),
    ], "buttons": []}
    actions, ask = planner.by_rule(page, DETAILS, {"cv": "cv.docx"})
    assert [(a.id, a.value) for a in actions] == [("c0", "Decline to self-identify"), ("c2", "Prefer not to say")]
    assert [q["id"] for q in ask] == ["c1"]
    assert planner.for_llm(page, set()) == []


def test_consent_is_yours_and_marketing_is_left_alone():
    page = {"fields": [
        _field("c0", "I agree to the Terms and Privacy Policy", "checkbox", required=True),
        _field("c1", "Send me job alerts and marketing emails", "checkbox"),
        _field("c2", "Password", "password"),
    ], "buttons": []}
    actions, ask = planner.by_rule(page, DETAILS, {"cv": "cv.docx"})
    assert actions == [] and [(q["id"], q["kind"]) for q in ask] == [("c0", "consent")]
    assert planner.for_llm(page, set()) == []


def test_the_llm_can_never_click_submit_or_create_an_account():
    page = {"fields": [_field("c0", "Years of Python")], "buttons": [
        {"id": "c1", "text": "Submit Application"}, {"id": "c2", "text": "Create Account"}, {"id": "c3", "text": "Next"}]}
    allowed = planner.for_llm(page, set())
    assert planner.parse_plan({"next": {"do": "click", "id": "c1"}}, page, allowed).next == "ready_to_submit"
    assert planner.parse_plan({"next": {"do": "click", "id": "c2"}}, page, allowed).next == "account"
    plan = planner.parse_plan({"fill": [{"id": "c0", "value": "6"}, {"id": "c9", "value": "x"}], "next": {"do": "click", "id": "c3"}},
                              page, allowed)
    assert [(a.id, a.value) for a in plan.actions] == [("c0", "6")] and (plan.next, plan.next_id) == ("click", "c3")


def test_option_matching_and_sites():
    assert planner.pick_option("Yes", ["Yes, I am authorised", "No"]) == "Yes, I am authorised"
    assert planner.pick_option("united kingdom", ["United States", "United Kingdom"]) == "United Kingdom"
    assert planner.pick_option("Mars", ["Earth"]) is None
    assert planner.site_of("https://acme.wd5.myworkdayjobs.com/en-US/careers") == "myworkdayjobs.com"
    assert planner.site_of("https://jobs.acme.co.uk/apply") == "acme.co.uk"
    assert planner.blocker({"fields": [], "text": "Please verify you are human", "captcha": False, "password_fields": 0}) == "human"
    assert planner.blocker({"fields": [_field("c0", "Password", "password")], "text": "Sign in", "captcha": False,
                            "password_fields": 1}) == "account"


def test_lessons_from_a_real_careers_site():
    # A "see how well you match" upload is not the application: your CV never goes there by rule.
    page = {"fields": [_field("c0", "Find out how well you match with this job", "file")], "buttons": []}
    assert planner.by_rule(page, DETAILS, {"cv": "cv.docx"}) == ([], [])
    # An unlabelled but required upload is asked about, not guessed.
    page = {"fields": [_field("c0", "Attachment", "file", required=True)], "buttons": []}
    assert [q["kind"] for q in planner.by_rule(page, DETAILS, {"cv": "cv.docx"})[1]] == ["upload"]
    # Email-first sign-in ("Sign in with AstraZeneca": email, then Continue) is an account step.
    page = {"fields": [_field("c0", "Email", "email")], "buttons": [{"id": "c1", "text": "Continue"}],
            "headings": ["Sign in with AstraZeneca"], "title": "", "text": "Sign in with AstraZeneca Email Continue",
            "captcha": False, "password_fields": 0}
    assert planner.blocker(page) == "account"
    # Cookie banners: only ever the privacy-preserving choice, and never accepting for you.
    banner = {"text": "We use cookies", "buttons": [{"id": "c0", "text": "Accept all"}, {"id": "c1", "text": "Reject all"}]}
    assert planner.cookie_banner(banner) == ("reject", "c1")
    assert planner.cookie_banner({"text": "We use cookies", "buttons": [{"id": "c0", "text": "Accept"}]}) == ("accept-only", "")


def test_interrupted_applications_can_be_resumed_after_a_restart(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    run = store.create_run(job_url="https://x")
    application = store.create_application(run, 1, "https://x/apply")
    application.status = "working"
    store.update_application(application)
    store.reset_interrupted()
    assert store.get_application(application.id).status == "stopped"


def test_a_field_that_couldnt_be_filled_is_asked_about_not_skipped(tmp_path, monkeypatch):
    from cv_maker.apply import session as apply_session

    store = Store(tmp_path / "db.sqlite")
    store.save_profile(Profile(name="Ada Lovelace", email="ada@example.com", phone="", location="London", links={},
                               target_role=None, experiences=[Experience("Northwind", "Engineer", "London", "2019", None, True,
                                                                         ["Built Python APIs used by 3 teams"], True)],
                               education=[], skills=[Skill("Python", "cv")], extras={}))
    app_details.save(store, STANDARD)
    run = store.create_run(job_url="https://careers.acme.com/job/1", status="ready")
    application = store.create_application(run, 1, "https://careers.acme.com/apply")
    info = {"url": "https://careers.acme.com/apply", "title": "Experience", "headings": ["Experience"], "text": "",
            "fields": [_field("c0", "Years of experience with Python *", required=True)],
            "buttons": [{"id": "c1", "text": "Continue", "box": [0, 0, 10, 10]}], "errors": [], "password_fields": 0}

    class Model:
        def complete(self, messages, *, json_mode=False):
            return json.dumps({"fill": [{"id": "c0", "value": "6"}], "next": {"do": "click", "id": "c1"}})

    clicked = []

    def perform(page, action, field):
        if action.op == "fill":
            raise TimeoutError("the custom widget didn't respond")
        clicked.append(action.id)

    monkeypatch.setattr(apply_session.pg, "observe", lambda page: info)
    monkeypatch.setattr(apply_session.pg, "perform", perform)
    monkeypatch.setattr(apply_session.pg, "settle", lambda page: None)
    session = apply_session.ApplySession(application=application, store=store, get_model=lambda: Model(),
                                         browser_dir=tmp_path / "browser", files={"cv": "cv.docx", "letter": ""},
                                         job_label="Platform Engineer at Acme", job_context="Platform Engineer Acme Python")
    session.page = types.SimpleNamespace(url=info["url"], wait_for_timeout=lambda ms: None)
    session._details = app_details.load(store)
    session._step()
    waiting = store.get_application(application.id).waiting
    assert clicked == [] and waiting["kind"] == "question"  # it stopped instead of pressing Continue
    assert [q["id"] for q in waiting["questions"]] == ["c0"] and "couldn't fill" in waiting["questions"][0]["question"]


# ---- a full run on a mock job site ----

def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


needs_browser = pytest.mark.skipif(not _browser_available(), reason="Playwright with Chromium is not installed")


class ScriptedModel:
    """Answers like a good model would on the mock site's experience page, and records every prompt."""

    def __init__(self) -> None:
        self.prompts = []

    def complete(self, messages, *, json_mode=False):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        field = re.search(r"^(\S+) · \w+ · Years of experience with Python", prompt, re.M)
        button = re.search(r"^(\S+) · Save and Continue$", prompt, re.M)
        if field and button:
            return json.dumps({"fill": [{"id": field.group(1), "value": "6"}], "next": {"do": "click", "id": button.group(1)},
                               "note": "Filled years of Python from the profile."})
        return json.dumps({"next": {"do": "wait"}})


def _wait(store, application_id, predicate, what, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        application = store.get_application(application_id)
        if application is not None and predicate(application):
            return application
        if application is not None and application.status == "failed":
            raise AssertionError(f"failed while waiting for {what}: {application.error}")
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: {store.get_application(application_id)}")


def _click(session, text):
    """What you'd do in the live view: click an element on the page."""
    observation = session.last_observation
    box = next((b["box"] for b in observation["buttons"] if b["text"] == text), None) or \
        next(f["box"] for f in observation["fields"] if text in f["label"])
    session.send({"action": "input", "type": "click", "x": box[0] + box[2] / 2, "y": box[1] + box[3] / 2})


def _setup(tmp_path, start_path):
    from fixtures.mock_ats import Running

    from cv_maker.apply.session import ApplySession

    site = Running()
    store = Store(tmp_path / "db.sqlite")
    store.save_profile(Profile(name="Ada Lovelace", email="ada@example.com", phone="+44 20 7946 0958", location="London",
                               links={"linkedin": "https://linkedin.com/in/ada-l"}, target_role=None,
                               experiences=[Experience("Northwind", "Engineer", "London", "2019", None, True,
                                                       ["Built Python APIs used by 3 teams"], True)],
                               education=[], skills=[Skill("Python", "cv")], extras={}))
    app_details.save(store, STANDARD)
    run = store.create_run(job_url=f"{site.url}{start_path}", status="ready")
    cv, letter = tmp_path / "cv_test_v1.docx", tmp_path / "letter_test_v1.docx"
    cv.write_bytes(b"cv")
    letter.write_bytes(b"letter")
    application = store.create_application(run, 1, f"{site.url}{start_path}")
    model = ScriptedModel()
    session = ApplySession(application=application, store=store,
                           get_model=lambda: PrivateModel(model, lambda: identity_secrets(store.get_profile(), [])),
                           browser_dir=tmp_path / "browser", files={"cv": str(cv), "letter": str(letter)},
                           job_label="Platform Engineer at Acme", job_context="Platform Engineer Acme Python")
    return site, store, application, session, model


@needs_browser
def test_a_supervised_application_from_job_posting_to_submit(tmp_path):
    site, store, application, session, model = _setup(tmp_path, "/job/1")
    try:
        session.start()
        wall = _wait(store, application.id, lambda a: a.waiting.get("kind") == "account", "the sign-in page")
        assert wall.waiting["creating"] is False  # a sign-in form: nothing was typed into it
        _click(session, "Create Account")  # you open the sign-up form in the view
        session.send({"action": "resume"})
        _wait(store, application.id, lambda a: a.waiting.get("kind") == "account" and a.waiting.get("creating"), "the sign-up form")
        session.send({"action": "account_fill"})
        _wait(store, application.id, lambda a: a.waiting.get("kind") == "account_check", "the filled sign-up form")
        assert len(session.new_password) == 18  # no system password store here: shown to you once
        _click(session, "I agree to the Terms")  # the terms are yours to accept
        _click(session, "Create Account")
        session.send({"action": "resume"})

        asked = _wait(store, application.id, lambda a: a.waiting.get("kind") == "question", "the certification question")
        assert [q["kind"] for q in asked.waiting["questions"]] == ["consent"]
        session.send({"action": "answer", "answers": [{"id": asked.waiting["questions"][0]["id"], "value": "yes"}]})

        ready = _wait(store, application.id, lambda a: a.waiting.get("kind") == "submit", "the review page")
        assert site.app.received.get("submitted") is None  # nothing is submitted without you
        assert ready.waiting["button"] == "Submit Application"
        session.send({"action": "approve_submit"})
        done = _wait(store, application.id, lambda a: a.status == "submitted", "the confirmation")
        session.join(timeout=30)
    finally:
        session.send({"action": "stop"})
        session.join(timeout=30)
        site.close()

    got = site.app.received
    assert got["account"] == {"email": "ada@example.com", "password_length": 18}
    assert {k: got["info"][k] for k in ("first", "last", "email", "country", "heard")} == {
        "first": "Ada", "last": "Lovelace", "email": "ada@example.com", "country": "United Kingdom", "heard": "LinkedIn"}
    assert "alerts" not in got["info"]  # marketing left unticked
    assert (got["experience"]["resume"], got["experience"]["letter"], got["experience"]["years"]) == (
        "cv_test_v1.docx", "letter_test_v1.docx", "6")
    assert (got["questions"]["auth"], got["questions"]["sponsor"], got["questions"]["salary"]) == ("yes", "no", "£70,000")
    assert (got["voluntary"]["gender"], got["voluntary"]["veteran"]) == ("Decline to self-identify", "I don't wish to answer")
    assert got["submitted"] is True and done.submitted_by == "assistant"
    assert any(f["label"].startswith("Years of experience") and f["source"] == "llm" for f in done.filled)
    # The LLM was asked once (the one field no rule could fill) and never saw a personal detail.
    assert len(model.prompts) == 1 and "Years of experience with Python" in model.prompts[0]
    assert not [p for p in ("Ada", "Lovelace", "ada@example.com", "7946", "linkedin.com/in/ada-l") if p in model.prompts[0]]


@needs_browser
def test_a_captcha_is_handed_to_you(tmp_path):
    site, store, application, session, model = _setup(tmp_path, "/apply/2/start")
    try:
        session.start()
        check = _wait(store, application.id, lambda a: a.waiting.get("kind") == "human", "the CAPTCHA")
        assert "human" in check.waiting["text"]
        _click(session, "I'm not a robot")
        _click(session, "Continue")
        session.send({"action": "resume"})
        _wait(store, application.id, lambda a: a.waiting.get("kind") == "account", "the sign-in page after the check")
        session.send({"action": "stop"})
        session.join(timeout=30)
        assert store.get_application(application.id).status == "stopped"
        assert model.prompts == []
    finally:
        session.send({"action": "stop"})
        session.join(timeout=30)
        site.close()
