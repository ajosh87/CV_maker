"""What the app is doing right now: a small live feed of events and task timers, shown in the nerdbar.

The feed lives in memory and is never sent anywhere. Messages are written with placeholders, never personal
details, so it is safe to leave open while sharing your screen. Each line also goes to the log in your data folder
(see logs.py), so what happened can still be read after a restart.
"""
import contextvars
import itertools
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager

# The task the current code is working for, so LLM calls and steps are attributed to the right job.
current_task: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_task", default=None)

KINDS = ("task", "step", "llm", "privacy", "ok", "warn", "error", "apply", "user", "info")
_log = logging.getLogger("cv_maker.feed")
_log.propagate = False  # to the log file only (logs.setup attaches it): the console stays as quiet as before
_LEVELS = {"error": logging.ERROR, "warn": logging.WARNING}


class Feed:
    def __init__(self, size: int = 800) -> None:
        self._events: deque = deque(maxlen=size)
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._tasks: dict[str, dict] = {}
        self.started = time.time()
        self._totals = {"llm_calls": 0, "tokens_in": 0, "tokens_out": 0, "estimated": False, "llm_seconds": 0.0,
                        "chars_sent": 0, "hidden": 0}

    # ---- events ----

    def emit(self, kind: str, text: str, *, task: str | None = None, **extra) -> dict:
        task = task or current_task.get()
        with self._lock:
            tag = self._tasks[task]["tag"] if task in self._tasks else ""
            event = {"seq": next(self._seq), "t": time.time(), "kind": kind if kind in KINDS else "info", "text": text,
                     "task": task, "tag": tag, **extra}
            self._events.append(event)
        _log.log(_LEVELS.get(event["kind"], logging.INFO), text, extra={"task": tag or "app"})
        return event

    def since(self, seq: int = 0, limit: int = 300) -> list[dict]:
        with self._lock:
            events = [e for e in self._events if e["seq"] > seq]
        return events[-limit:]

    def last_seq(self) -> int:
        with self._lock:
            return self._events[-1]["seq"] if self._events else 0

    # ---- tasks ----

    @contextmanager
    def task(self, tag: str, label: str):
        """Time a piece of work and attribute everything inside it (steps, LLM calls) to it.

        `tag` is the short name shown on each log line, e.g. "job 3f2a1c"."""
        task_id = f"{tag}#{next(self._seq)}"
        info = {"id": task_id, "tag": tag, "label": label, "started": time.time(), "ended": None, "status": "running",
                "llm_calls": 0, "tokens_in": 0, "tokens_out": 0, "estimated": False, "llm_seconds": 0.0}
        with self._lock:
            self._tasks[task_id] = info
            self._trim_tasks()
        token = current_task.set(task_id)
        self.emit("task", f"▶ {label}")
        try:
            yield info
        except Exception as exc:
            info["status"] = "failed"
            self.emit("error", f"✗ {label}: {type(exc).__name__}")
            raise
        finally:
            info["ended"] = time.time()
            if info["status"] == "running":
                info["status"] = "done"
            self.emit("task", f"■ {label} · {info['ended'] - info['started']:.1f}s"
                              + (f" · {info['tokens_in'] + info['tokens_out']:,} tokens" if info["llm_calls"] else ""))
            current_task.reset(token)

    def set_status(self, status: str, task_id: str | None = None) -> None:
        task_id = task_id or current_task.get()
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["status"] = status

    def _trim_tasks(self) -> None:
        finished = sorted((t for t in self._tasks.values() if t["ended"]), key=lambda t: t["ended"])
        for old in finished[:-20]:
            self._tasks.pop(old["id"], None)

    def record_llm(self, *, tokens_in: int, tokens_out: int, seconds: float, estimated: bool, chars_sent: int,
                   hidden: int) -> None:
        task_id = current_task.get()
        with self._lock:
            for bucket in [self._totals] + ([self._tasks[task_id]] if task_id in self._tasks else []):
                bucket["llm_calls"] += 1
                bucket["tokens_in"] += tokens_in
                bucket["tokens_out"] += tokens_out
                bucket["llm_seconds"] += seconds
                bucket["estimated"] = bucket["estimated"] or estimated
            self._totals["chars_sent"] += chars_sent
            self._totals["hidden"] += hidden

    def stats(self) -> dict:
        now = time.time()
        with self._lock:
            tasks = [dict(t) for t in self._tasks.values()]
            totals = dict(self._totals)
        for t in tasks:
            t["elapsed"] = round((t["ended"] or now) - t["started"], 1)
        active = sorted((t for t in tasks if not t["ended"]), key=lambda t: t["started"])
        recent = sorted((t for t in tasks if t["ended"]), key=lambda t: t["ended"], reverse=True)[:6]
        totals["llm_seconds"] = round(totals["llm_seconds"], 1)
        totals["uptime"] = round(now - self.started)
        return {"now": now, "active": active, "recent": recent, "session": totals}


def estimate_tokens(text: str) -> int:
    """Rough count when a provider doesn't report usage: about four characters per token."""
    return max(1, round(len(text) / 4))


feed = Feed()
