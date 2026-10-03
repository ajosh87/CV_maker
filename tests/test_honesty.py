from cv_maker.honesty import CvDocument, filter_cv_document, filter_text
from cv_maker.models import Education, Experience, Profile, Skill


def _profile(**kw):
    base = dict(
        name="A", email="a@x.com", phone="", location="", links={},
        target_role=None, experiences=[], education=[],
        skills=[Skill("Python", "cv")], extras={},
    )
    base.update(kw)
    return Profile(**base)


def test_filter_preserves_allowed_skill():
    doc = CvDocument(name="A", summary="Python engineer", experiences=[], education=[], skills=["Python"])
    filtered = filter_cv_document(doc, _profile())
    assert filtered.skills == ["Python"]
    assert "Python" in filtered.summary


def test_filter_removes_unknown_skill_from_summary_and_skills():
    doc = CvDocument(name="A", summary="Kubernetes expert", experiences=[], education=[], skills=["Python", "Kubernetes"])
    filtered = filter_cv_document(doc, _profile())
    assert "Kubernetes" not in filtered.skills
    assert "Kubernetes" not in filtered.summary
    assert "Python" in filtered.skills


def test_summary_keeps_readable_sentences():
    """Regression: the old word-level filter turned this into "with in Python and Go, in Kafka , PostgreSQL and ."."""
    profile = _profile(
        skills=[Skill(n, "cv") for n in ("Python", "Go", "Kafka", "PostgreSQL", "AWS (ECS, Lambda, S3)")],
        experiences=[Experience("Northwind", "Senior Backend Engineer", "", "2021", None, True, ["Built payment platforms"], True)],
    )
    first = ("Senior backend engineer with nine years building distributed payment platforms in Python and Go, "
             "specialising in Kafka event streaming, PostgreSQL performance and AWS infrastructure.")
    summary = first + " Runs Kubernetes clusters in production."
    filtered = filter_cv_document(CvDocument("", summary, [], [], ["Python"]), profile, banned_terms=["Kubernetes"])
    assert filtered.summary == first


def test_numbers_and_identity_come_from_the_profile():
    exp = Experience("Northwind", "Engineer", "London", "2021-03", None, True,
                     ["Cut latency from 820ms to 190ms", "Led migration of 14 services"], True)
    profile = _profile(name="Priya", experiences=[exp], education=[Education("Leeds", "BEng", "Software", "2010-2014")])
    rewritten = [Experience("Northwind", "Engineer", "", "", None, False,
                            ["Cut p99 latency by 95% (820ms to 40ms)", "Led the Kafka migration of 14 services"], True)]
    filtered = filter_cv_document(CvDocument("Someone Else", "", rewritten, [], []), profile)
    assert filtered.name == "Priya"
    assert filtered.contact == ["a@x.com"]
    bullets = filtered.experiences[0].bullets
    assert bullets[0] == "Cut latency from 820ms to 190ms"  # invented numbers: original kept
    assert bullets[1] == "Led the Kafka migration of 14 services"
    assert (filtered.experiences[0].location, filtered.experiences[0].start) == ("London", "2021-03")
    assert filtered.education[0].school == "Leeds"


def test_mismatched_bullet_count_falls_back_to_original_facts():
    exp = Experience("Acme", "Dev", "", "", None, True, ["One", "Two"], True)
    rewritten = [Experience("Acme", "Dev", "", "", None, False, ["Merged one and two", "Extra", "Invented third"], True)]
    filtered = filter_cv_document(CvDocument("", "", rewritten, [], []), _profile(experiences=[exp]))
    assert filtered.experiences[0].bullets == ["One", "Two"]


def test_filter_text_drops_only_offending_sentences():
    assert filter_text("I use Python. I love Rust. Ship fast.", ["rust"]) == "I use Python. Ship fast."


def test_fact_check_records_what_changed_and_why():
    from cv_maker.honesty import FactCheck

    exp = Experience("Acme", "Dev", "", "", None, True, ["Cut costs 10%", "Shipped APIs"], True)
    rewritten = [Experience("Acme", "Dev", "", "", None, False, ["Cut costs 40%", "Shipped Python APIs"], True)]
    check = FactCheck()
    out = filter_cv_document(CvDocument("", "Great at Python. Rust wizard.", rewritten, [], ["Python", "Rust"]),
                             _profile(experiences=[exp]), check=check)
    reasons = {(i["where"], i["action"]): i["reason"] for i in check.items}
    assert "not a skill in your profile" in reasons[("skills", "removed")]
    assert "Rust" in reasons[("summary", "removed")]
    assert "40" in reasons[("experience", "kept original")]
    assert out.experiences[0].bullets == ["Cut costs 10%", "Shipped Python APIs"]
    assert check.rewrites == [
        {"role": "Dev — Acme", "original": "Cut costs 10%", "final": "Cut costs 10%"},
        {"role": "Dev — Acme", "original": "Shipped APIs", "final": "Shipped Python APIs"},
    ]
