"""Polite LLM use: a few requests at a time, under a per-minute budget and an optional daily cap, and
waiting out rate limits instead of retrying hard. Providers (free tiers especially) suspend keys that
hammer them; this keeps the app well inside their rules.
"""
import threading
import time
from collections import deque
from datetime import date

from cv_maker.events import feed
from cv_maker.llm.chat import LLMError

_TRANSIENT = {429, 502, 503, 504, 529}
MAX_RETRIES = 3
MAX_WAIT = 120.0  # asked to wait longer than this, a quota is usually used up: stop rather than retry


class Governor:
    def __init__(self, limits, counter=None, sleep=time.sleep, clock=time.monotonic) -> None:
        """`limits()` -> {"rpm", "concurrency", "daily"}; `counter` persists the daily count (get/set)."""
        self._limits = limits
        self._counter = counter
        self._sleep = sleep
        self._clock = clock
        self._cv = threading.Condition()
        self._active = 0
        self._starts: deque = deque()

    def run(self, call):
        limits = self._limits()
        self._enter(limits)
        try:
            return self._with_retries(call, limits)
        finally:
            with self._cv:
                self._active -= 1
                self._cv.notify_all()

    def _enter(self, limits: dict) -> None:
        with self._cv:
            while self._active >= max(1, limits["concurrency"]):
                self._cv.wait(timeout=1)
            self._active += 1
        try:
            self._within_budget(limits)
        except BaseException:
            with self._cv:
                self._active -= 1
                self._cv.notify_all()
            raise

    def _within_budget(self, limits: dict) -> None:
        told = False
        while True:
            with self._cv:
                now = self._clock()
                while self._starts and now - self._starts[0] >= 60:
                    self._starts.popleft()
                rpm = limits["rpm"]
                if not rpm or len(self._starts) < rpm:
                    self._count_today(limits["daily"])
                    self._starts.append(now)
                    return
                wait = 60 - (now - self._starts[0]) + 0.2
            if not told:
                feed.emit("warn", f"… pacing: waiting {wait:.0f}s to stay under {rpm} LLM requests a minute")
                told = True
            self._sleep(min(wait, 5))

    def _count_today(self, cap: int) -> None:
        if self._counter is None:
            return
        today = date.today().isoformat()
        day = self._counter.get() or {}
        count = day.get("count", 0) if day.get("date") == today else 0
        if cap and count >= cap:
            raise LLMError(f"You've reached today's limit of {cap} LLM requests (Settings → Pace and automation). "
                           "It resets tomorrow, or raise it there.")
        self._counter.set({"date": today, "count": count + 1})

    def _with_retries(self, call, limits: dict):
        waited = 0.0
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                self._within_budget(limits)  # a retry is another request: it counts toward the minute and the day
            try:
                return call()
            except LLMError as exc:
                if exc.status in _TRANSIENT and (exc.retry_after or 0) > MAX_WAIT:
                    raise LLMError(f"{exc} (the provider asks to wait {exc.retry_after / 60:.0f} minutes, which usually "
                                   "means a quota is used up, so the app isn't retrying)", status=exc.status) from exc
                if exc.status not in _TRANSIENT or attempt == MAX_RETRIES:
                    if exc.status in _TRANSIENT and waited:
                        raise LLMError(f"{exc} (still refused after waiting {waited:.0f}s; the app stopped "
                                       "retrying so the provider doesn't treat it as abuse)", status=exc.status) from exc
                    raise
                wait = min(MAX_WAIT, exc.retry_after if exc.retry_after is not None else 5.0 * 3 ** attempt)
                feed.emit("warn", f"! the provider is busy or rate-limiting (HTTP {exc.status}); waiting {wait:.0f}s "
                                  f"before trying again ({attempt + 1}/{MAX_RETRIES})")
                self._sleep(wait)
                waited += wait


class GovernedModel:
    """A chat model whose requests go through the governor."""

    def __init__(self, inner, governor: Governor) -> None:
        self.inner = inner
        self._governor = governor

    @property
    def last_usage(self):
        return getattr(self.inner, "last_usage", None)

    def complete(self, messages, *, json_mode: bool = False) -> str:
        return self._governor.run(lambda: self.inner.complete(messages, json_mode=json_mode))
