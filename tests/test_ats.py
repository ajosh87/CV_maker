"""The core: requirements split into single skills, matched with traceable evidence, asked about by level, and a CV
written and checked the way an applicant tracking system reads it."""
import json

import pytest

from cv_maker import ats
from cv_maker import skills as sk
from cv_maker.honesty import CvDocument, FactCheck, filter_cv_document
from cv_maker.jobs.requirements import JobRequirements
from cv_maker.models import Experience, Profile, Skill
from cv_maker.pipeline.graph import Pipeline
from cv_maker.store import Store


def _profile(**kw):
    base = dict(name="Ada Lovelace", email="ada@example.com", phone="+44 20 7946 0958", location="London", links={}, target_role=None,
                experiences=[Experience("Contoso", "AI Engineer", "London", "2023", None, True,
                                        ["Built a multimodal RAG pipeline on Azure OpenAI and Azure AI Search, cutting search time by 40%",
                                         "Deployed services with Docker"], True)],
                education=[], skills=[Skill("multimodal RAG", "cv"), Skill("Azure OpenAI", "cv"), Skill("PostgreSQL", "cv"),
                                      Skill("Docker", "cv"), Skill("K8s", "cv")], extras={})
    base.update(kw)
    return Profile(**base)


@pytest.mark.parametrize("text, parts", [
    ("Python, Java, JavaScript/TypeScript, React", ["Python", "Java", "JavaScript", "TypeScript", "React"]),
    ("Experience with Python, Java and React", ["Python", "Java", "React"]),
    ("Python and Java", ["Python", "Java"]),
    ("Cloud platforms (AWS, Azure or GCP)", ["AWS", "Azure", "GCP"]),
    ("Retrieval-Augmented Generation (RAG)", ["Retrieval-Augmented Generation"]),
    ("CI/CD", ["CI/CD"]),
    ("UI/UX design", ["UI/UX design"]),
    ("Research and Development", ["Research and Development"]),
    ("Strong communication skills", ["Strong communication skills"]),
])
def test_a_composite_requirement_becomes_single_skills(text, parts):
    assert sk.split_requirement(text) == parts


def test_requirements_from_the_llm_are_split_too():
    reqs = JobRequirements.from_dict({"must_have": [{"term": "Python, Java, JavaScript/TypeScript, React", "original": "Python, Java, JS/TS, React"}]})
    assert [m.term for m in reqs.must_have] == ["Python", "Java", "JavaScript", "TypeScript", "React"]
    assert {m.original for m in reqs.must_have} == {"Python, Java, JS/TS, React"}


def test_evidence_is_traceable_and_understands_other_names():
    p = _profile()
    rag = sk.evidence("Retrieval-Augmented Generation", p)
    assert rag["strength"] == "strong" and rag["score"] == 70
    assert [f["why"] for f in rag["found"]] == ["your skill “multimodal RAG” includes it", "in a bullet (current role)"]
    assert sk.evidence("Azure AI services", p)["strength"] == "strong"  # Azure OpenAI is one of them
    assert sk.evidence("Kubernetes", p)["found"][0]["why"].startswith("listed as “K8s”")
    assert sk.evidence("SQL", p)["found"][0]["why"] == "“PostgreSQL” relies on it"
    assert sk.evidence("Azure AI Search", p)["strength"] == "partial"  # only in a bullet, not the same as Azure OpenAI
    assert sk.evidence("Java", p) == {"term": "Java", "score": 0, "strength": "none", "same": False, "level": "", "found": []}


def test_a_level_you_give_is_evidence_but_beginner_is_never_strong():
    p = _profile()
    assert sk.evidence("Go", p, "beginner")["strength"] == "partial"
    assert sk.evidence("Go", p, "intermediate")["strength"] == "strong"
    assert sk.overclaims("An expert in Go who leads teams.", "Go", "beginner")
    assert not sk.overclaims("Familiar with Go from side projects.", "Go", "beginner")
    assert sk.overclaims("Deep expertise in Kubernetes.", "Kubernetes", "intermediate")


def test_honesty_keeps_your_skills_under_the_postings_names_and_holds_levels():
    p = _profile(skills=[Skill("multimodal RAG", "cv"), Skill("Go", "clarification", level="beginner")])
    draft = CvDocument(name="", summary="Expert in Go, building services daily. Builds RAG systems.", education=[],
                       experiences=[Experience("Contoso", "AI Engineer", "", "", None, False,
                                               ["Built a multimodal RAG pipeline with expert Go skills", "Deployed services with Docker"], True)],
                       skills=["Retrieval-Augmented Generation", "Go", "Rust"])
    check = FactCheck()
    out = filter_cv_document(draft, p, [], check, "AI Engineer Acme")
    assert "Retrieval-Augmented Generation" in out.skills and "Rust" not in out.skills
    assert out.summary == "Builds RAG systems."  # the beginner skill isn't called expert
    assert out.experiences[0].bullets[0] == p.experiences[0].bullets[0]  # reverted to your words
    assert any("beyond the level you gave (beginner)" in i["reason"] for i in check.items)


