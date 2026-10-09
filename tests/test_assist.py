"""The assistant around LinkedIn, sign-ins and your own driving; accounts you add; finding your way back."""
import json
import types

from cv_maker.app import create_app
from cv_maker.apply import accounts
from cv_maker.apply import details as app_details
from cv_maker.apply import scout
from cv_maker.jobs.fetch import FetchResult
from cv_maker.jobs.html import parse_job_page
from cv_maker.models import Experience, Profile, Skill
from cv_maker.store import Store

LINKEDIN_PAGE = """<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "AI Engineer",
"hiringOrganization": {"name": "Acme"}, "description": "<p>Build RAG systems. Apply at https://careers.acme.com/jobs/123 today.</p>"}
</script></head><body><button class="sign-up-modal__outlet top-card-layout__cta" data-modal="job-details-topcard-apply-modal"> Apply
<icon data-svg-class-name="apply-button__offsite-apply-icon-svg"></icon></button></body></html>"""


def test_a_linkedin_apply_button_that_leads_off_linkedin_is_recognised():
    page = parse_job_page(LINKEDIN_PAGE)
    assert page.offsite_apply and not page.easy_apply
    assert page.apply_url == "https://careers.acme.com/jobs/123"  # the employer's own link, found in the description
    easy = parse_job_page(LINKEDIN_PAGE.replace("apply-button__offsite-apply-icon-svg", "x").replace(" Apply\n", "Easy Apply"))
    assert not easy.offsite_apply


def test_where_an_application_starts_for_linkedin_jobs():
    run = types.SimpleNamespace(apply_url="https://www.linkedin.com/jobs/view/externalApply/1", easy_apply=False,
                                job_url="https://www.linkedin.com/jobs/view/123/")
    assert scout.start_url(run) == ("", scout.start_url(run)[1]) and "sign in" in scout.start_url(run)[1]  # never checked there
    assert scout.start_url(run, assistant=True) == (run.job_url, "")  # you click Apply; the assistant carries on
    assert scout.linkedin_offsite(run)
    run.apply_url = "https://careers.acme.com/apply/1"
    assert scout.start_url(run) == (run.apply_url, "") and not scout.linkedin_offsite(run)
    run.apply_url, run.easy_apply = "", True
    assert scout.start_url(run, assistant=True)[0] == "" and "Easy Apply" in scout.start_url(run, assistant=True)[1]


def _session(tmp_path, monkeypatch, url, start_url="https://careers.acme.com/apply"):
    from cv_maker.apply import session as apply_session

    store = Store(tmp_path / "db.sqlite")
    store.save_profile(Profile(name="Ada Lovelace", email="ada@example.com", phone="", location="", links={}, target_role=None,
                               experiences=[Experience("Contoso", "Engineer", "", "2021", None, True, ["Built APIs"], True)],
                               education=[], skills=[Skill("Python", "cv")], extras={}))
    run = store.create_run(job_url="https://www.linkedin.com/jobs/view/1/", status="ready")
    application = store.create_application(run, 1, start_url)
    calls = []
    page = types.SimpleNamespace(url=url, calls=calls, wait_for_timeout=lambda ms: None,
                                 wait_for_load_state=lambda *a, **k: None, is_closed=lambda: False,
                                 go_back=lambda **k: calls.append("back"), go_forward=lambda **k: calls.append("forward"),
                                 reload=lambda **k: calls.append("reload"), goto=lambda u, **k: calls.append(("goto", u)),
                                 keyboard=types.SimpleNamespace(press=lambda k: calls.append(("press", k))))

    class Model:
        def complete(self, messages, *, json_mode=False):
            return json.dumps({"next": {"do": "wait"}})

    session = apply_session.ApplySession(application=application, store=store, get_model=lambda: Model(), browser_dir=tmp_path / "b",
                                         files={"cv": "cv.docx", "letter": ""}, job_label="AI Engineer at Acme", job_context="AI Engineer")
    session.page = page
    session._details = app_details.load(store)
    monkeypatch.setattr(apply_session.pg, "settle", lambda p: None)
    monkeypatch.setattr(apply_session.pg, "screenshot", lambda p: b"")
    return apply_session, store, application, session, page


