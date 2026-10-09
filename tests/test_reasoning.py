"""The multi-step reasoning: a reading of the match before asking, a plan before writing, and research that knows
which company it means. Each step's reply is checked before it's used, and the counted parts stay counted."""
import json

import httpx
from docx import Document

from cv_maker import research, tailor
from cv_maker.jobs.assess import assess
from cv_maker.jobs.match import match_profile
from cv_maker.jobs.polite import SiteThrottle
from cv_maker.jobs.requirements import JobRequirements
from cv_maker.models import Experience, Profile, Skill
from cv_maker.pipeline.graph import Pipeline
from cv_maker.store import Store

REQS = {"job_title": "ML Platform Engineer", "company": "Prodigal", "domain": "fintech collections software",
        "responsibilities": ["Deploy and run model services", "Build data pipelines"],
        "must_have": [{"term": "Python", "original": "Python"}, {"term": "Kubernetes", "original": "Kubernetes for model serving"}],
        "nice_to_have": [{"term": "Go", "original": "Go"}]}


def _profile() -> Profile:
    return Profile(
        name="Ada", email="ada@x.com", phone="", location="London", links={}, target_role=None,
        experiences=[
            Experience("Northwind", "ML Engineer", "London", "2022", None, True,
                       ["Deployed model services on EKS for 40 clients", "Built Python feature pipelines",
                        "Ran the office book club", "Led go-to-market demos for sales"], True),
            Experience("Babbage", "Data Analyst", "London", "2019", "2022", False,
                       ["Wrote Python reports for finance", "Organised the summer party"], True),
            Experience("Café Uno", "Barista", "Leeds", "2015", "2017", False, ["Served 200 customers a day"], True),
        ],
        education=[], skills=[Skill("Python", "cv"), Skill("Latte art", "cv"), Skill("Docker", "cv")], extras={},
    )


class Model:
    """Answers each kind of request the way a model might, and records what it was asked."""

    def __init__(self, replies: dict):
        self.replies, self.asked = replies, []

    def complete(self, messages, *, json_mode=False):
        prompt = messages[-1]["content"]
        self.asked.append(prompt)
        for prefix, reply in self.replies.items():
            if prompt.startswith(prefix):
                return json.dumps(reply)
        return "{}"


READING = {"items": [
    {"term": "Kubernetes", "need": "serving models in production", "evidence": ["R1.B1", "R9.B9"], "adjacent": [],
     "reasoning": "EKS is managed Kubernetes (R1.B1)", "verdict": "shown",
     "question": "Your Northwind work deployed model services on EKS (R1.B1). Did you write the manifests or run the cluster?",
     "hint": "[what you ran] — [how many services]"},
    {"term": "Go", "need": "writing services", "evidence": [], "adjacent": [], "reasoning": "Only go-to-market, not the language",
     "verdict": "absent", "question": "The team writes services in Go. Have you shipped Go code anywhere?", "hint": "[where] — [what]"},
    {"term": "Invented", "verdict": "shown", "evidence": ["R1.B1"]},
]}


# ---- the reading of the match ------------------------------------------------------------------------------------

def test_the_reading_is_checked_and_asks_specific_questions():
    profile = _profile()
    reqs = JobRequirements.from_dict(REQS)
    plain = match_profile(profile, reqs)
    gaps = [g.to_dict() for g in plain.gaps]
    assert next(g for g in gaps if g["term"] == "Go")["score"] > 0  # the keyword search is fooled by "go-to-market"
    reading = assess(Model({"You are assessing": READING}), profile, REQS, gaps, "Need Kubernetes and Go", "ML Platform Engineer")

    assert set(reading) == {"kubernetes", "go"}  # a requirement that wasn't asked about is ignored
    k = reading["kubernetes"]
    assert k["verdict"] == "shown" and [e["text"] for e in k["evidence"]] == ["Deployed model services on EKS for 40 clients"]
    assert "R1" not in k["question"] and "R1" not in k["reasoning"]  # ids are for the model, not for you

    result = match_profile(profile, reqs, assessment=reading)
    by_term = {g.term: g for g in result.gaps}
    assert by_term["Kubernetes"].same and by_term["Kubernetes"].score >= 40
    assert any(f.get("judged") for f in by_term["Kubernetes"].found)
    assert not by_term["Go"].same and by_term["Go"].score == 0  # "go-to-market" no longer counts as Go
    asked = {q.term: q for q in result.questions}
    assert asked["Go"].prompt == "The team writes services in Go. Have you shipped Go code anywhere?"
    assert asked["Go"].hint == "[where] — [what]" and asked["Go"].need == "writing services"
    assert asked["Kubernetes"].optional  # shown in other words: a level only sharpens the wording


