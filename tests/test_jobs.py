import httpx

from cv_maker.jobs.fetch import fetch_job_url
from cv_maker.jobs.html import extract_job_description
from cv_maker.jobs.urls import split_urls
from cv_maker.store import Store

OK_HTML = """
<html><body>
<div class="jobs-description__content">Need Python and SQL</div>
<nav>Home Jobs</nav>
</body></html>
"""

AUTH_HTML = """
<html><body>
<div class="authwall">Sign in</div>
</body></html>
"""


def test_split_ignores_blank_lines():
    assert split_urls("\nhttps://a\n\nhttps://b\n") == ["https://a", "https://b"]


def test_extract_uses_description_not_nav():
    text = extract_job_description(OK_HTML)
    assert "Python" in text
    assert "Home Jobs" not in text


def test_extract_authwall_empty():
    assert extract_job_description(AUTH_HTML) == ""


def test_fetch_403(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=AUTH_HTML)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    result = fetch_job_url("https://www.linkedin.com/jobs/view/1", client=client)
    assert result.ok is False
    assert result.text == ""


def test_batch_one_fail_two_ok(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/fail"):
            return httpx.Response(403, text=AUTH_HTML)
        return httpx.Response(200, text=OK_HTML)

    store = Store(tmp_path / "db.sqlite")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    analyzed = []
    tasks = _tasks(store, lambda url: fetch_job_url(url, client=client), analyzed)
    urls = [
        "https://www.linkedin.com/jobs/view/ok1",
        "https://www.linkedin.com/jobs/view/fail",
        "https://www.linkedin.com/jobs/view/ok2",
    ]
    for url in urls:
        tasks.queue_fetch(store.create_run(job_url=url))
    statuses = {r.job_url: r.status for r in store.list_runs()}
    assert statuses[urls[0]] == "analyzing" and statuses[urls[2]] == "analyzing"  # handed to the pipeline
    assert statuses[urls[1]] == "needs_paste"
    assert len(analyzed) == 2


def _tasks(store, fetcher, analyzed):
    from cv_maker.tasks import Runner, Tasks

    class PipelineStub:
        def analyze(self, run_id):
            analyzed.append(run_id)

    return Tasks(store, PipelineStub(), lambda: None, fetcher, Runner(sync=True))


def test_fetch_crash_does_not_leave_job_spinning(tmp_path):
    store = Store(tmp_path / "db.sqlite")

    def crash(url):
        raise RecursionError("maximum recursion depth exceeded")

    tasks = _tasks(store, crash, [])
    run = store.create_run(job_url="https://jobs.example.com/x")
    tasks.queue_fetch(run)
    after = store.get_run(run.id)
    assert after.status == "needs_paste" and after.step == ""
    assert "RecursionError" in after.fetch_reason and "Paste the description" in after.fetch_reason


def test_fetch_sends_user_agent_and_no_cookies(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ua"] = request.headers.get("user-agent")
        captured["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, text=OK_HTML)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    fetch_job_url("https://www.linkedin.com/jobs/view/1", client=client)
    assert captured["ua"] == "Mozilla/5.0 (compatible; CVTailor/0.1)"
    assert captured["cookie"] is None


# ---- real-world LinkedIn markup, URL handling and fetch behaviour ----

from pathlib import Path

from cv_maker.jobs.batch import apply_fetch_result
from cv_maker.jobs.fetch import FetchResult
from cv_maker.jobs.html import parse_job_page
from cv_maker.jobs.urls import normalize_job_url, parse_url_lines

GUEST_HTML = (Path(__file__).parent / "fixtures" / "linkedin_guest.html").read_text(encoding="utf-8")


def test_guest_page_with_valueless_class_attribute_is_parsed():
    page = parse_job_page(GUEST_HTML)
    assert page.title == "Senior Backend Engineer"
    assert page.company == "Acme Payments"
    assert "Kubernetes in production" in page.description
    assert "• 5+ years of Python" in page.description
    assert "Nice to have: Rust." in page.description


def test_description_stops_at_its_container():
    text = parse_job_page(GUEST_HTML).description
    for leaked in ("Cookie Policy", "footer-script", "Show more", "tracking"):
        assert leaked not in text


def test_br_tags_become_line_breaks():
    text = parse_job_page(GUEST_HTML).description
    assert "About the role\n\nWe build payment rails" in text


def test_json_ld_job_posting_is_used_for_other_sites():
    html = """<html><head><script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "JobPosting", "title": "Data Engineer",
     "hiringOrganization": {"@type": "Organization", "name": "Globex"},
     "description": "&lt;p&gt;Build pipelines in &lt;b&gt;Spark&lt;/b&gt;.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Airflow&lt;/li&gt;&lt;/ul&gt;"}
    </script></head><body><div>Unrelated chrome</div></body></html>"""
    page = parse_job_page(html)
    assert (page.title, page.company) == ("Data Engineer", "Globex")
    assert "Build pipelines in Spark." in page.description
    assert "• Airflow" in page.description
    assert "Unrelated chrome" not in page.description


def test_unknown_page_fails_closed():
    assert parse_job_page("<html><body><main>Lots of text but no job container</main></body></html>").description == ""


def test_linkedin_urls_are_normalized():
    expected = "https://www.linkedin.com/jobs/view/4471998654/"
    assert normalize_job_url("https://www.linkedin.com/jobs/search-results/?currentJobId=4471998654&keywords=AI") == expected
    assert normalize_job_url("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4471998654") == expected
    assert normalize_job_url("https://uk.linkedin.com/jobs/view/senior-engineer-at-acme-4471998654?trk=x") == expected
    assert normalize_job_url("https://boards.greenhouse.io/acme/jobs/123") == "https://boards.greenhouse.io/acme/jobs/123"


def test_parse_url_lines_validates_and_dedupes():
    text = "https://www.linkedin.com/jobs/view/4471998654/\nnot a url\n\nhttps://www.linkedin.com/jobs/search/?currentJobId=4471998654\nftp://x.y/z\n"
    valid, invalid = parse_url_lines(text)
    assert valid == ["https://www.linkedin.com/jobs/view/4471998654/"]
    assert invalid == ["not a url", "ftp://x.y/z"]


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_login_redirect_is_named_not_reported_as_cookie_problem():
    def handler(request):
        if "jobs-guest" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(307, headers={"location": "https://www.linkedin.com/uas/login?session_redirect=x", "set-cookie": "bcookie=1"})

    result = fetch_job_url("https://www.linkedin.com/jobs/view/4471998654/", client=_client(handler))
    assert result.ok is False
    assert "sign" in result.reason.lower() and "login" in result.reason.lower()


def test_search_link_is_fetched_from_the_job_page():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=GUEST_HTML)

    result = fetch_job_url("https://www.linkedin.com/jobs/search-results/?currentJobId=4471998654&keywords=x", client=_client(handler))
    assert result.ok and result.title == "Senior Backend Engineer"
    assert seen == ["https://www.linkedin.com/jobs/view/4471998654/"]


def test_guest_api_fallback_when_view_page_is_empty():
    def handler(request):
        if "jobs-guest" in request.url.path:
            return httpx.Response(200, text=GUEST_HTML)
        return httpx.Response(200, text="<html><body><div class='authwall'>Join to view</div></body></html>")

    result = fetch_job_url("https://www.linkedin.com/jobs/view/4471998654/", client=_client(handler))
    assert result.ok
    assert "Kafka" in result.text


def test_cookies_set_on_redirect_are_not_sent():
    cookies = []

    def handler(request):
        cookies.append(request.headers.get("cookie"))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/job", "set-cookie": "tracker=abc; Path=/"})
        return httpx.Response(200, text=OK_HTML)

    result = fetch_job_url("https://jobs.example.com/start", client=_client(handler))
    assert result.ok
    assert cookies == [None, None]


def test_status_codes_have_readable_reasons():
    for status, word in ((404, "not found"), (429, "rate-limiting"), (999, "rate-limiting"), (500, "server error")):
        result = fetch_job_url("https://jobs.example.com/x", client=_client(lambda r, s=status: httpx.Response(s, text=OK_HTML)))
        assert result.ok is False and word in result.reason.lower(), (status, result.reason)


def test_failed_refetch_keeps_pasted_description(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    run = store.create_run(job_url="https://jobs.example.com/x")
    run.jd_text = "Pasted by the user"
    apply_fetch_result(run, FetchResult(ok=False, text="", reason="blocked"))
    assert run.jd_text == "Pasted by the user"
    assert run.status == "needs_answers"
