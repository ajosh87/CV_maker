"""Being a polite visitor to job sites: one request at a time per site, a pause between requests, and backing
off for a while when a site says to slow down. This is about load and good manners: the app doesn't hide that
it's automated; it simply never behaves like a crawler.
"""
import threading
import time
from contextlib import contextmanager

from cv_maker.events import feed
from cv_maker.jobs.urls import site_of

# How polite to be (Settings → Pace and automation). Every number the app uses for pacing lives here.
LEVELS = {
    "gentle": {"label": "Gentle", "between": 4.0, "linkedin": 10.0, "refusal_pause": 900.0, "field_pause_ms": 700,
               "page_pause_ms": 2500},
    "standard": {"label": "Standard", "between": 2.0, "linkedin": 6.0, "refusal_pause": 300.0, "field_pause_ms": 350,
                 "page_pause_ms": 1200},
    "brisk": {"label": "Brisk", "between": 1.0, "linkedin": 4.0, "refusal_pause": 120.0, "field_pause_ms": 150,
              "page_pause_ms": 600},
}
_chosen = lambda: "standard"  # noqa: E731  (replaced by configure() with the user's setting)


def configure(get_level) -> None:
    global _chosen
    _chosen = get_level


def level() -> dict:
    return LEVELS.get(str(_chosen() or "standard"), LEVELS["standard"])


class SiteThrottle:
    def __init__(self, intervals: dict | None = None, default: float | None = None, sleep=time.sleep) -> None:
        """Explicit `intervals` / `default` (seconds) are for tests; otherwise the chosen politeness level decides."""
        self._intervals = intervals
        self._default = default
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}
        self._paused: dict[str, float] = {}
        self._busy: dict[str, threading.Lock] = {}

    def _interval(self, site: str) -> float:
        if self._intervals is not None:
            return self._intervals.get(site, self._default or 0.0)
        chosen = level()
        return chosen["linkedin"] if site == "linkedin.com" else chosen["between"]

    def wait(self, url: str) -> None:
        """Block until this site may be contacted again."""
        site = site_of(url)
        interval = self._interval(site)
        with self._lock:
            now = time.monotonic()
            turn = max(now, self._next.get(site, 0.0))
            self._next[site] = turn + interval
        if turn - now > 0.5:
            feed.emit("step", f"waiting {turn - now:.0f}s before the next request to {site} (one at a time, politely)")
        if turn > now:
            self._sleep(turn - now)

    @contextmanager
    def turn(self, url: str):
        """Wait for this site's turn and keep it until the response is in, so requests to one site never overlap."""
        with self._lock:
            busy = self._busy.setdefault(site_of(url), threading.Lock())
        with busy:
            self.wait(url)
            yield

    def paused_for(self, url: str) -> float:
        with self._lock:
            return max(0.0, self._paused.get(site_of(url), 0.0) - time.monotonic())

    def pause(self, url: str, seconds: float | None = None) -> None:
        seconds = level()["refusal_pause"] if seconds is None else seconds
        site = site_of(url)
        with self._lock:
            self._paused[site] = time.monotonic() + seconds
        feed.emit("warn", f"! {site} asked us to slow down; leaving it alone for {seconds / 60:.0f} minutes")


sites = SiteThrottle()
