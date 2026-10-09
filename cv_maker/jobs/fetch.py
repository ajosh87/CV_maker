from contextlib import nullcontext
from urllib.parse import urljoin, urlparse

import httpx

from cv_maker.jobs.html import parse_job_page
from cv_maker.jobs.urls import linkedin_job_id, normalize_job_url, site_of

USER_AGENT = "Mozilla/5.0 (compatible; CVTailor/0.1)"
_MAX_REDIRECTS = 5
_LOGIN_PATH_MARKERS = ("/login", "/uas/", "/authwall", "/checkpoint", "/signin", "/sign-in", "/signup")


class FetchResult:
    def __init__(self, ok: bool, text: str, reason: str, title: str = "", company: str = "", apply_url: str = "",
                 easy_apply: bool = False, offsite_apply: bool = False) -> None:
        self.ok = ok
        self.text = text
        self.reason = reason
        self.title = title
        self.company = company
        self.apply_url = apply_url  # where the application itself starts, when the page says
        self.easy_apply = easy_apply  # applying happens on the job board itself (e.g. LinkedIn Easy Apply)
        self.offsite_apply = offsite_apply  # its Apply button leads to the employer's own site


def _is_login_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(marker in path for marker in _LOGIN_PATH_MARKERS)


def _status_reason(status: int) -> str:
    if status in (404, 410):
        return f"Job page not found (HTTP {status}); the posting may have expired."
    if status in (401, 403):
        return f"The site blocked automated access (HTTP {status})."
    if status in (429, 999):
        return f"The site is rate-limiting requests (HTTP {status}). Wait a minute and retry."
    if status >= 500:
        return f"The site had a server error (HTTP {status}). Retry shortly."
    return f"Unexpected response from the site (HTTP {status})."


def _get(client: httpx.Client, url: str, throttle=None) -> tuple[httpx.Response | None, str]:
    """GET without cookies, following redirects manually so login walls can be named."""
    for _ in range(_MAX_REDIRECTS + 1):
        client.cookies.clear()  # never send cookies picked up along the way
        with throttle.turn(url) if throttle is not None else nullcontext():
            resp = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
        if throttle is not None and resp.status_code in (429, 999):
            throttle.pause(url)
        if resp.is_redirect and resp.headers.get("location"):
            url = urljoin(str(resp.url), resp.headers["location"])
            if _is_login_url(url):
                return None, "This link needs a signed-in session (the site redirected to its login page)."
            continue
        return resp, ""
    return None, "Too many redirects."


def _fetch_one(client: httpx.Client, url: str, throttle=None) -> FetchResult:
    resp, reason = _get(client, url, throttle)
    if resp is None:
        return FetchResult(ok=False, text="", reason=reason)
    if resp.status_code != 200:
        return FetchResult(ok=False, text="", reason=_status_reason(resp.status_code))
    page = parse_job_page(resp.text)
    if not page.description:
        reason = (
            "The page asks you to sign in before showing the description."
            if page.auth_wall
            else "Could not find a job description on the page."
        )
        return FetchResult(ok=False, text="", reason=reason, title=page.title, company=page.company)
    return FetchResult(ok=True, text=page.description, reason="", title=page.title, company=page.company,
                       apply_url=page.apply_url, easy_apply=page.easy_apply, offsite_apply=page.offsite_apply)


def fetch_job_url(url: str, client: httpx.Client | None = None, throttle=None) -> FetchResult:
    """Read a job posting. Real requests (no `client` given) go through the polite per-site throttle."""
    own = client is None
    if own:
        from cv_maker.jobs.polite import sites

        client, throttle = httpx.Client(timeout=15.0), throttle or sites
    try:
        target = normalize_job_url(url)
        if throttle is not None and throttle.paused_for(target):
            minutes = max(1, round(throttle.paused_for(target) / 60))
            return FetchResult(ok=False, text="", reason=f"{site_of(target)} asked us to slow down, so the app is leaving it "
                                                         f"alone for about {minutes} more minute{'s' if minutes != 1 else ''}. "
                                                         "Paste the description, or retry later.")
        result = _fetch_one(client, target, throttle)
        job_id = linkedin_job_id(target)
        if not result.ok and job_id and not (throttle is not None and throttle.paused_for(target)):
            # LinkedIn's guest endpoint serves the same description fragment without the page chrome.
            fallback = _fetch_one(client, f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}", throttle)
            if fallback.ok:
                return fallback
        return result
    except httpx.TimeoutException:
        return FetchResult(ok=False, text="", reason="The site took too long to respond (timed out).")
    except httpx.HTTPError as exc:
        return FetchResult(ok=False, text="", reason=f"Could not connect to the site: {exc}")
    finally:
        if own:
            client.close()