def test_shown_without_evidence_is_not_shown_and_edited_bullets_stop_counting():
    profile = _profile()
    gaps = [g.to_dict() for g in match_profile(profile, JobRequirements.from_dict(REQS)).gaps]
    reply = {"items": [{"term": "Kubernetes", "verdict": "shown", "evidence": ["R7.B1"], "reasoning": "trust me"}]}
    reading = assess(Model({"You are assessing": reply}), profile, REQS, gaps, "", "")
    assert reading["kubernetes"]["verdict"] == "absent"

    reading = assess(Model({"You are assessing": READING}), profile, REQS, gaps, "", "")
    profile.experiences[0].bullets[0] = "Deployed model services"  # the bullet it relied on has changed
    k = next(g for g in match_profile(profile, JobRequirements.from_dict(REQS), assessment=reading).gaps if g.term == "Kubernetes")
    assert not k.same and k.score < 40


# ---- the plan before writing -------------------------------------------------------------------------------------

PLAN = {"needs": ["Run model services in production", "Python data pipelines"],
        "roles": [{"id": "R1", "tier": "core"}, {"id": "R2", "tier": "supporting"}, {"id": "R3", "tier": "peripheral"}],
        "bullets": [{"id": "R1.B2", "relevance": 3, "need": "pipelines"}, {"id": "R1.B1", "relevance": 2},
                    {"id": "R2.B1", "relevance": 2}, {"id": "R3.B1", "relevance": 1}, {"id": "R5.B1", "relevance": 3}],
        "skills_first": ["Python", "Docker"], "skills_drop": ["Latte art"], "angle": "An ML engineer who ships models."}


def test_the_plan_chooses_bullets_by_relevance_within_bounds():
    profile = _profile()
    plan = tailor.validate(PLAN, profile)
    assert (4, 0) not in plan["relevance"]  # a bullet that doesn't exist is ignored
    chosen = tailor.select(profile, plan)
    assert chosen == [[1, 0], [0], []]  # most relevant first; the barista role keeps its title and dates only
    assert tailor.select(profile, {}) == [[0, 1, 2, 3], [0, 1], [0]]  # no plan: every bullet, as before

    thin = tailor.validate({"bullets": [{"id": "R2.B1", "relevance": 3}], "roles": [{"id": "R1", "tier": "peripheral"}]}, profile)
    assert len(tailor.select(profile, thin)[0]) == 2  # your latest role is never left bare

    gaps = [{"term": "Python", "required": True, "found": [{"where": "Data Analyst at Babbage", "text": "Wrote Python reports for finance"}]}]
    only_r1 = tailor.validate({"bullets": [{"id": "R1.B1", "relevance": 3}, {"id": "R1.B2", "relevance": 3}]}, profile)
    assert tailor.select(profile, only_r1, gaps)[1] == [0]  # the evidence for a must-have stays in context


def _pipeline(tmp_path, model):
    store = Store(tmp_path / "db.sqlite")
    store.save_profile(_profile())
    run = store.create_run(job_url="https://www.prodigaltech.example/jobs/1")
    run.jd_text = "ML Platform Engineer at Prodigal. Python and Kubernetes for model serving. Go is a plus."
    store.update_run(run)
    return store, Pipeline(store, lambda: model, tmp_path), run


