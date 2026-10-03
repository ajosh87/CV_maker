import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cv_maker.models import ACTIVE_STATUSES, Application, CvUpload, Document, Education, Experience, JobRun, Profile, Skill


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile_to_dict(profile: Profile) -> dict:
    return asdict(profile)


def _profile_from_dict(data: dict) -> Profile:
    return Profile(
        name=data.get("name", ""),
        email=data.get("email", ""),
        phone=data.get("phone", ""),
        location=data.get("location", ""),
        links=data.get("links") or {},
        target_role=data.get("target_role"),
        experiences=[Experience(**exp) for exp in data.get("experiences", [])],
        education=[Education(**edu) for edu in data.get("education", [])],
        skills=[Skill(**skill) for skill in data.get("skills", [])],
        extras=data.get("extras") or {},
    )


def _known(cls, data: dict) -> dict:
    # Rows written by older versions may lack newer fields (they have defaults) or carry dropped ones.
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def _run_to_dict(run: JobRun) -> dict:
    return asdict(run)


def _run_from_dict(data: dict) -> JobRun:
    return JobRun(**_known(JobRun, data))


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Rows are whole JSON documents, so any read-modify-write must hold this lock to avoid lost updates
        # between the worker threads and request handlers.
        self._lock = threading.RLock()
        self.revision = 0  # bumped when your details or the never-send list change (the log's masking follows it)
        self._init_db()

    def locked(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit or roll back
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS documents_run ON documents (run_id);
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS applications (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research (
                    company TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_cache (
                    key TEXT PRIMARY KEY,
                    created REAL NOT NULL,
                    purpose TEXT NOT NULL,
                    reply TEXT NOT NULL
                );
                """
            )

    def get_profile(self) -> Profile | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM profile WHERE id = 1").fetchone()
        if row is None:
            return None
        return _profile_from_dict(json.loads(row["data"]))

    def save_profile(self, profile: Profile) -> None:
        payload = json.dumps(_profile_to_dict(profile))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO profile (id, data) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET data = excluded.data
                """,
                (payload,),
            )
        self.revision += 1

    def create_run(self, job_url: str | None = None, id: str | None = None, status: str = "needs_paste") -> JobRun:
        now = _utc_now()
        run = JobRun(
            id=id or uuid4().hex,
            created_at=now,
            updated_at=now,
            status=status,
            job_url=job_url,
            jd_text="",
            fetch_ok=False,
            fetch_reason="",
            requirements={},
            gaps=[],
            questions=[],
            answers=[],
            generate_letter=False,
            source_cv_path="",
            output_cv_path="",
            letter_path="",
            draft={},
        )
        self._insert_run(run)
        return run

    def _insert_run(self, run: JobRun) -> None:
        payload = json.dumps(_run_to_dict(run))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (id, data) VALUES (?, ?)",
                (run.id, payload),
            )

    def get_run(self, run_id: str) -> JobRun:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _run_from_dict(json.loads(row["data"]))

    def find_run(self, run_id: str) -> JobRun | None:
        try:
            return self.get_run(run_id)
        except KeyError:
            return None

    def update_run(self, run: JobRun) -> None:
        run.updated_at = _utc_now()
        payload = json.dumps(_run_to_dict(run))
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET data = ? WHERE id = ?",
                (payload, run.id),
            )

    def list_runs(self) -> list[JobRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM runs ORDER BY json_extract(data, '$.created_at')"
            ).fetchall()
        return [_run_from_dict(json.loads(row["data"])) for row in rows]

    def create_upload(self, filename: str, path: str, text: str, replace_profile: bool = False, status: str = "parsing") -> CvUpload:
        now = _utc_now()
        upload = CvUpload(
            id=uuid4().hex,
            created_at=now,
            updated_at=now,
            filename=filename,
            path=path,
            text=text,
            replace_profile=replace_profile,
            status=status,
        )
        with self._connect() as conn:
            conn.execute("INSERT INTO uploads (id, data) VALUES (?, ?)", (upload.id, json.dumps(asdict(upload))))
        return upload

    def get_upload(self, upload_id: str) -> CvUpload | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        if row is None:
            return None
        return CvUpload(**_known(CvUpload, json.loads(row["data"])))

    def update_upload(self, upload: CvUpload) -> None:
        upload.updated_at = _utc_now()
        with self._connect() as conn:
            conn.execute("UPDATE uploads SET data = ? WHERE id = ?", (json.dumps(asdict(upload)), upload.id))

    def list_uploads(self) -> list[CvUpload]:
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM uploads ORDER BY json_extract(data, '$.created_at') DESC").fetchall()
        return [CvUpload(**_known(CvUpload, json.loads(row["data"]))) for row in rows]

    # ---- generated documents (versioned, append-only) ----

    def next_version(self, run_id: str) -> int:
        """Never reuses a number, even after the newest version was deleted (run.last_version remembers it)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(json_extract(data, '$.version')) AS v FROM documents WHERE run_id = ?", (run_id,)
            ).fetchone()
        run = self.find_run(run_id)
        return max(int(row["v"] or 0), run.last_version if run else 0) + 1

    def add_document(self, run: JobRun, kind: str, version: int, path: str, meta: dict | None = None) -> Document:
        doc = Document(
            id=uuid4().hex,
            created_at=_utc_now(),
            run_id=run.id,
            kind=kind,
            version=version,
            path=path,
            title=run.title,
            company=run.company,
            job_url=run.job_url,
            meta=meta or {},
        )
        with self._connect() as conn:
            conn.execute("INSERT INTO documents (id, run_id, data) VALUES (?, ?, ?)", (doc.id, run.id, json.dumps(asdict(doc))))
        return doc

    def get_document(self, doc_id: str) -> Document | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return Document(**_known(Document, json.loads(row["data"]))) if row else None

    def list_documents(self, run_id: str | None = None) -> list[Document]:
        query = "SELECT data FROM documents"
        args: tuple = ()
        if run_id is not None:
            query += " WHERE run_id = ?"
            args = (run_id,)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY json_extract(data, '$.created_at')", args).fetchall()
        return [Document(**_known(Document, json.loads(row["data"]))) for row in rows]

    def backfill_documents(self) -> int:
        """Register files generated before versioning existed as version 1."""
        with self._connect() as conn:
            have = {row["run_id"] for row in conn.execute("SELECT DISTINCT run_id FROM documents")}
        added = 0
        for run in self.list_runs():
            if run.id in have or run.status != "ready" or not run.output_cv_path or not Path(run.output_cv_path).exists():
                continue
            self.add_document(run, "cv", 1, run.output_cv_path, {"answers": run.answers, "backfilled": True})
            if run.letter_path and Path(run.letter_path).exists():
                self.add_document(run, "letter", 1, run.letter_path, {"backfilled": True})
            added += 1
        return added

    def delete_document(self, doc_id: str) -> Document | None:
        with self._lock:
            doc = self.get_document(doc_id)
            if doc is not None:
                with self._connect() as conn:
                    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        return doc

    def delete_run(self, run_id: str) -> list[str]:
        """Remove a job and its documents; returns the file paths that belonged to it."""
        with self._lock:
            paths = [d.path for d in self.list_documents(run_id)]
            run = self.find_run(run_id)
            if run is not None:
                paths += [p for p in (run.output_cv_path, run.letter_path) if p]
            with self._connect() as conn:
                conn.execute("DELETE FROM documents WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM applications WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return sorted(set(paths))

    def delete_upload(self, upload_id: str) -> CvUpload | None:
        with self._lock:
            upload = self.get_upload(upload_id)
            if upload is not None:
                with self._connect() as conn:
                    conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
        return upload

    def delete_everything(self) -> None:
        with self._lock, self._connect() as conn:
            for table in ("documents", "runs", "uploads", "profile", "preferences", "llm_log", "applications", "llm_cache",
                          "research"):
                conn.execute(f"DELETE FROM {table}")
        self.revision += 1

    # ---- privacy: words never sent, and a record of what was sent ----

    def get_preference(self, key: str):
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM preferences WHERE key = ?", (key,)).fetchone()
        return json.loads(row["data"]) if row else None

    def set_preference(self, key: str, value) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO preferences (key, data) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET data = excluded.data",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_never_send(self) -> list[str]:
        return self.get_preference("never_send") or []

    def set_never_send(self, terms: list[str]) -> None:
        self.set_preference("never_send", terms)
        self.revision += 1

    # ---- supervised applications (see cv_maker.apply) ----

    def create_application(self, run: JobRun, version: int, start_url: str, show_window: bool = False) -> Application:
        now = _utc_now()
        application = Application(id=uuid4().hex, run_id=run.id, created_at=now, updated_at=now, version=version,
                                  start_url=start_url, show_window=show_window)
        with self._connect() as conn:
            conn.execute("INSERT INTO applications (id, run_id, data) VALUES (?, ?, ?)",
                         (application.id, run.id, json.dumps(asdict(application), ensure_ascii=False)))
        return application

    def get_application(self, application_id: str) -> Application | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM applications WHERE id = ?", (application_id,)).fetchone()
        return Application(**_known(Application, json.loads(row["data"]))) if row else None

    def update_application(self, application: Application) -> None:
        application.updated_at = _utc_now()
        with self._connect() as conn:
            conn.execute("UPDATE applications SET data = ? WHERE id = ?",
                         (json.dumps(asdict(application), ensure_ascii=False), application.id))

    def delete_application(self, application_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM applications WHERE id = ?", (application_id,))

    def list_applications(self, run_id: str | None = None) -> list[Application]:
        query, args = "SELECT data FROM applications", ()
        if run_id is not None:
            query, args = query + " WHERE run_id = ?", (run_id,)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY json_extract(data, '$.created_at')", args).fetchall()
        return [Application(**_known(Application, json.loads(row["data"]))) for row in rows]

    LLM_LOG_SIZE = 50

    def log_llm_call(self, purpose: str, text: str, hidden: list[str], usage: dict | None = None) -> None:
        entry = {"created_at": _utc_now(), "purpose": purpose, "text": text, "hidden": hidden, **(usage or {})}
        with self._connect() as conn:
            conn.execute("INSERT INTO llm_log (data) VALUES (?)", (json.dumps(entry, ensure_ascii=False),))
            conn.execute("DELETE FROM llm_log WHERE id <= (SELECT MAX(id) FROM llm_log) - ?", (self.LLM_LOG_SIZE,))

    def list_llm_calls(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM llm_log ORDER BY id DESC").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def clear_llm_log(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM llm_log")

    # ---- company research, shared by every job at the same company ----

    def get_research(self, company_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM research WHERE company = ?", (company_key,)).fetchone()
        return json.loads(row["data"]) if row else None

    def save_research(self, company_key: str, data: dict) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO research (company, data) VALUES (?, ?)",
                         (company_key, json.dumps(data, ensure_ascii=False)))

    # ---- earlier LLM answers to identical read-only requests (masked text only) ----

    CACHE_DAYS = 14

    def llm_cache_get(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT reply FROM llm_cache WHERE key = ? AND created > ?",
                               (key, time.time() - self.CACHE_DAYS * 86400)).fetchone()
        return row["reply"] if row else None

    def llm_cache_put(self, key: str, purpose: str, reply: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO llm_cache (key, created, purpose, reply) VALUES (?, ?, ?, ?)",
                         (key, time.time(), purpose, reply))
            conn.execute("DELETE FROM llm_cache WHERE created <= ?", (time.time() - self.CACHE_DAYS * 86400,))

    def clear_llm_cache(self) -> int:
        with self._connect() as conn:
            return conn.execute("DELETE FROM llm_cache").rowcount

    def set_archived(self, run_ids: list[str], archived: bool) -> int:
        with self._lock:
            return self._set_archived(run_ids, archived)

    def _set_archived(self, run_ids: list[str], archived: bool) -> int:
        count = 0
        for run_id in run_ids:
            run = self.find_run(run_id)
            if run is not None and run.archived != archived:
                run.archived = archived
                self.update_run(run)
                count += 1
        return count

    def reset_interrupted(self) -> int:
        """Background work does not survive a restart: mark in-flight items as retryable failures."""
        count = 0
        for run in self.list_runs():
            if run.status not in ACTIVE_STATUSES:
                continue
            if run.status == "fetching":
                run.status = "needs_paste"
                run.fetch_reason = "Fetch was interrupted by a server restart."
            else:
                run.failed_stage = "analyze" if run.status == "analyzing" else "generate"
                run.status = "failed"
                run.error = "Interrupted by a server restart. Click Retry."
            run.step = ""
            self.update_run(run)
            count += 1
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM uploads").fetchall()
        for row in rows:
            upload = CvUpload(**_known(CvUpload, json.loads(row["data"])))
            if upload.status == "parsing":
                upload.status = "failed"
                upload.error = "Interrupted by a server restart. Click Retry."
                self.update_upload(upload)
                count += 1
        for application in self.list_applications():
            if application.status in ("starting", "working", "needs_you"):
                application.status, application.waiting = "stopped", {}
                application.error = "The app was restarted. Resume to continue from the last page."
                self.update_application(application)
                count += 1
        return count