def test_on_linkedin_you_click_apply_and_the_assistant_carries_on_by_itself(tmp_path, monkeypatch):
    mod, store, application, session, page = _session(tmp_path, monkeypatch, "https://www.linkedin.com/jobs/view/1/")
    session._step()
    waiting = store.get_application(application.id).waiting
    assert waiting["kind"] == "site" and waiting["linkedin"] and "click Apply" in waiting["text"] and session.mode == "paused"
    page.url = "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/1/apply"  # you clicked Apply
    session._last_watch = 0
    session._watch()
    assert session.mode == "agent" and "myworkdayjobs.com" in store.get_application(application.id).allowed_sites


def test_application_systems_employers_use_are_allowed_without_asking(tmp_path, monkeypatch):
    mod, store, application, session, page = _session(tmp_path, monkeypatch, "https://acme.wd3.myworkdayjobs.com/apply")
    monkeypatch.setattr(mod.pg, "observe", lambda p: {"url": p.url, "title": "Apply", "headings": [], "text": "", "fields": [],
                                                      "buttons": [], "errors": [], "password_fields": 0})
    session._step()
    application = store.get_application(application.id)
    assert "myworkdayjobs.com" in application.allowed_sites and application.waiting["kind"] != "site"


def test_the_browser_bar_lets_you_take_over_and_navigate(tmp_path, monkeypatch):
    mod, store, application, session, page = _session(tmp_path, monkeypatch, "https://careers.acme.com/apply")
    session.mode = "agent"
    session._command({"action": "nav", "type": "back"})
    session._command({"action": "nav", "type": "goto", "url": "https://careers.acme.com/jobs/2"})
    session._command({"action": "nav", "type": "goto", "url": "javascript:alert(1)"})
    assert session.mode == "paused" and page.calls == ["back", ("goto", "https://careers.acme.com/jobs/2")]


def test_a_saved_account_signs_you_in_on_its_own_site_only(tmp_path, monkeypatch):
    mod, store, application, session, page = _session(tmp_path, monkeypatch, "https://acme.wd3.myworkdayjobs.com/login")
    vault = {("acme.wd3.myworkdayjobs.com", "ada@work.example"): "S3cret!"}
    monkeypatch.setattr(accounts, "get_password", lambda site, user: vault.get((site, user)))
    monkeypatch.setattr(accounts, "can_store", lambda: True)
    accounts.remember_account(store, "acme.wd3.myworkdayjobs.com", "ada@work.example", in_keychain=True)
    done = []
    monkeypatch.setattr(mod.pg, "perform", lambda p, action, field: done.append((action.op, action.id, action.value)))
    info = {"fields": [{"id": "c0", "label": "Email address", "kind": "email", "filled": False},
                       {"id": "c1", "label": "Password", "kind": "password", "filled": False}],
            "buttons": [{"id": "c2", "text": "Sign In"}], "password_fields": 1, "headings": ["Sign In"], "title": "Sign in"}
    session._account_needed(info)
    assert done == [("fill", "c0", "ada@work.example"), ("fill", "c1", "S3cret!"), ("click", "c2", "")]
    assert session.mode != "paused"
    other = dict(info, headings=["Create Account"], password_fields=2)  # never to create an account
    session._account_needed(other)
    assert session.mode == "paused" and store.get_application(application.id).waiting["kind"] == "account"


def test_you_can_add_accounts_and_find_your_way_back(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "can_store", lambda: True)
    saved = {}
    monkeypatch.setattr(accounts, "store_password", lambda site, user, pw: saved.update({(site, user): pw}) or True)
    fetch = lambda url: FetchResult(ok=True, text="Build RAG systems. " * 10, reason="", title="AI Engineer", company="Acme",  # noqa: E731
                                    offsite_apply=True)
    app = create_app(data_dir=tmp_path, fetcher=fetch, sync_jobs=True)
    client = app.test_client()
    run = app.store.create_run(job_url="https://www.linkedin.com/jobs/view/123/", status="ready")
    run.title, run.offsite_apply = "AI Engineer", True
    app.store.update_run(run)

    page = client.get(f"/settings/apply?next=/runs/{run.id}").data.decode()
    assert f'href="/runs/{run.id}" class="back">← AI Engineer</a>' in page  # back to the job you came from
    client.post("/settings/apply/accounts", data={"site": "https://acme.wd3.myworkdayjobs.com/en-US/careers", "username": "ada@work.example",
                                                  "password": "S3cret!"})
    client.post("/settings/apply/accounts", data={"site": "linkedin.com", "username": "ada", "password": "x"})
    sites = {a["site"]: a for a in accounts.list_accounts(app.store)}
    assert list(sites) == ["acme.wd3.myworkdayjobs.com"] and sites["acme.wd3.myworkdayjobs.com"]["in_keychain"]
    assert saved == {("acme.wd3.myworkdayjobs.com", "ada@work.example"): "S3cret!"}
    assert accounts.account_key("careers.acme.com/jobs/1") == "acme.com"
    client.post("/settings/apply/auto-sign-in", data={})
    assert app_details.load(app.store)["auto_sign_in"] is False

    res = client.post(f"/runs/{run.id}/scan", data={"apply_url": "https://www.linkedin.com/jobs/view/123/"}, follow_redirects=True)
    assert "That&#39;s LinkedIn&#39;s page, not the company&#39;s application" in res.data.decode()


