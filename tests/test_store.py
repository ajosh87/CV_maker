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


def test_create_run_with_id(tmp_path):
    store = Store(tmp_path / "cv_maker.sqlite")
    run = store.create_run(job_url="https://www.linkedin.com/jobs/view/1", id="fixed-id")
    assert run.id == "fixed-id"
    got = store.get_run("fixed-id")
    assert got.job_url.endswith("/1")


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


def test_rows_from_older_versions_still_load(tmp_path):
    import json
    import sqlite3
    from contextlib import closing

    store = Store(tmp_path / "cv_maker.sqlite")
    old = {
        "id": "old", "created_at": "2026-09-15T00:00:00", "updated_at": "2026-09-15T00:00:00", "status": "needs_paste",
        "job_url": "https://x", "jd_text": "", "fetch_ok": False, "fetch_reason": "empty description", "requirements": {},
        "gaps": [], "questions": [], "answers": [], "generate_letter": False, "source_cv_path": "", "output_cv_path": "",
        "letter_path": "", "draft": {}, "some_removed_field": 1,
    }
    with closing(sqlite3.connect(tmp_path / "cv_maker.sqlite")) as conn, conn:
        conn.execute("INSERT INTO runs (id, data) VALUES (?, ?)", ("old", json.dumps(old)))
    run = store.get_run("old")
    assert run.error == "" and run.title == ""


def test_interrupted_work_becomes_retryable(tmp_path):
    store = Store(tmp_path / "cv_maker.sqlite")
    fetching = store.create_run(job_url="https://x", status="fetching")
    generating = store.create_run(job_url="https://y", status="generating")
    upload = store.create_upload("cv.docx", "p", "text")
    assert store.reset_interrupted() == 3
    assert store.get_run(fetching.id).status == "needs_paste"
    gen = store.get_run(generating.id)
    assert (gen.status, gen.failed_stage) == ("failed", "generate")
    assert store.get_upload(upload.id).status == "failed"


def test_documents_are_versioned_per_job(tmp_path):
    store = Store(tmp_path / "cv_maker.sqlite")
    run = store.create_run(job_url="https://x")
    assert store.next_version(run.id) == 1
    store.add_document(run, "cv", 1, "a.docx")
    store.add_document(run, "letter", 1, "b.docx")
    assert store.next_version(run.id) == 2
    other = store.create_run(job_url="https://y")
    assert store.next_version(other.id) == 1
    assert [d.kind for d in store.list_documents(run.id)] == ["cv", "letter"]


def test_backfill_registers_files_made_before_versioning(tmp_path):
    store = Store(tmp_path / "cv_maker.sqlite")
    run = store.create_run(job_url="https://x", status="ready")
    cv = tmp_path / "cv_old.docx"
    cv.write_bytes(b"x")
    run.output_cv_path = str(cv)
    store.update_run(run)
    assert store.backfill_documents() == 1
    assert store.backfill_documents() == 0  # idempotent
    assert [(d.kind, d.version) for d in store.list_documents(run.id)] == [("cv", 1)]
