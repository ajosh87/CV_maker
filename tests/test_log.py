"""The log: a trace of what went wrong, with the job it belongs to, and nothing personal in it."""
from pathlib import Path

from cv_maker import logs
from cv_maker.app import create_app
from cv_maker.jobs.fetch import FetchResult
from cv_maker.models import Experience, Profile, Skill
from cv_maker.privacy import Secret


def _ada(store):
    store.save_profile(Profile(name="Ada Lovelace", email="ada@example.com", phone="+44 20 7946 0958", location="London",
                               links={}, target_role=None,
                               experiences=[Experience("Northwind", "Engineer", "London", "2019", None, True, ["Built APIs"], True)],
                               education=[], skills=[Skill("Python", "cv")], extras={}))


def test_a_failure_is_in_the_log_with_its_job_and_traceback_and_nothing_personal(tmp_path):
    class Broken:
        def complete(self, messages, *, json_mode=False):
            raise RuntimeError("boom near Ada Lovelace <ada@example.com> with key sk-abcdefghijklmnopqrstuvwx")

    fetch = lambda url: FetchResult(ok=True, text="Need Python. " * 30, reason="", title="Engineer", company="Acme")  # noqa: E731
    app = create_app(data_dir=tmp_path, model_factory=Broken, fetcher=fetch, sync_jobs=True)
    _ada(app.store)
    client = app.test_client()
    client.post("/jobs", data={"urls": "https://careers.acme.com/job/1"})
    run = app.store.list_runs()[0]
    assert run.status == "failed"
    assert f'href="/settings/log?ref={run.id[:6]}' in client.get(f"/runs/{run.id}").data.decode()

    raw = (tmp_path / "logs" / logs.FILE).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in raw and "RuntimeError" in raw
    assert f"[job {run.id[:6]}]" in raw and "ERROR" in raw and "WARNING" in raw
    assert "Ada" not in raw and "Lovelace" not in raw and "ada@example.com" not in raw and "sk-abcdefghijkl" not in raw
    assert "[[NAME]]" in raw and "[[EMAIL]]" in raw and "[[KEY]]" in raw

    page = client.get(f"/settings/log?ref={run.id[:6]}&level=errors").data.decode()
    assert "analysis failed" in page and "Traceback" not in page.split("analysis failed")[0][-200:]
    assert "Ada Lovelace" not in page
    warnings = client.get(f"/settings/log?ref={run.id[:6]}&level=warnings").data.decode()
    assert "Traceback and details" in warnings and "RuntimeError" in warnings

    download = client.get("/settings/log/download")
    assert "attachment" in download.headers["Content-Disposition"] and b"RuntimeError" in download.data
    client.post("/settings/log/clear")
    assert logs.read(tmp_path)["total"] == 0


def test_the_redactor_masks_keys_your_details_and_your_home_folder():
    redact = logs.Redactor(secrets=lambda: [Secret("NAME", "Ada Lovelace"), Secret("PHONE", "+44 20 7946 0958")],
                           keys=lambda: ["my-own-provider-key-123"])
    home = str(Path.home())
    text = redact(f"Ada called +44 20 7946 0958 from {home}/data with Bearer abc.def and my-own-provider-key-123 tvly-0123456789abc")
    assert "Ada" not in text and "7946" not in text and home not in text and "~/data" in text
    assert "abc.def" not in text and "my-own-provider-key-123" not in text and "tvly-0123456789abc" not in text
    assert "2026-10-03 15:41:53" in redact("2026-10-03 15:41:53 dates and https://careers.acme.com/job/1 stay readable")
    assert "https://careers.acme.com/job/1" in redact("https://careers.acme.com/job/1")


def test_settings_says_how_many_problems_there_were(tmp_path):
    app = create_app(data_dir=tmp_path, fetcher=lambda url: None)
    page = app.test_client().get("/settings").data.decode()
    assert "No errors or warnings in the last day" in page
    import logging
    logging.getLogger("cv_maker.test").error("something broke")
    page = app.test_client().get("/settings").data.decode()
    assert "1 error and 0 warnings in the last day" in page
