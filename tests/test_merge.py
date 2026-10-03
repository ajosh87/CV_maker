from cv_maker.models import Experience, Profile, Skill
from cv_maker.profile.merge import merge_profile


def _p(**kwargs):
    base = Profile(
        name="",
        email="",
        phone="",
        location="",
        links={},
        target_role=None,
        experiences=[],
        education=[],
        skills=[],
        extras={},
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def test_fill_empty_fields():
    existing = _p(name="Ada", email="a@x.com", phone="")
    incoming = _p(name="Ada Lovelace", email="a@x.com", phone="99")
    out = merge_profile(existing, incoming)
    assert out.name == "Ada"
    assert out.phone == "99"


def test_add_new_role_does_not_drop_old():
    existing = _p(
        experiences=[
            Experience("Acme", "Eng", "", "2020-01", None, True, ["APIs"], True)
        ]
    )
    incoming = _p(
        experiences=[
            Experience("Acme", "Eng", "", "2020-01", "2024-01", False, ["APIs"], False),
            Experience("Beta", "Lead", "", "2024-02", None, True, ["Lead"], False),
        ]
    )
    out = merge_profile(existing, incoming)
    companies = {e.company for e in out.experiences}
    assert companies == {"Acme", "Beta"}
    acme = next(e for e in out.experiences if e.company == "Acme")
    assert acme.end == "2024-01"
    assert acme.current is False
    assert acme.confirmed is True


def test_none_existing_returns_incoming_confirmed():
    incoming = _p(
        name="Ada",
        experiences=[
            Experience("Acme", "Eng", "", "2020-01", None, True, ["APIs"], False)
        ],
    )
    out = merge_profile(None, incoming)
    assert out.experiences[0].confirmed is True
    assert out.name == "Ada"


def test_confirmed_bullets_not_overwritten():
    existing = _p(
        experiences=[
            Experience("Acme", "Eng", "", "2020-01", None, True, ["APIs"], True)
        ]
    )
    incoming = _p(
        experiences=[
            Experience("Acme", "Eng", "", "2020-01", None, True, ["New bullet"], False),
        ]
    )
    out = merge_profile(existing, incoming)
    assert out.experiences[0].bullets == ["APIs"]


def test_merge_adds_new_skill():
    existing = _p(skills=[Skill("Python", "cv")])
    incoming = _p(skills=[Skill("Kubernetes", "cv")])
    out = merge_profile(existing, incoming)
    assert any(s.name == "Kubernetes" for s in out.skills)
