"""Progress bars (one job, every job, one application) and the light / dark / auto theme."""
import json
from io import BytesIO
from types import SimpleNamespace

from docx import Document

from cv_maker.app import create_app
from cv_maker.jobs.fetch import FetchResult
from cv_maker.progress import job_progress, overall


def _run(status, failed_stage="", jd_text="Need Python"):
    return SimpleNamespace(status=status, failed_stage=failed_stage, jd_text=jd_text)


def test_a_jobs_steps_follow_where_it_is():
    states = lambda p: [s["state"] for s in p["steps"]]  # noqa: E731
    assert states(job_progress(_run("fetching"))) == ["working", "todo", "todo", "todo", "todo"]
    assert states(job_progress(_run("needs_answers"))) == ["done", "done", "you", "todo", "todo"]
    assert states(job_progress(_run("generating"))) == ["done", "done", "done", "working", "todo"]
    assert states(job_progress(_run("failed", "generate"))) == ["done", "done", "done", "failed", "todo"]
    ready = job_progress(_run("ready"), versions=1)
    assert states(ready) == ["done"] * 4 + ["todo"] and ready["percent"] == 80 and ready["current"] == "Applied"
    applying = job_progress(_run("ready"), application="needs_you")
    assert applying["steps"][-1]["state"] == "you" and applying["state"] == "you"
    applied = job_progress(_run("ready"), application="submitted")
    assert applied["percent"] == 100 and applied["state"] == "applied" and applied["number"] == 5
    together = overall([applied, ready, job_progress(_run("needs_answers"))])
    assert together["jobs"] == 3 and together["percent"] == round((100 + 80 + 40) / 3)
    assert together["counts"] == {"applied": 1, "ready": 1, "working": 0, "you": 1, "failed": 0}


class Model:
    def complete(self, messages, *, json_mode=False):
        prompt = messages[-1]["content"]
        if prompt.startswith("Extract a structured profile"):
            return json.dumps({"name": "Ada Lovelace", "email": "", "phone": "", "location": "", "links": {}, "skills": ["Python"],
                               "experiences": [{"company": "Northwind", "title": "Engineer", "bullets": ["Built APIs"]}], "education": []})
        if prompt.startswith("Read this job description"):
            return json.dumps({"job_title": "Platform Engineer", "company": "Acme", "must_have": [{"term": "Python", "original": "Python"}]})
        return "{}"


def test_the_job_page_the_list_and_the_api_show_progress(tmp_path):
    fetch = lambda url: FetchResult(ok=True, text="Need Python. " * 20, reason="", title="Engineer", company="Acme")  # noqa: E731
    app = create_app(data_dir=tmp_path, model_factory=Model, fetcher=fetch, sync_jobs=True)
    client = app.test_client()
    buf = BytesIO()
    doc = Document()
    doc.add_paragraph("Ada Lovelace. Engineer at Northwind since 2021, building Python APIs used by three teams across the company.")
    doc.save(buf)
    res = client.post("/profile/upload", data={"cv": (BytesIO(buf.getvalue()), "cv.docx")}, content_type="multipart/form-data")
    client.post(res.headers["Location"], data={"action": "send"})
    client.post("/jobs", data={"urls": "https://careers.acme.com/job/1\nhttps://careers.acme.com/job/2"})
    run = app.store.list_runs()[0]

    page = client.get(f"/runs/{run.id}").data.decode()
    assert 'class="stepper"' in page and page.count("st-done") == 2 and "st-you" in page and "step 3 of 5, Your answers" in page
    jobs = client.get("/jobs").data.decode()
    assert 'id="overall"' in jobs and "40%</strong> of the way" in jobs and "2 need you" in jobs and jobs.count('class="mini-bar you"') == 2
    api = client.get(f"/api/runs?ids={run.id}").get_json()
    assert api["runs"][run.id]["progress"]["percent"] == 40 and api["overall"]["jobs"] == 2


def test_the_theme_can_be_light_dark_or_follow_the_system(tmp_path):
    app = create_app(data_dir=tmp_path, fetcher=lambda url: None)
    client = app.test_client()
    page = client.get("/settings").data.decode()
    assert "data-theme" not in page.split("<head>")[0] and 'name="theme" value="light"' in page  # auto; the button offers light
    client.post("/settings/theme", data={"theme": "light", "next": "/jobs"})
    page = client.get("/jobs").data.decode()
    assert '<html lang="en" class="no-js" data-theme="light">' in page and 'name="theme" value="dark"' in page
    client.post("/settings/theme", data={"theme": "neon"})
    assert "data-theme" not in client.get("/jobs").data.decode().split("<head>")[0]  # anything else: auto
