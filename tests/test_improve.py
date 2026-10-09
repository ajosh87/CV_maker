"""Improving a version from its ATS check: pick items, the LLM checks them, you answer, a new version is checked again."""
import json

from werkzeug.datastructures import MultiDict

from cv_maker import ats, improve
from cv_maker.app import create_app
from cv_maker.models import Experience, Profile, Skill
from tests.test_app import FakeModel, _ready_job, fake_fetch


def _profile(**kw):
    base = dict(name="Ada Lovelace", email="ada@example.com", phone="", location="London", links={}, target_role=None,
                experiences=[Experience("Northwind", "Platform Engineer", "London", "2021", None, True,
                                        ["Built Python APIs for the payments team", "Cut deploy time by 40%"], True),
                             Experience("Contoso", "Engineer", "", "", None, False, ["Ran Docker services on AWS ECS"], True)],
                education=[], skills=[Skill("Python", "cv"), Skill("Docker", "cv"), Skill("Go", "cv")], extras={})
    base.update(kw)
    return Profile(**base)


def _check(profile):
    cv = {"name": profile.name, "headline": "Platform Engineer", "contact": [profile.email], "summary": "Engineer who builds APIs.",
          "skills": ["Python", "Docker", "Go"], "education": [],
          "experiences": [{"company": e.company, "title": e.title, "start": e.start, "bullets": e.bullets} for e in profile.experiences]}
    plan = [{"term": "Python", "required": True, "have": True, "level": "", "score": 80, "support": []},
            {"term": "Kubernetes", "required": True, "have": False, "level": "", "score": 0, "support": []},
            {"term": "Go", "required": True, "have": True, "level": "", "score": 55, "support": []},
            {"term": "Terraform", "required": False, "have": False, "level": "", "score": 0, "support": []}]
    return ats.check(cv, plan, "Platform Engineer")


def test_the_check_turns_into_items_you_can_pick():
    items = {i["id"]: i for i in improve.improvable(_check(_profile()))}
    assert items["kw:Kubernetes"]["group"] == "must" and items["kw:Kubernetes"]["default"]  # a must-have you may have
    assert items["kw:Kubernetes"]["gain"] == 15.0  # a third of the 45 must-have points: one of three must-haves
    assert not items["kw:Terraform"]["default"]  # nice-to-haves aren't picked for you
    assert "ctx:Go" in items and "ctx:Python" not in items  # Go is only in the skills list; Python is in a bullet
    assert {"contact", "dates", "sections"} <= set(items) and "title" not in items  # the title is already right
    assert improve.improvable({}) == []


def test_the_llms_check_is_kept_only_where_it_holds_up():
    profile, check = _profile(), _check(_profile())
    picked = [i for i in improve.improvable(check) if i["id"] in ("kw:Kubernetes", "numbers", "dates", "contact")]
    picked.append({"id": "numbers", "group": "numbers", "label": "Measurable results", "detail": "1 of 3", "gain": 2, "default": True})
    reply = {"items": [
        {"id": "kw:Kubernetes", "verdict": "fixable", "why": "Docker on ECS is close.",  # not fixable: the profile doesn't show it
         "questions": [{"target": "Kubernetes", "role": "R2", "ask": "Have you run anything on Kubernetes, e.g. at Contoso?"}]},
        {"id": "numbers", "verdict": "ask", "why": "Two results have no number.",
         "questions": [{"target": "R1.B2", "ask": "?"}, {"target": "R9.B1", "ask": "Nope"},  # has a number already / no such bullet
                       {"target": "R1.B1", "ask": "How many teams or calls a day used those APIs?"}]},
        {"id": "dates", "verdict": "ask", "why": "Contoso has no dates.", "questions": [{"target": "R2", "ask": "When were you at Contoso?"}]},
        {"id": "made-up", "verdict": "ask", "questions": [{"target": "x", "ask": "Tell me your salary"}]},
    ]}

    class Model:
        def complete(self, messages, *, json_mode=False):
            assert messages[-1]["content"].startswith("You are checking which improvements")
            assert "R1.B1 Built Python APIs" in messages[-1]["content"]  # bullets are numbered for it to point at
            return json.dumps(reply)

    the_plan = improve.plan(Model(), profile, picked, check, title="Platform Engineer")
    by = {i["id"]: i for i in the_plan["items"]}
    assert set(by) == {"kw:Kubernetes", "numbers", "dates", "contact"}  # nothing it wasn't asked about
    assert by["kw:Kubernetes"]["verdict"] == "ask"  # the facts decide: a keyword you don't have can't be "fixable"
    kq = by["kw:Kubernetes"]["questions"][0]
    assert kq["kind"] == "keyword" and kq["role"] == 1 and kq["ask"].startswith("Have you run anything on Kubernetes")
    assert [(q["role"], q["bullet"]) for q in by["numbers"]["questions"]] == [(0, 0)]  # only the bullet without a number
    assert by["dates"]["questions"] == [{"kind": "dates", "role": 1, "ask": "When were you at Contoso?"}]
    assert by["contact"]["questions"][0]["kind"] == "contact"  # not in the reply: the standard question


