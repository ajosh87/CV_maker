from cv_maker.honesty import CvDocument, filter_cv_document
from cv_maker.models import Profile, Skill, Experience
from cv_maker.profile.answers import Answer, apply_answers
from cv_maker.jobs.match import Question


def test_skip_does_not_add_skill():
    profile = Profile(
        name="A", email="a@x.com", phone="", location="", links={},
        target_role=None, experiences=[], education=[],
        skills=[Skill("Python", "cv")], extras={},
    )
    q = Question(term="Kubernetes", prompt="Have you used Kubernetes?", kind="yesno", required=True)
    out = apply_answers(profile, [q], [Answer(term="Kubernetes", skipped=True, text="", add_role=None)])
    assert all(s.name.lower() != "kubernetes" for s in out.skills)


def test_yes_adds_clarification_skill():
    profile = Profile(
        name="A", email="a@x.com", phone="", location="", links={},
        target_role=None, experiences=[], education=[],
        skills=[Skill("Python", "cv")], extras={},
    )
    q = Question(term="Kubernetes", prompt="Have you used Kubernetes?", kind="yesno", required=True)
    out = apply_answers(profile, [q], [Answer(term="Kubernetes", skipped=False, text="yes", add_role=None)])
    assert any(s.name == "Kubernetes" and s.source == "clarification" for s in out.skills)


def test_contradiction_without_add_role_rejected():
    profile = Profile(
        name="A", email="a@x.com", phone="", location="", links={},
        target_role=None,
        experiences=[Experience("Acme", "Eng", "", "2020-01", None, True, ["APIs"], True)],
        education=[], skills=[], extras={},
    )
    q = Question(term="other employer", prompt="Add role?", kind="text", required=False)
    out = apply_answers(
        profile,
        [q],
        [Answer(term="other employer", skipped=False, text="I worked at Globex as CEO", add_role=None)],
    )
    assert all(e.company != "Globex" for e in out.experiences)


def test_add_role_path():
    profile = Profile(
        name="A", email="a@x.com", phone="", location="", links={},
        target_role=None, experiences=[], education=[], skills=[], extras={},
    )
    q = Question(term="role", prompt="Add role?", kind="add_role", required=False)
    out = apply_answers(
        profile,
        [q],
        [Answer(
            term="role",
            skipped=False,
            text="",
            add_role={"company": "Globex", "title": "Dev", "start": "2018-01", "end": "2019-01"},
        )],
    )
    assert out.experiences[0].company == "Globex"


def test_strips_kubernetes_not_in_profile():
    allowed = Profile(
        name="A", email="a@x.com", phone="", location="", links={},
        target_role=None, experiences=[], education=[],
        skills=[Skill("Python", "cv")], extras={},
    )
    doc = CvDocument(
        name="A",
        summary="Kubernetes expert",
        experiences=[],
        education=[],
        skills=["Python", "Kubernetes"],
    )
    filtered = filter_cv_document(doc, allowed)
    assert "Kubernetes" not in filtered.skills
    assert "Kubernetes" not in filtered.summary
    assert "Python" in filtered.skills