def test_the_job_page_and_the_application_page_have_two_halves(tmp_path):
    app = create_app(data_dir=tmp_path, fetcher=lambda url: None)
    client = app.test_client()
    run = app.store.create_run(job_url="https://careers.acme.com/1", status="needs_answers")
    run.title, run.jd_text = "AI Engineer", "Build RAG systems. " * 10
    run.requirements = {"must_have": [{"term": "RAG", "original": "RAG"}]}
    app.store.update_run(run)
    page = client.get(f"/runs/{run.id}").data.decode()
    assert 'data-dock="yes"' in page and 'id="dock-body"' in page and "The posting" in page  # the posting, until a CV exists
    assert 'id="gutter-v"' in page and 'id="gutter-h"' in page and 'id="nb-panel"' in page and "layout.js" in page
    application = app.store.create_application(run, 1, "https://careers.acme.com/apply")
    page = client.get(f"/applications/{application.id}").data.decode()
    assert 'data-dock="yes"' in page and 'data-nav="back"' in page and 'id="ap-go"' in page and "Take over" in page
    assert 'class="nb-pill"' in page and 'class="ghost sm nb-toggle"' not in page  # the nerdbar is a pill again


def test_every_page_has_a_right_half_of_its_own(tmp_path):
    app = create_app(data_dir=tmp_path, fetcher=lambda url: None)
    client = app.test_client()
    run = app.store.create_run(job_url="https://careers.acme.com/1", status="needs_answers")
    run.title, run.jd_text = "AI Engineer", "We build RAG systems on Kubernetes (K8s). " * 5
    run.requirements = {"must_have": [{"term": "Kubernetes", "original": "Kubernetes"}]}
    run.questions = [{"term": "Kubernetes", "prompt": "How much experience do you have with Kubernetes?", "required": True,
                      "kind": "level", "why": "", "context": "", "optional": False}]
    app.store.update_run(run)
    right = {"/jobs": ['id="jobs-dock"', "How it works"],  # no profile yet: how it works, beside "Start with your CV"
             "/profile": ["Your CV files", "What happens to your CV"],
             "/settings": ['id="glance"', 'href="#llm"', "Browser engine", "Password store", 'data-section="automation"'],
             "/settings/apply": ['<section id="accounts">', "Add an account you already have"],
             "/documents": ['id="doc-dock"', "Click a document on the left"],
             "/settings/log": ['id="log-dock"', "No errors or warnings in the last day", "Download the log"],
             "/settings/sent": ['id="sent-dock"'],
             f"/runs/{run.id}/questions": ['id="q-dock"', '<mark class="q-mark" data-q="0">Kubernetes</mark>',
                                           '<mark class="q-mark" data-q="0">K8s</mark>', "What your profile shows for it now"],
             f"/runs/{run.id}/peek": ["← Add jobs", "Open the job →", 'class="state st-idle">After the CV']}
    for path, marks in right.items():
        page = client.get(path).data.decode()
        if not path.endswith("/peek"):
            assert 'data-dock="yes"' in page, path
        for mark in marks:
            assert mark in page, (path, mark)
    assert 'data-dock="no"' in client.get("/no-such-page").data.decode()  # a message: the nerdbar takes the half when open


