"""Search, filter, sort and grouping for the Library pages (documents and jobs).

Data volumes are small (one local user), so everything is done in Python over full listings.
"""
from collections import Counter
from dataclasses import dataclass

from cv_maker.models import ACTIVE_STATUSES, JobRun
from cv_maker.progress import job_progress, latest_applications
from cv_maker.store import Store

DOC_KINDS = [("all", "All"), ("cv", "Tailored CVs"), ("letter", "Cover letters"), ("source", "Source CVs")]
DOC_SORTS = [("newest", "Newest first"), ("oldest", "Oldest first"), ("company", "Company A–Z"), ("title", "Job title A–Z")]
DOC_GROUPS = [("none", "No grouping"), ("job", "Job"), ("company", "Company"), ("kind", "Type")]

JOB_BUCKETS = [("all", "All"), ("needs", "Needs you"), ("working", "Working"), ("ready", "Ready"), ("archived", "Archived")]
JOB_SORTS = [("updated", "Recently updated"), ("newest", "Newest first"), ("oldest", "Oldest first"),
             ("company", "Company A–Z"), ("title", "Job title A–Z"), ("status", "Status")]
_BUCKET_STATUSES = {"ready": {"ready"}, "needs": {"needs_paste", "needs_answers", "failed"}, "working": set(ACTIVE_STATUSES)}
_STATUS_ORDER = ["failed", "needs_paste", "needs_answers", "fetching", "analyzing", "generating", "ready"]
KIND_LABELS = {"cv": "Tailored CV", "letter": "Cover letter", "source": "Source CV"}


def _pick(value: str, options: list[tuple[str, str]], default: str) -> str:
    return value if value in {k for k, _ in options} else default


def job_label(run: JobRun) -> str:
    return run.title or ("Pasted job" if not run.job_url else run.job_url)


@dataclass
class DocRow:
    kind: str
    id: str
    created_at: str
    title: str
    company: str
    run_id: str = ""
    version: int = 0
    is_latest: bool = True
    job_url: str | None = None
    filename: str = ""
    status: str = ""
    meta: dict | None = None

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]


def _matches(q: str, *fields: str | None) -> bool:
    if not q:
        return True
    hay = " ".join(f for f in fields if f).lower()
    return all(word in hay for word in q.lower().split())


def _sort_key(sort: str):
    if sort == "company":
        return lambda r: ((r.company or "~").lower(), (r.title or "").lower(), r.created_at)
    if sort == "title":
        return lambda r: ((r.title or "~").lower(), r.created_at)
    return lambda r: r.created_at


def document_rows(store: Store, *, kind: str = "all", q: str = "", company: str = "", job: str = "",
                  latest: bool = False, include_archived: bool = False, sort: str = "newest", group: str = "none") -> dict:
    kind = _pick(kind, DOC_KINDS, "all")
    sort = _pick(sort, DOC_SORTS, "newest")
    group = _pick(group, DOC_GROUPS, "none")
    runs = {r.id: r for r in store.list_runs()}

    docs = store.list_documents()
    newest = {}
    for d in docs:
        key = (d.run_id, d.kind)
        newest[key] = max(newest.get(key, 0), d.version)

    rows: list[DocRow] = []
    for d in docs:
        run = runs.get(d.run_id)
        if run is not None and run.archived and not include_archived:
            continue
        rows.append(DocRow(
            kind=d.kind, id=d.id, created_at=d.created_at, run_id=d.run_id, version=d.version,
            is_latest=d.version == newest[(d.run_id, d.kind)],
            title=(run.title if run else "") or d.title or ("Pasted job" if not d.job_url else d.job_url),
            company=(run.company if run else "") or d.company, job_url=d.job_url, meta=d.meta,
        ))
    for u in store.list_uploads():
        if u.status == "failed" and not u.summary:
            continue  # nothing useful was read from it
        rows.append(DocRow(kind="source", id=u.id, created_at=u.created_at, title=u.filename, company="",
                           filename=u.filename, status=u.status, meta=u.summary))

    # Facets reflect every filter except the category itself.
    scoped = [
        r for r in rows
        if _matches(q, r.title, r.company, r.filename)
        and (not company or r.company == company)
        and (not job or r.run_id == job)
        and (not latest or r.is_latest)
    ]
    counts = Counter(r.kind for r in scoped)
    counts["all"] = len(scoped)
    selected = [r for r in scoped if kind == "all" or r.kind == kind]
    selected.sort(key=_sort_key(sort), reverse=sort == "newest")

    return {
        "rows": selected,
        "groups": _group(selected, group, runs),
        "counts": counts,
        "companies": sorted({r.company for r in rows if r.company}, key=str.lower),
        "job": runs.get(job) if job else None,
        "kind": kind, "sort": sort, "group": group,
    }


