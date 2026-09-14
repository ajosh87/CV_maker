import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cv_maker.models import Education, Experience, JobRun, Profile, Skill


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile_to_dict(profile: Profile) -> dict:
    return asdict(profile)


def _profile_from_dict(data: dict) -> Profile:
    return Profile(
        name=data["name"],
        email=data["email"],
        phone=data["phone"],
        location=data["location"],
        links=data["links"],
        target_role=data["target_role"],
        experiences=[Experience(**exp) for exp in data["experiences"]],
        education=[Education(**edu) for edu in data["education"]],
        skills=[Skill(**skill) for skill in data["skills"]],
        extras=data["extras"],
    )


def _run_to_dict(run: JobRun) -> dict:
    return asdict(run)


def _run_from_dict(data: dict) -> JobRun:
    return JobRun(**data)


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

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

    def create_run(self, job_url: str | None = None) -> JobRun:
        now = _utc_now()
        run = JobRun(
            id=uuid4().hex,
            created_at=now,
            updated_at=now,
            status="needs_paste",
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
