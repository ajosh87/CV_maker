from cv_maker.models import Experience, Profile, Skill
from cv_maker.store import Store


def test_save_and_load_single_profile(tmp_path):
    store = Store(tmp_path / "cv_maker.sqlite")
    store.save_profile(
        Profile(
            name="Ada",
            email="ada@example.com",
            phone="",
            location="Berlin",
            links={},
            target_role=None,
            experiences=[
                Experience(
                    company="Acme",
                    title="Engineer",
                    location="",
                    start="2020-01",
                    end=None,
                    current=True,
                    bullets=["Built APIs"],
                    confirmed=True,
                )
            ],
            education=[],
            skills=[Skill(name="Python", source="cv")],
            extras={},
        )
    )
    loaded = store.get_profile()
    assert loaded is not None
    assert loaded.email == "ada@example.com"
    assert loaded.experiences[0].company == "Acme"
    loaded.phone = "1"
    store.save_profile(loaded)
    assert store.get_profile().phone == "1"


def test_create_run_default_status(tmp_path):
    store = Store(tmp_path / "cv_maker.sqlite")
    run = store.create_run(job_url="https://www.linkedin.com/jobs/view/1")
    assert run.status == "needs_paste" or run.status in {
        "needs_paste",
        "needs_answers",
        "ready",
        "failed",
    }
    got = store.get_run(run.id)
    assert got.job_url.endswith("/1")
