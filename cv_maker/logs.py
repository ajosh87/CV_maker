"""The log: what the app did, with every warning and error and the job it belongs to, so when something goes wrong
there is a trace to read (Settings → Log). Failures carry their full traceback.

It is kept in your data folder (logs/cv-tailor.log, rotated at 1 MB, four files at most) and never sent anywhere.
Your contact details, the words you never send, email addresses and API keys are masked in it, and your home folder
is written as ~, so a log can go into a bug report as it is.
"""
import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from cv_maker.events import current_task
from cv_maker.paths import make_private_dir, make_private_file
from cv_maker.privacy import redact

FILE = "cv-tailor.log"
MAX_BYTES, BACKUPS = 1_000_000, 3
LEVELS = {"all": None, "warnings": ("WARNING", "ERROR", "CRITICAL"), "errors": ("ERROR", "CRITICAL")}
_KEYS = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|tvly-[A-Za-z0-9_\-]{10,}|gsk_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[^\s\"',]+")
_ENTRY = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\[([^\]]*)\] (.*)$")
_OWN = "_cv_tailor_log"


class Redactor:
    """Masks personal details and secrets in log text. What to mask is looked up again as soon as `version()` changes
    (your details were saved) and otherwise at most every `ttl` seconds."""

    def __init__(self, secrets=lambda: [], keys=lambda: [], version=lambda: 0, ttl: float = 30.0) -> None:
        self._secrets, self._keys, self._version, self._ttl = secrets, keys, version, ttl
        self._known: tuple[list, list] = ([], [])
        self._at, self._seen = float("-inf"), None
        parts = [p for p in re.split(r"[\\/]", str(Path.home())) if p]
        self._home = re.compile(r"[\\/]".join(map(re.escape, parts)), re.IGNORECASE) if len(parts) > 1 else None

    def _current(self) -> tuple[list, list]:
        now, version = time.monotonic(), self._version()
        if version != self._seen or now - self._at >= self._ttl:
            try:
                self._known = (list(self._secrets()), [k for k in self._keys() if k and len(k) >= 8])
                self._at, self._seen = now, version
            except Exception:
                pass  # keep what was known (and try again next time): writing the log must never fail
        return self._known

    def __call__(self, text: str) -> str:
        secrets, keys = self._current()
        if self._home is not None:
            text = self._home.sub("~", text)
        for key in keys:
            text = text.replace(key, "[[KEY]]")
        text = _BEARER.sub(r"\1[[KEY]]", _KEYS.sub("[[KEY]]", text))
        return redact(text, secrets)


def task_tag() -> str:
    """The job or task the current code works for ("job 3f2a1c"), or "app"."""
    task = current_task.get()
    return task.split("#", 1)[0] if task else "app"


class _Formatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__("%(asctime)s %(levelname)-7s [%(task)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        self._redact = redactor

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, "task", ""):
            record.task = task_tag()
        saved = record.msg, record.args, record.exc_text
        try:
            # Redacted here, on the copy that's written; the traceback is formatted (and redacted) afresh.
            record.msg, record.args, record.exc_text = self._redact(record.getMessage()), None, None
            return super().format(record)
        finally:
            record.msg, record.args, record.exc_text = saved

    def formatException(self, ei) -> str:
        return self._redact(super().formatException(ei))


def setup(data_dir: Path, redactor: Redactor) -> Path:
    """Write the app's log (and the nerdbar's lines, and web-server errors) to the data folder."""
    folder = data_dir / "logs"
    make_private_dir(folder)
    path = folder / FILE
    path.touch(exist_ok=True)
    make_private_file(path)
    handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
    handler.setFormatter(_Formatter(redactor))
    handler.addFilter(lambda r: not r.name.startswith("werkzeug") or r.levelno >= logging.WARNING)  # not every request
    setattr(handler, _OWN, True)
    for name in ("cv_maker", "cv_maker.feed", "werkzeug"):
        logger = logging.getLogger(name)
        for old in [h for h in logger.handlers if getattr(h, _OWN, False)]:  # a second app in the same process
            logger.removeHandler(old)
            old.close()
        logger.addHandler(handler)
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)
    return path


def _files(data_dir: Path) -> list[Path]:
    """Oldest first."""
    folder = data_dir / "logs"
    return [p for p in [folder / f"{FILE}.{i}" for i in range(BACKUPS, 0, -1)] + [folder / FILE] if p.exists()]


def read(data_dir: Path, *, level: str = "all", ref: str = "", q: str = "", limit: int = 500) -> dict:
    """Newest first, filtered: {"entries": [{time, level, task, message, details}], "counts": {...}, "total": n}."""
    entries: list[dict] = []
    for path in _files(data_dir):
        current = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _ENTRY.match(line)
            if m:
                current = {"time": m[1], "level": m[2], "task": m[3], "message": m[4], "details": []}
                entries.append(current)
            elif current is not None and line.strip():
                current["details"].append(line)
    ref, q = ref.strip().casefold(), q.strip().casefold()
    if ref:
        entries = [e for e in entries if ref in e["task"].casefold() or ref in e["message"].casefold()]
    if q:
        entries = [e for e in entries if q in (e["message"] + "\n" + "\n".join(e["details"])).casefold()]
    counts = {"all": len(entries), "warnings": sum(e["level"] in LEVELS["warnings"] for e in entries),
              "errors": sum(e["level"] in LEVELS["errors"] for e in entries)}
    if LEVELS.get(level):
        entries = [e for e in entries if e["level"] in LEVELS[level]]
    return {"entries": entries[::-1][:limit], "counts": counts, "total": len(entries)}


def recent_problems(data_dir: Path, hours: int = 24) -> dict:
    """How many warnings and errors in the last `hours` (for Settings)."""
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))
    found = read(data_dir, level="warnings", limit=10_000)["entries"]
    recent = [e for e in found if e["time"] >= since]
    return {"warnings": sum(e["level"] == "WARNING" for e in recent), "errors": sum(e["level"] != "WARNING" for e in recent)}


def text(data_dir: Path) -> str:
    """The whole log, oldest first (for downloading)."""
    return "".join(p.read_text(encoding="utf-8", errors="replace") for p in _files(data_dir))


def clear(data_dir: Path) -> None:
    for path in _files(data_dir):
        if path.name == FILE:
            with open(path, "w", encoding="utf-8"):
                pass  # emptied in place: the log handler keeps writing to it
        else:
            path.unlink(missing_ok=True)
