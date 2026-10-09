from cv_maker.library import document_rows, job_rows
from cv_maker.store import Store


def _store(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    acme = store.create_run(job_url="https://acme", status="ready")
    acme.title, acme.company = "Platform Engineer", "Acme"
    store.update_run(acme)
    beta = store.create_run(job_url="https://beta", status="needs_answers")
    beta.title, beta.company = "Data Engineer", "Beta"
    store.update_run(beta)
    store.create_run(job_url="https://gone", status="failed")
    store.add_document(acme, "cv", 1, "a1.docx")
    store.add_document(acme, "cv", 2, "a2.docx")
    store.add_document(acme, "letter", 2, "l2.docx")
    store.add_document(beta, "cv", 1, "b1.docx")
    upload = store.create_upload("my_cv.pdf", "p", "text")
    upload.status, upload.summary = "ready", {"name": "Ada", "roles": ["x"]}
    store.update_upload(upload)
    return store, acme, beta


def test_counts_per_category(tmp_path):
    store, *_ = _store(tmp_path)
    data = document_rows(store)
    assert dict(data["counts"]) == {"cv": 3, "letter": 1, "source": 1, "all": 5}


def test_latest_only_and_kind_filter(tmp_path):
    store, acme, _ = _store(tmp_path)
    rows = document_rows(store, kind="cv", latest=True)["rows"]
    assert sorted((r.title, r.version) for r in rows) == [("Data Engineer", 1), ("Platform Engineer", 2)]


def test_search_company_and_job_filters(tmp_path):
    store, acme, beta = _store(tmp_path)
    assert {r.company for r in document_rows(store, q="platform")["rows"]} == {"Acme"}
    assert all(r.company == "Beta" for r in document_rows(store, company="Beta")["rows"])
    assert {r.run_id for r in document_rows(store, job=acme.id)["rows"]} == {acme.id}
    assert document_rows(store, q="my_cv")["rows"][0].kind == "source"


def test_sorting_and_grouping(tmp_path):
    store, acme, beta = _store(tmp_path)
    by_company = [r.company for r in document_rows(store, kind="cv", sort="company")["rows"]]
    assert by_company == sorted(by_company, key=str.lower)
    newest = document_rows(store, sort="newest")["rows"]
    assert [r.created_at for r in newest] == sorted((r.created_at for r in newest), reverse=True)
    groups = document_rows(store, group="job")["groups"]
    labels = [g[0] for g in groups]
    assert "Platform Engineer · Acme" in labels and "Source CVs" in labels
    by_kind = {g[0]: len(g[2]) for g in document_rows(store, group="kind")["groups"]}
    assert by_kind == {"Tailored CVs": 3, "Cover letters": 1, "Source CVs": 1}


def test_archived_jobs_hidden_by_default(tmp_path):
    store, acme, _ = _store(tmp_path)
    store.set_archived([acme.id], True)
    assert all(r.run_id != acme.id for r in document_rows(store)["rows"])
    assert any(r.run_id == acme.id for r in document_rows(store, include_archived=True)["rows"])
    jobs = job_rows(store)
    assert jobs["counts"]["archived"] == 1 and jobs["counts"]["all"] == 2
    assert [row["run"].id for row in job_rows(store, bucket="archived")["rows"]] == [acme.id]


def test_job_buckets_and_sort(tmp_path):
    store, acme, beta = _store(tmp_path)
    counts = job_rows(store)["counts"]
    assert (counts["ready"], counts["needs"], counts["working"]) == (1, 2, 0)
    rows = job_rows(store, sort="title")["rows"]
    assert [row["label"] for row in rows] == ["Data Engineer", "https://gone", "Platform Engineer"]
    assert {row["run"].id: row["versions"] for row in job_rows(store)["rows"]}[acme.id] == 2


def test_invalid_options_fall_back_to_defaults(tmp_path):
    store, *_ = _store(tmp_path)
    data = document_rows(store, kind="bogus", sort="bogus", group="bogus")
    assert (data["kind"], data["sort"], data["group"]) == ("all", "newest", "none")