def test_a_pasted_application_link_is_read_in_any_reasonable_form():
    read = scout.application_link
    assert read("careers.acme.com/jobs/1") == "https://careers.acme.com/jobs/1"  # https:// left off
    assert read(' "https://jobs.lever.co/acme/1" ') == "https://jobs.lever.co/acme/1"  # copied with quotes
    wrapped = "https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fjobs.lever.co%2Facme%2F123&urlhash=x"
    assert read(wrapped) == "https://jobs.lever.co/acme/123"  # LinkedIn's redirect, unwrapped to the company's link
    assert read("https://www.linkedin.com/jobs/view/123/") == "https://www.linkedin.com/jobs/view/123/"  # still LinkedIn's
    assert read("apply on their site") == "" and read("acme") == "" and read("ftp://acme.com/x") == ""


def test_the_answer_to_a_button_shows_in_its_own_part_of_the_job_page(tmp_path):
    app = create_app(data_dir=tmp_path, fetcher=lambda url: None, scanner=None)
    client = app.test_client()
    run = app.store.create_run(job_url="https://www.linkedin.com/jobs/view/123/", status="ready")
    run.title, run.jd_text = "AI Engineer", "Build RAG systems. " * 10
    run.requirements = {"must_have": [{"term": "RAG", "original": "RAG"}], "notes": []}
    app.store.update_run(run)

    page = client.post(f"/runs/{run.id}/scan", data={"apply_url": "not a link"}, follow_redirects=True).data.decode()
    card = page.split('id="before"')[1].split('id="prep"')[0]
    assert "isn&#39;t a web address" in card and 'class="sec-note error"' in card  # in the Applying card...
    assert "isn&#39;t a web address" not in page.split('id="before"')[0]  # ...not at the top of the page
    assert 'id="apply-url"' in card and "data-inline" in card  # the link field, sent without leaving the page
    page = client.post(f"/runs/{run.id}/scan", data={"apply_url": ""}, follow_redirects=True).data.decode()
    assert "Paste the link of the company&#39;s application page first" in page.split('id="before"')[1]
    client.post(f"/runs/{run.id}/scan", data={"apply_url": "careers.acme.com/apply/9"})
    assert app.store.get_run(run.id).apply_url == "https://careers.acme.com/apply/9"  # kept, even with no browser engine
    page = client.get(f"/runs/{run.id}").data.decode()
    assert '<section id="before" class="card" data-part>' in page and 'class="state st-' in page


def test_every_part_of_a_job_says_where_it_stands():
    from cv_maker.progress import section_states

    run = types.SimpleNamespace(status="ready", scan={}, easy_apply=False, prep={}, company="Acme", answers=[], jd_text="x" * 40)
    gaps = [{"strength": "strong", "required": True}, {"strength": "none", "required": True}, {"strength": "partial", "required": False}]
    states = section_states(run, versions=[{"version": 2, "ats": {"score": 83}}], gaps=gaps, applications=[], busy=["research"],
                            research=None, research_depth="simple")
    assert states["match"] == {"tone": "attn", "label": "1 of 3 strong"}
    assert states["versions"] == {"tone": "ok", "label": "v2 · ATS 83"}
    assert states["applying"]["label"] == "Ready to apply" and states["research"]["tone"] == "busy"
    assert states["prep"] == {"tone": "idle", "label": "Not written"}
    run.status = "generating"
    assert section_states(run, versions=[], gaps=[], applications=[], busy=["scan"], research=None,
                          research_depth="off")["applying"]["label"] == "Checking the application…"
    applied = types.SimpleNamespace(status="submitted")
    assert section_states(run, versions=[], gaps=[], applications=[applied], busy=[], research=None,
                          research_depth="off")["applying"] == {"tone": "ok", "label": "Applied"}


def test_each_page_says_a_thing_once(tmp_path):
    app = create_app(data_dir=tmp_path, fetcher=lambda url: None)
    client = app.test_client()
    settings = client.get("/settings").data.decode()
    left, right = settings.split('id="pane-side"')
    assert 'id="glance"' in left and 'class="settings-section"' not in left  # where each setting stands, on the left...
    assert '<section id="llm" class="settings-section"' in right and 'id="glance"' not in right  # ...changing it, on the right
    log = client.get("/settings/log").data.decode()
    assert log.count("Download the log") == 1 and 'class="mt-xs inline-only"' not in log.split('id="log-dock"')[1]