def test_your_answers_become_facts_in_your_profile():
    profile, check = _profile(), _check(_profile())
    picked = [i for i in improve.improvable(check) if i["id"] in ("kw:Kubernetes", "dates", "contact", "sections")]
    picked.append({"id": "numbers", "group": "numbers", "label": "Measurable results", "detail": "", "gain": 2, "default": True})
    the_plan = improve.standard_plan(profile, picked, check)
    numbered = improve.numbered_questions(the_plan)
    fields = {q["kind"]: name for name, q, _ in reversed(numbered)}  # the first question of each kind
    assert [(q["role"], q["bullet"]) for _, q, _ in numbered if q["kind"] == "bullet"] == [(0, 0), (1, 0)]  # no numbers yet
    form = MultiDict({
        fields["bullet"]: "Built Python APIs for the payments team, used by 12 services",
        f"{fields['keyword']}_level": "intermediate", f"{fields['keyword']}_role": "1",
        f"{fields['keyword']}_text": "Moved our ECS services to Kubernetes (EKS)",
        f"{fields['dates']}_start": "Jun 2017", f"{fields['dates']}_end": "Feb 2021",
        f"{fields['contact']}_email": "not-an-email", f"{fields['contact']}_phone": "+44 20 7946 0958",
        f"{fields['education']}_credential": "MSc", f"{fields['education']}_field": "Computer Science",
        f"{fields['education']}_school": "University of Manchester",
    })
    updated, levels, focus, problems = improve.apply(profile, the_plan, form)
    assert updated.experiences[0].bullets[0].endswith("used by 12 services")  # your words, your number
    assert updated.experiences[1].bullets[-1] == "Moved our ECS services to Kubernetes (EKS)"
    assert any(s.name == "Kubernetes" and s.level == "intermediate" and s.source == "user" for s in updated.skills)
    assert levels == [{"term": "Kubernetes", "level": "intermediate"}]
    assert (updated.experiences[1].start, updated.experiences[1].end) == ("Jun 2017", "Feb 2021")
    assert updated.phone == "+44 20 7946 0958" and updated.email == "ada@example.com"  # a bad email isn't saved...
    assert any("doesn't look like an email" in p for p in problems)  # ...and you're told
    assert updated.education[0].credential == "MSc"
    assert any("12 services" in line for line in focus) and any("Kubernetes" in line for line in focus)
    assert profile.experiences[0].bullets[0] == "Built Python APIs for the payments team"  # the original is untouched


def test_nothing_you_leave_empty_or_decline_changes_anything():
    profile, check = _profile(), _check(_profile())
    picked = [i for i in improve.improvable(check) if i["id"] == "kw:Kubernetes"]
    the_plan = improve.standard_plan(profile, picked, check)
    name = improve.numbered_questions(the_plan)[0][0]
    updated, levels, focus, _ = improve.apply(profile, the_plan, MultiDict({f"{name}_level": "none", f"{name}_role": "1",
                                                                            f"{name}_text": "Kubernetes everywhere"}))
    assert updated == profile and focus == []  # "no experience": no line, no skill
    assert levels == [{"term": "Kubernetes", "level": "none"}]  # recorded, so it's never claimed


def test_the_comparison_shows_what_moved():
    before = {"score": 49, "checks": [{"key": "must", "label": "Must-have keywords", "points": 15.0, "max": 45},
                                      {"key": "dates", "label": "Dates on every role", "points": 5.0, "max": 5}]}
    after = {"score": 72, "checks": [{"key": "must", "label": "Must-have keywords", "points": 30.0, "max": 45},
                                     {"key": "dates", "label": "Dates on every role", "points": 5.0, "max": 5}]}
    c = improve.compare(before, after)
    assert (c["before"], c["after"], c["delta"]) == (49, 72, 23)
    assert [r["delta"] for r in c["checks"]] == [15.0, 0.0]
    assert improve.compare({}, after) == {}