def test_a_planned_cv_is_focused_and_a_short_reply_never_reverts_a_whole_role(tmp_path):
    rewrite = {"summary": "ML engineer who ships Python model services.",
               "experiences": [{"id": "R1", "bullets": [{"id": "R1.B2", "text": "Built Python feature pipelines for scoring models"}]},
                               {"id": "R2", "bullets": [{"id": "R2.B1", "text": "Automated finance reporting in Python"}]}],
               "skills": ["Python"]}
    model = Model({"Read this job description": REQS, "You are assessing": READING, "You are planning": PLAN,
                   "You are tailoring": rewrite})
    store, pipeline, run = _pipeline(tmp_path, model)
    analyzed = pipeline.analyze(run.id)
    assert analyzed.assessment["kubernetes"]["verdict"] == "shown"
    assert any("Have you shipped Go code" in q["prompt"] for q in analyzed.questions)

    done = pipeline.generate(run.id)
    assert done.status == "ready", done.error
    roles = {e["company"]: e["bullets"] for e in done.draft["experiences"]}
    # R1.B1 wasn't returned: it keeps your wording, and its role's other rewrite is still used (no whole-role revert).
    assert roles["Northwind"] == ["Built Python feature pipelines for scoring models", "Deployed model services on EKS for 40 clients"]
    assert roles["Babbage"] == ["Automated finance reporting in Python"]
    assert roles["Café Uno"] == []  # kept with its dates, no bullets
    assert "Ran the office book club" not in json.dumps(done.draft)
    assert "Latte art" not in done.draft["skills"] and "Docker" in done.draft["skills"]
    text = "\n".join(p.text for p in Document(done.output_cv_path).paragraphs)
    assert "Barista" in text and "Organised the summer party" not in text

    planning = next(p for p in model.asked if p.startswith("You are planning"))
    writing = next(p for p in model.asked if p.startswith("You are tailoring"))
    assert "R1.B1 Deployed model services on EKS" in planning
    assert "Run model services in production" in writing and "R3.B1" not in writing.split("Candidate profile")[0]
    meta = store.list_documents(run.id)[0].meta
    assert meta["tailoring"]["kept"] == 3 and meta["tailoring"]["planned"] and meta["profile_sig"]


def test_without_a_plan_every_bullet_is_written_as_before(tmp_path):
    rewrite = {"summary": "Engineer.", "experiences": [{"company": "Northwind", "title": "ML Engineer",
                                                        "bullets": ["A", "B", "C", "D"]}], "skills": ["Python"]}
    model = Model({"Read this job description": REQS, "You are tailoring": rewrite})
    store, pipeline, run = _pipeline(tmp_path, model)
    pipeline.analyze(run.id)
    done = pipeline.generate(run.id)
    assert done.status == "ready", done.error
    assert [len(e["bullets"]) for e in done.draft["experiences"]] == [4, 2, 1]
    assert done.draft["experiences"][0]["bullets"] == ["A", "B", "C", "D"]


# ---- research knows which company it means ----------------------------------------------------------------------

def test_a_film_of_the_same_name_is_never_taken_for_the_company():
    pages = {"/Prodigal": {"type": "standard", "title": "Prodigal", "description": "2019 American film",
                           "extract": "Prodigal is a 2019 American thriller film produced by a small company."},
             "/Prodigal_(company)": {"type": "standard", "title": "Prodigal (company)", "description": "American software company",
                                     "extract": "Prodigal is an American fintech company.", "wikibase_item": "Q9"}}

    def handler(request):
        if request.url.path.endswith("api.php"):
            return httpx.Response(200, json=["Prodigal", ["Prodigal", "Prodigal (company)"], [], []])
        page = next((v for k, v in pages.items() if request.url.path.endswith(k)), None)
        return httpx.Response(200, json=page) if page else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        found = research.wikipedia("Prodigal", client, SiteThrottle(intervals={}, default=0))
    assert found["title"] == "Prodigal (company)"


