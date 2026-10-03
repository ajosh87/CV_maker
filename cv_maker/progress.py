"""How far each job has come, for the progress bars: one job's steps, and all jobs together."""
from collections import Counter

STEPS = [("read", "Job read"), ("analysed", "Analysed"), ("answers", "Your answers"), ("written", "CV written"),
         ("applied", "Applied")]
_LIVE_APPLICATION = ("starting", "working", "needs_you")


def _states(run, versions: int, application: str) -> dict:
    """Each step: done, working, you (waiting for you), failed or todo."""
    status = run.status
    order = [key for key, _ in STEPS]
    reached = {"fetching": 0, "needs_paste": 0, "analyzing": 1, "needs_answers": 2, "generating": 3, "ready": 4}.get(status)
    if status == "failed":
        reached = {"analyze": 1, "generate": 3}.get(run.failed_stage, 0 if not (run.jd_text or "").strip() else 1)
    states = {key: ("done" if i < (reached or 0) else "todo") for i, key in enumerate(order)}
    here = order[reached or 0]
    states[here] = {"fetching": "working", "analyzing": "working", "generating": "working", "needs_paste": "you",
                    "needs_answers": "you", "failed": "failed", "ready": "todo"}.get(status, "todo")
    if status == "ready":
        states["applied"] = "done" if application == "submitted" else "working" if application in _LIVE_APPLICATION else "todo"
        if application == "needs_you":
            states["applied"] = "you"
    return states


def job_progress(run, *, versions: int = 0, application: str = "") -> dict:
    """`application`: the latest application's status ("" when there is none)."""
    states = _states(run, versions, application)
    steps = [{"key": key, "label": label, "state": states[key]} for key, label in STEPS]
    done = sum(s["state"] == "done" for s in steps)
    working = any(s["state"] == "working" for s in steps)
    percent = round(100 * (done + (0.5 if working else 0)) / len(steps))
    current = next((s for s in steps if s["state"] != "done"), None)
    if run.status == "failed":
        state = "failed"
    elif any(s["state"] == "you" for s in steps):
        state = "you"
    elif working:
        state = "working"
    elif states["applied"] == "done":
        state = "applied"
    elif run.status == "ready":
        state = "ready"
    else:
        state = "working"
    return {"steps": steps, "done": done, "total": len(steps), "percent": percent, "state": state,
            "current": current["label"] if current else "Done", "number": (steps.index(current) + 1) if current else len(steps)}


def overall(items: list[dict]) -> dict:
    """All jobs together (each item a job_progress): average progress and how many are in each state."""
    counts = Counter(p["state"] for p in items)
    total = len(items)
    return {"jobs": total, "percent": round(sum(p["percent"] for p in items) / total) if total else 0,
            "counts": {key: counts.get(key, 0) for key in ("applied", "ready", "working", "you", "failed")}}


def latest_applications(store) -> dict:
    """job id -> its latest application's status."""
    latest: dict[str, tuple[str, str]] = {}
    for a in store.list_applications():
        if a.run_id not in latest or a.created_at >= latest[a.run_id][0]:
            latest[a.run_id] = (a.created_at, a.status)
    return {run_id: status for run_id, (_, status) in latest.items()}


def all_jobs(store) -> dict:
    """The overall bar for every job that isn't archived."""
    versions = Counter(d.run_id for d in store.list_documents() if d.kind == "cv")
    applications = latest_applications(store)
    submitted = {a.run_id for a in store.list_applications() if a.status == "submitted"}
    return overall([job_progress(r, versions=versions.get(r.id, 0),
                                 application="submitted" if r.id in submitted else applications.get(r.id, ""))
                    for r in store.list_runs() if not r.archived])


def _ago(iso: str) -> str:
    from datetime import datetime, timezone
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    days = (datetime.now(timezone.utc) - (then if then.tzinfo else then.replace(tzinfo=timezone.utc))).days
    return "today" if days < 1 else "yesterday" if days == 1 else f"{days} days ago"