def _group(rows: list[DocRow], group: str, runs: dict) -> list[tuple[str, str, list[DocRow]]]:
    """Returns [(label, link_kind, rows)] in first-appearance order so the chosen sort still applies."""
    if group == "none":
        return [("", "", rows)]
    buckets: dict[str, tuple[str, str, list[DocRow]]] = {}
    for r in rows:
        if group == "kind":
            key, label, link = r.kind, KIND_LABELS[r.kind] + "s", ""
        elif group == "company":
            key = r.company or "~none"
            label, link = (r.company or "No company"), ("company" if r.company else "")
        else:  # job
            key = r.run_id or "~source"
            label = f"{r.title}" + (f" · {r.company}" if r.company else "") if r.run_id else "Source CVs"
            link = "job" if r.run_id else ""
        buckets.setdefault(key, (label, link, []))[2].append(r)
    return list(buckets.values())


def job_rows(store: Store, *, bucket: str = "all", q: str = "", company: str = "", sort: str = "updated") -> dict:
    bucket = _pick(bucket, JOB_BUCKETS, "all")
    sort = _pick(sort, JOB_SORTS, "updated")
    runs = store.list_runs()
    cvs = [d for d in store.list_documents() if d.kind == "cv"]
    versions = Counter(d.run_id for d in cvs)
    newest: dict = {}
    for d in cvs:  # the latest version's ATS score, for the list
        if d.version >= newest.get(d.run_id, (0, None))[0]:
            newest[d.run_id] = (d.version, ((d.meta or {}).get("ats") or {}).get("score"))
    applied = {a.run_id for a in store.list_applications() if a.status == "submitted"}
    latest = latest_applications(store)

    def in_bucket(run: JobRun, name: str) -> bool:
        if name == "archived":
            return run.archived
        if run.archived:
            return False
        return name == "all" or run.status in _BUCKET_STATUSES[name]

    scoped = [r for r in runs if _matches(q, r.title, r.company, r.job_url) and (not company or r.company == company)]
    counts = {name: sum(in_bucket(r, name) for r in scoped) for name, _ in JOB_BUCKETS}
    selected = [r for r in scoped if in_bucket(r, bucket)]

    keys = {
        "updated": (lambda r: r.updated_at, True),
        "newest": (lambda r: r.created_at, True),
        "oldest": (lambda r: r.created_at, False),
        "company": (lambda r: ((r.company or "~").lower(), job_label(r).lower()), False),
        "title": (lambda r: (job_label(r).lower(), r.created_at), False),
        "status": (lambda r: (_STATUS_ORDER.index(r.status) if r.status in _STATUS_ORDER else 99, r.updated_at), False),
    }
    key, reverse = keys[sort]
    selected.sort(key=key, reverse=reverse)
    return {
        "rows": [{"run": r, "label": job_label(r), "versions": versions.get(r.id, 0), "applied": r.id in applied,
                  "ats": newest.get(r.id, (0, None))[1],
                  "progress": job_progress(r, versions=versions.get(r.id, 0),
                                           application="submitted" if r.id in applied else latest.get(r.id, ""))}
                 for r in selected],
        "counts": counts,
        "companies": sorted({r.company for r in runs if r.company}, key=str.lower),
        "bucket": bucket, "sort": sort,
    }