def test_the_score_is_counted_not_guessed():
    profile = _profile()
    first, second = _check(profile), _check(profile)
    assert first == second  # the same CV and posting always get the same score
    better = _profile(experiences=[Experience("Northwind", "Platform Engineer", "London", "2021", None, True,
                                              ["Ran Kubernetes for 40 services", "Cut deploy time by 40%"], True),
                                   Experience("Contoso", "Engineer", "", "2017", "2021", False, ["Ran Docker services on AWS ECS"], True)])
    assert _check(better)["score"] > first["score"]  # a must-have in a bullet, and dates on every role, count


class ImprovingModel(FakeModel):
    def complete(self, messages, *, json_mode=False):
        prompt = messages[-1]["content"]
        if prompt.startswith("You are checking which improvements"):
            return json.dumps({"items": [{"id": "kw:Kubernetes", "verdict": "unlikely",
                                          "why": "Nothing in your profile mentions Kubernetes.",
                                          "questions": [{"target": "Kubernetes", "ask": "Have you used Kubernetes, and where?"}]}]})
        return super().complete(messages, json_mode=json_mode)


def test_improving_a_version_end_to_end(tmp_path):
    app = create_app(data_dir=tmp_path, model_factory=ImprovingModel, fetcher=fake_fetch, sync_jobs=True)
    client = app.test_client()
    run = _ready_job(app, client)  # v1: Kubernetes skipped, so it's a gap
    detail = client.get(f"/runs/{run.id}").data.decode()
    assert 'name="item" value="kw:Kubernetes"' in detail and "Check my picks and ask me" in detail
    v1 = next(d for d in app.store.list_documents(run.id) if d.kind == "cv")
    assert v1.meta["ats"]["file"]["ok"]  # the DOCX was read back and holds what the check counted

    res = client.post(f"/runs/{run.id}/versions/1/improve", data={"item": ["kw:Kubernetes"]})
    assert res.headers["Location"].endswith(f"/runs/{run.id}/improve")
    page = client.get(f"/runs/{run.id}/improve").data.decode()
    assert "Have you used Kubernetes, and where?" in page and "No sign of it in your profile" in page  # the LLM's check
    assert "Add to my profile and write v2" in page

    res = client.post(f"/runs/{run.id}/improve", data={"q0_level": "intermediate", "q0_role": "0",
                                                       "q0_text": "Ran our Python APIs on Kubernetes"})
    assert res.headers["Location"].endswith(f"/runs/{run.id}")
    profile = app.store.get_profile()
    assert "Ran our Python APIs on Kubernetes" in profile.experiences[0].bullets
    run = app.store.get_run(run.id)
    assert run.improve == {"status": "done", "version": 2, "base_version": 1}
    assert {"term": "Kubernetes", "skipped": False, "text": "intermediate", "add_role": None, "level": "intermediate"} in run.answers
    v2 = next(d for d in app.store.list_documents(run.id) if d.kind == "cv" and d.version == 2)
    assert v2.meta["improved_from"] == 1 and v2.meta["improve"]["items"][0]["id"] == "kw:Kubernetes"
    assert v2.meta["ats"]["score"] > v1.meta["ats"]["score"]  # checked again, and better
    detail = client.get(f"/runs/{run.id}").data.decode()
    assert f"Checked again: v1 {v1.meta['ats']['score']} → v2 {v2.meta['ats']['score']}" in detail
    assert "Kubernetes <span class=\"faint\">· now in the CV</span>" in detail  # what each pick came to


def test_an_improvement_can_be_cancelled_without_changing_anything(tmp_path):
    app = create_app(data_dir=tmp_path, model_factory=ImprovingModel, fetcher=fake_fetch, sync_jobs=True)
    client = app.test_client()
    run = _ready_job(app, client)
    before = app.store.get_profile()
    client.post(f"/runs/{run.id}/versions/1/improve", data={"item": ["kw:Kubernetes"]})
    assert client.post(f"/runs/{run.id}/improve/cancel").headers["Location"].endswith(f"/runs/{run.id}")
    assert app.store.get_run(run.id).improve == {} and app.store.get_profile() == before
    assert client.post(f"/runs/{run.id}/versions/1/improve", data={}).headers["Location"].endswith(f"/runs/{run.id}")  # nothing picked