def section_states(run, *, versions: list[dict], gaps: list[dict], applications: list, busy: list[str],
                   research: dict | None, research_depth: str) -> dict:
    """Each part of a job's page and where it stands: {"tone": ok | busy | attn | bad | idle, "label": ...}. Shown on
    each part's header, in the page's own menu and in the job list's overview, so every part says how it's doing."""
    out = {}
    if gaps:
        strong = sum(g.get("strength") == "strong" for g in gaps)
        must = [g for g in gaps if g.get("required")] or gaps
        shown = sum(g.get("strength") in ("strong", "partial") for g in must) / len(must)
        out["match"] = {"tone": "ok" if shown >= 0.75 else "attn" if shown >= 0.5 else "bad",
                        "label": f"{strong} of {len(gaps)} strong"}
    latest = versions[0] if versions else None
    if run.status == "generating":
        out["versions"] = {"tone": "busy", "label": f"Writing v{(latest['version'] if latest else 0) + 1}…"}
    elif latest is None:
        out["versions"] = {"tone": "attn" if run.status == "needs_answers" else "idle",
                           "label": "Answer the questions first" if run.status == "needs_answers" else "None yet"}
    else:
        score = (latest.get("ats") or {}).get("score")
        out["versions"] = {"tone": "ok" if score is None or score >= 70 else "attn" if score >= 50 else "bad",
                           "label": f"v{latest['version']}" + (f" · ATS {score}" if score is not None else "")}
    last = applications[-1] if applications else None
    scan = run.scan or {}
    if any(a.status == "submitted" for a in applications):
        out["applying"] = {"tone": "ok", "label": "Applied"}
    elif last is not None and last.status == "needs_you":
        out["applying"] = {"tone": "attn", "label": "The assistant needs you"}
    elif last is not None and last.status in ("starting", "working"):
        out["applying"] = {"tone": "busy", "label": "The assistant is applying"}
    elif "scan" in busy:
        out["applying"] = {"tone": "busy", "label": "Checking the application…"}
    elif run.easy_apply:
        out["applying"] = {"tone": "idle" if latest is None else "attn", "label": "Easy Apply, on LinkedIn"}
    elif latest is None:
        out["applying"] = {"tone": "idle", "label": "After the CV"}
    elif scan.get("error"):
        out["applying"] = {"tone": "attn", "label": "Ready · the check failed"}
    elif scan.get("account"):
        out["applying"] = {"tone": "ok", "label": "Ready · needs " + ("an account" if scan["account"].get("creating") else "a sign-in")}
    else:
        out["applying"] = {"tone": "ok", "label": "Ready to apply"}
    prep = run.prep or {}
    if "prep" in busy:
        out["prep"] = {"tone": "busy", "label": "After the CV" if run.status == "generating" else "Writing…"}
    elif prep.get("notes"):
        out["prep"] = {"tone": "ok", "label": "Ready"}
    elif prep.get("error"):
        out["prep"] = {"tone": "bad", "label": "Failed"}
    else:
        out["prep"] = {"tone": "idle", "label": "Not written"}
    if "research" in busy or "reviews" in busy:
        out["research"] = {"tone": "busy", "label": "Researching…" if "research" in busy else "Summarising reviews…"}
    elif (research or {}).get("researched_at"):
        out["research"] = {"tone": "ok", "label": f"{(research.get('depth') or 'simple').capitalize()} · {_ago(research['researched_at'])}"}
    elif not run.company:
        out["research"] = {"tone": "idle", "label": "Company not known"}
    else:
        out["research"] = {"tone": "idle", "label": "Off" if research_depth == "off" else "Not run"}
    answered = [a for a in run.answers or [] if not a.get("skipped")]
    out["posting"] = {"tone": "idle", "label": f"{len(answered)} answer{'' if len(answered) == 1 else 's'}" if run.answers else
                      f"{len(run.jd_text or ''):,} characters"}
    return out