def test_the_ats_check_counts_what_a_tracking_system_reads():
    plan = [{"term": "Python", "required": True, "have": True, "level": "", "score": 80, "support": []},
            {"term": "Kubernetes", "required": True, "have": True, "level": "", "score": 70, "support": []},
            {"term": "Java", "required": True, "have": False, "level": "", "score": 0, "support": []},
            {"term": "Terraform", "required": False, "have": False, "level": "", "score": 0, "support": []}]
    cv = {"headline": "Platform Engineer", "summary": "Platform Engineer building Python services.", "skills": ["Python", "Kubernetes"],
          "experiences": [{"company": "Contoso", "title": "Engineer", "start": "2021", "bullets": ["Built Python APIs used by 3 teams"] * 4}],
          "education": [{"credential": "BSc", "field": "CS"}], "contact": ["ada@example.com", "+44 20 7946 0958"]}
    result = ats.check(cv, plan, "Platform Engineer (m/f/d)")
    by = {c["key"]: c for c in result["checks"]}
    assert by["must"]["points"] == 30 and by["must"]["detail"] == "2 of 3 in your CV"  # Java is an honest gap
    assert "Not in your profile: Java" in by["must"]["fix"]
    assert by["context"]["points"] == 5  # Python is in a bullet; Kubernetes only in the skills list
    assert by["title"]["ok"] and by["sections"]["ok"] and by["contact"]["ok"] and by["dates"]["ok"] and by["numbers"]["ok"]
    assert result["lacking"] == ["Java"] and result["missing_have"] == []
    kw = {k["term"]: k for k in result["keywords"]}
    assert kw["Python"]["places"][:2] == ["summary", "skills"] and kw["Kubernetes"]["in_context"] is False


def test_optimising_uses_the_postings_words_for_what_you_have_and_nothing_else():
    doc = CvDocument(name="Ada", summary="", experiences=[], education=[], skills=["multimodal RAG", "K8s", "Docker"])
    plan = [{"term": "RAG", "required": True, "have": True, "level": "", "score": 70, "support": []},
            {"term": "Kubernetes", "required": True, "have": True, "level": "", "score": 55, "support": []},
            {"term": "Java", "required": True, "have": False, "level": "", "score": 0, "support": []}]
    out = ats.optimise(doc, plan, "AI Engineer - Remote")
    assert out.skills == ["Retrieval-Augmented Generation (RAG)", "Kubernetes", "multimodal RAG", "Docker"]
    assert "Java" not in out.skills and out.headline == "AI Engineer"


class _Model:
    def __init__(self):
        self.prompts = []

    def complete(self, messages, *, json_mode=False):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if prompt.startswith("Read this job description"):
            return json.dumps({"job_title": "AI Engineer", "company": "Acme", "notes": [],
                               "must_have": [{"term": "RAG, Kubernetes and Java", "original": "RAG, Kubernetes and Java"}]})
        if prompt.startswith("You are tailoring"):
            return json.dumps({"summary": "AI Engineer who builds RAG pipelines on Azure OpenAI.", "skills": ["multimodal RAG", "Docker"],
                               "experiences": [{"company": "Contoso", "title": "AI Engineer",
                                                "bullets": ["Built a multimodal RAG pipeline on Azure OpenAI and Azure AI Search, cutting search time by 40%",
                                                            "Deployed services with Docker"]}]})
        return "{}"


def test_the_pipeline_writes_for_the_ats_and_records_the_check(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.save_profile(_profile())
    model = _Model()
    pipeline = Pipeline(store, lambda: model, tmp_path)
    run = store.create_run(job_url="https://careers.acme.com/1")
    run.jd_text, run.title = "AI Engineer. RAG, Kubernetes and Java. " * 5, "AI Engineer"
    store.update_run(run)
    analyzed = pipeline.analyze(run.id)
    assert [g["term"] for g in analyzed.gaps] == ["RAG", "Kubernetes", "Java"]
    assert [q["term"] for q in analyzed.questions if not q["optional"]] == ["Java"]
    analyzed.answers = [{"term": "Java", "skipped": False, "text": "beginner", "add_role": None, "level": "beginner"}]
    store.update_run(analyzed)
    done = pipeline.generate(run.id)
    assert done.status == "ready", done.error
    writing = next(p for p in model.prompts if p.startswith("You are tailoring"))
    assert "Keywords from the posting that the candidate HAS" in writing and "- Java (must-have, beginner level)" in writing
    assert '"skill_levels"' in writing and 'beginner = "familiar with"' in writing  # the level rules, written plainly
    meta = store.list_documents(run.id)[0].meta
    assert meta["ats"]["score"] >= 70 and meta["draft"]["headline"] == "AI Engineer"
    assert meta["draft"]["skills"][:3] == ["Retrieval-Augmented Generation (RAG)", "Kubernetes", "Java"]
    assert store.get_profile().skills[-1].level == "beginner"  # the level is kept for every CV from now on
