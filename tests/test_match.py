from cv_maker.jobs.match import match_profile
from cv_maker.jobs.requirements import JobRequirements, MustHave, NiceToHave
from cv_maker.models import Profile, Skill


def test_match_caps_questions_at_10():
    profile = Profile(
        name="A",
        email="a@x.com",
        phone="",
        location="",
        links={},
        target_role=None,
        experiences=[],
        education=[],
        skills=[],
        extras={},
    )
    reqs = JobRequirements(
        must_have=[MustHave(term=f"Tool{i}", original=f"Tool {i}") for i in range(10)],
        nice_to_have=[],
        tools=[],
        seniority="",
        domain="",
        education="",
        language="",
        work_auth="",
    )
    result = match_profile(profile, reqs)
    assert len(result.questions) == 10  # one per must-have here; more would be a chore


def test_a_requirement_is_matched_on_its_core():
    profile = Profile(
        name="A",
        email="a@x.com",
        phone="",
        location="",
        links={},
        target_role=None,
        experiences=[],
        education=[],
        skills=[Skill(name="Kubernetes", source="cv")],
        extras={},
    )
    reqs = JobRequirements(
        must_have=[MustHave(term="Kubernetes experience", original="Kubernetes experience")],
        nice_to_have=[],
        tools=[],
        seniority="",
        domain="",
        education="",
        language="",
        work_auth="",
    )
    result = match_profile(profile, reqs)
    assert result.gaps[0].status == "partial" and result.gaps[0].same  # "Kubernetes experience" is Kubernetes, listed


def test_nice_to_have_only_if_missing_and_new_skill():
    profile = Profile(
        name="A",
        email="a@x.com",
        phone="",
        location="",
        links={},
        target_role=None,
        experiences=[],
        education=[],
        skills=[Skill(name="Python", source="cv")],
        extras={},
    )
    reqs = JobRequirements(
        must_have=[],
        nice_to_have=[NiceToHave(term="Python", original="Python"), NiceToHave(term="Rust", original="Rust")],
        tools=[],
        seniority="",
        domain="",
        education="",
        language="",
        work_auth="",
    )
    result = match_profile(profile, reqs)
    terms = [q.term for q in result.questions]
    assert "Python" not in terms
    assert "Rust" in terms


def test_question_wording_avoids_doubled_phrases():
    profile = Profile(name="A", email="", phone="", location="", links={}, target_role=None, experiences=[], education=[], skills=[], extras={})
    reqs = JobRequirements(
        must_have=[MustHave("LLM", "experience building LLM applications"), MustHave("Kafka", "Kafka")],
        nice_to_have=[], tools=[], seniority="", domain="", education="", language="", work_auth="",
    )
    questions = match_profile(profile, reqs).questions
    assert [q.prompt for q in questions] == ["How much experience do you have with LLM?", "How much experience do you have with Kafka?"]
    assert questions[0].context == "experience building LLM applications" and questions[1].context == ""


def test_matching_uses_whole_words():
    profile = Profile(
        name="A", email="", phone="", location="", links={}, target_role=None, experiences=[], education=[],
        skills=[Skill(name="Google Analytics", source="cv"), Skill(name="JavaScript", source="cv"), Skill(name="C++", source="cv")],
        extras={},
    )
    reqs = JobRequirements(
        must_have=[MustHave("Go", "Go"), MustHave("Java", "Java"), MustHave("C++", "C++")],
        nice_to_have=[], tools=[], seniority="", domain="", education="", language="", work_auth="",
    )
    gaps = {g.term: g for g in match_profile(profile, reqs).gaps}
    assert gaps["Go"].score == 0 and gaps["Java"].score == 0  # not "Google Analytics", not "JavaScript"
    assert gaps["C++"].same and gaps["C++"].score >= 55