def test_research_searches_for_the_company_and_sets_aside_namesakes(tmp_path):
    sent, asked = [], []

    def handler(request):
        if request.url.host == "api.tavily.com":
            body = json.loads(request.content)
            sent.append(body["query"])
            if body["topic"] == "news":
                return httpx.Response(200, json={"results": [
                    {"title": "Prodigal raises $40m for AI collections", "url": "https://techcrunch.com/p", "content": "Fintech."},
                    {"title": "Prodigal movie review", "url": "https://variety.com/p", "content": "A thriller."}]})
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404)

    class Organiser:
        def complete(self, messages, *, json_mode=False):
            asked.append(messages[-1]["content"])
            return json.dumps({"unrelated": ["N2"], "identity": "Prodigal, a fintech collections software company",
                               "what_they_do": "AI for loan servicing.", "points": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        found = research.research("Prodigal", depth="thorough", sources=["tavily"], store=Store(tmp_path / "db.sqlite"),
                                  get_model=lambda: Organiser(), client=client, throttle=SiteThrottle(intervals={}, default=0),
                                  tavily_key="tvly-test-0123456789", job_url="https://www.prodigaltech.example/jobs/1",
                                  context={"title": "ML Platform Engineer", "domain": "fintech collections software"})
    assert sent[0] == '"Prodigal" company fintech collections software'
    assert "Which Prodigal: the employer hiring for \"ML Platform Engineer\", in fintech collections software" in asked[0]
    assert "website https://www.prodigaltech.example" in asked[0]
    assert [n["title"] for n in found["web"]["news"]] == ["Prodigal raises $40m for AI collections"]
    assert found["set_aside"] == 1 and found["summary"]["identity"].startswith("Prodigal, a fintech")
    assert found["version"] == research.VERSION


def test_research_saved_before_it_could_tell_namesakes_apart_is_redone():
    old = {"researched_at": "2099-01-01T00:00:00+00:00", "depth": "thorough"}
    assert not research.fresh(old, "simple")
    assert research.fresh({**old, "version": research.VERSION, "researched_at": research.datetime.now(research.timezone.utc).isoformat()}, "simple")


# ---- a CV written before your profile changed --------------------------------------------------------------------

def test_after_a_profile_edit_the_job_offers_a_new_version_from_it(tmp_path):
    from tests.test_app import FakeModel, _ready_job, fake_fetch
    from cv_maker.app import create_app

    app = create_app(data_dir=tmp_path, model_factory=FakeModel, fetcher=fake_fetch, sync_jobs=True)
    client = app.test_client()
    run = _ready_job(app, client)
    assert run.status == "ready"
    assert "with your changes" not in client.get(f"/runs/{run.id}").data.decode()

    res = client.post("/profile/edit", data={"name": "Ada Lovelace", "email": "ada@example.com", "location": "London",
                                             "exp-0-title": "Engineer", "exp-0-company": "Northwind", "exp-0-start": "2021",
                                             "exp-0-current": "1", "exp-0-bullets": "Built Python APIs\nRan Kubernetes clusters",
                                             "skills": "Python"}, follow_redirects=True)
    assert "1 job was written from your earlier profile" in res.data.decode()
    profile_page = client.get("/profile").data.decode()
    assert "Write new versions with your changes" in profile_page
    page = client.get(f"/runs/{run.id}").data.decode()
    assert "Your profile has changed since v1 was written." in page and "Write v2 with your changes" in page

    client.post(f"/runs/{run.id}/rewrite")
    assert [d.version for d in app.store.list_documents(run.id) if d.kind == "cv"] == [1, 2]
    assert "with your changes" not in client.get(f"/runs/{run.id}").data.decode()
    client.post("/profile/rewrite")
    assert len([d for d in app.store.list_documents(run.id) if d.kind == "cv"]) == 2  # nothing left to rewrite
