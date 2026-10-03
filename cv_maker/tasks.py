"""Background work: CV parsing, job fetching and the two pipeline phases.

Routes only flip a run/upload into a transient status and queue the work, so every request returns
immediately; pages poll the status API while anything is in flight.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

from cv_maker import improve, prep, research
from cv_maker.apply import details as app_details
from cv_maker.apply import scout
from cv_maker.events import feed
from cv_maker.jobs.batch import apply_fetch_result
from cv_maker.jobs.fetch import FetchResult
from cv_maker.llm.chat import ChatModel, LLMError
from cv_maker.models import CvUpload, JobRun
from cv_maker.pipeline.graph import Pipeline, _prompt_profile, _to_plain, friendly_error
from cv_maker.privacy import PrivateModel, cv_request, fill_missing_contacts
from cv_maker.profile.merge import merge_profile
from cv_maker.profile.parse import parse_cv_profile, profile_summary
from cv_maker.store import Store

logger = logging.getLogger("cv_maker.tasks")


class Runner:
    def __init__(self, sync: bool = False, max_workers: int = 3) -> None:
        self.sync = sync
        self._pool = None if sync else ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cv-worker")

    def submit(self, fn: Callable, *args) -> None:
        if self.sync:
            self._safe(fn, *args)
        else:
            self._pool.submit(self._safe, fn, *args)

    @staticmethod
    def _safe(fn: Callable, *args) -> None:
        try:
            fn(*args)
        except Exception:
            logger.exception("background task %s%s failed", getattr(fn, "__name__", fn), args)


class Tasks:
    def __init__(self, store: Store, pipeline: Pipeline, get_model: Callable[[], ChatModel], fetcher: Callable, runner: Runner,
                 *, automation: Callable[[], dict] | None = None, scanner: Callable | None = None,
                 on_generate: Callable | None = None, researcher: Callable | None = None) -> None:
        self.store = store
        self.pipeline = pipeline
        self.get_model = get_model
        self.fetcher = fetcher
        self.runner = runner
        self.automation = automation or (lambda: {})
        self.scanner = scanner  # (url, application details) -> what the application needs; read-only
        self.on_generate = on_generate  # (run, version): start applying alongside the writing
        self.researcher = researcher  # (company, depth, job_url, force) -> company research (research.py)
        # Application checks drive a browser for up to a minute each: their own worker, so CVs never queue behind them.
        self.scan_runner = Runner(sync=runner.sync, max_workers=1)
        self._busy: dict[str, set[str]] = {}  # job id -> background work under way: research, prep, scan, reviews
        self._busy_lock = threading.Lock()

    def busy(self, run_id: str) -> list[str]:
        """What is being worked on for this job in the background (the job's page shows it and refreshes when done)."""
        with self._busy_lock:
            return sorted(self._busy.get(run_id, ()))

    def _claim(self, run_id: str, kind: str) -> bool:
        """Mark `kind` as under way for this job; False if it already is, so a second click doesn't run it twice."""
        with self._busy_lock:
            kinds = self._busy.setdefault(run_id, set())
            if kind in kinds:
                return False
            kinds.add(kind)
            return True

    def _release(self, run_id: str, kind: str) -> None:
        with self._busy_lock:
            self._busy.get(run_id, set()).discard(kind)

    def _submit(self, runner: Runner, kind: str, fn: Callable, run_id: str, *args) -> bool:
        if not self._claim(run_id, kind):
            return False

        def work() -> None:
            try:
                fn(run_id, *args)
            finally:
                self._release(run_id, kind)

        work.__name__ = getattr(fn, "__name__", "work")
        runner.submit(work)
        return True

    def _allowed(self, key: str) -> bool:
        return str(self.automation().get(key, "0")) == "1"

    # ---- workers ----

    def _parse_upload(self, upload_id: str) -> None:
        upload = self.store.get_upload(upload_id)
        if upload is None:
            return
        with feed.task(f"cv {upload_id[:6]}", f"Reading {upload.filename}"):
            self._parse(upload)

    def _parse(self, upload: CvUpload) -> None:
        upload_id = upload.id
        try:
            # Exactly what the check-before-sending page showed: unused parts left out, chosen details hidden.
            request = cv_request(upload.text, keep_unused=upload.keep_unused, also_hide=upload.also_hide,
                                 send_as_is=upload.send_as_is)
            model = self.get_model()
            if isinstance(model, PrivateModel):
                model = model.with_extra(request["found"], allow=upload.send_as_is)
            parsed = parse_cv_profile(request["text"], model)
            if isinstance(model, PrivateModel):
                fill_missing_contacts(parsed, model.last_mapping)
            if not (parsed.name or parsed.experiences or parsed.skills):
                raise LLMError("The model found no name, roles or skills in the CV text. Retry, or try another model.")
        except Exception as exc:
            upload.status, upload.error = "failed", friendly_error(exc)
            logger.warning("CV %s couldn't be read: %s", upload.id[:6], exc, exc_info=True)
            feed.emit("error", f"✗ couldn't read the CV: {upload.error}")
            feed.set_status("failed")
            self.store.update_upload(upload)
            return
        with self.store.locked():
            if self.store.get_upload(upload_id) is None:
                # Deleted (or "Delete all my data") while the LLM was reading it: keep nothing from it.
                logger.info("CV parse discarded upload=%s: deleted while parsing", upload_id)
                return
            merged = merge_profile(None if upload.replace_profile else self.store.get_profile(), parsed)
            self.store.save_profile(merged)
            upload.status, upload.error, upload.summary = "ready", "", profile_summary(merged)
            self.store.update_upload(upload)
        logger.info("CV parsed upload=%s roles=%d skills=%d", upload.id, len(merged.experiences), len(merged.skills))
        feed.emit("ok", f"✓ profile updated · {len(merged.experiences)} roles · {len(merged.skills)} skills")

    def _fetch_then_analyze(self, run_id: str) -> None:
        run = self.store.find_run(run_id)
        if run is None:
            return
        with feed.task(f"job {run_id[:6]}", f"Adding a job from {urlparse(run.job_url or '').hostname or 'a link'}"):
            self._fetch(run)

    def _fetch(self, run: JobRun) -> None:
        run_id = run.id
        feed.emit("step", f"fetching {urlparse(run.job_url or '').hostname or 'the page'} (no cookies, nothing about you)")
        try:
            result = self.fetcher(run.job_url)
        except Exception as exc:  # never leave a job spinning on "Fetching"
            logger.exception("fetch crashed run=%s", run_id)
            result = FetchResult(ok=False, text="", reason=f"Could not read the page ({type(exc).__name__}). Paste the description or try again.")
        with self.store.locked():
            run = self.store.find_run(run_id)
            if run is None:
                return
            apply_fetch_result(run, result)
            logger.info("Fetched run=%s ok=%s chars=%d reason=%s", run.id, result.ok, len(result.text), result.reason)
            if result.ok:
                feed.emit("ok", f"✓ read {len(result.text):,} characters" + (f" · {run.title}" if run.title else ""))
            else:
                feed.emit("warn", f"! no description: {result.reason}")
            if run.status == "needs_answers":
                run.status, run.step, run.error, run.failed_stage = "analyzing", "Queued for analysis…", "", ""
            else:
                run.step = ""
            self.store.update_run(run)
        if run.status == "analyzing":
            self._analyzed(self.pipeline.analyze(run_id))

    def _analyzed(self, run: JobRun | None) -> None:
        """While you read the questions: research the company (in parallel) and look at what the application itself
        will need (read-only, no tokens)."""
        if run is None or run.status != "needs_answers":
            return
        if self._allowed("RESEARCH_AUTO") and self._depth() != "off" and run.company and self.researcher is not None:
            self._submit(self.runner, "research", self._research_task, run.id, None, False)
        if self._allowed("SCAN_APPLICATIONS") and self.scanner is not None:
            self._submit(self.scan_runner, "scan", self._scan_task, run.id)

    def _depth(self) -> str:
        return str(self.automation().get("RESEARCH_DEPTH", "off"))

    def _scan(self, run_id: str) -> None:
        run = self.store.find_run(run_id)
        if run is None or self.scanner is None:
            return
        url, why = scout.start_url(run)
        checked = datetime.now(timezone.utc).isoformat()
        if not url:
            found = {"checked_at": checked, "note": why}
            feed.emit("step", why)
        else:
            try:
                found = self.scanner(url, app_details.load(self.store)) or {
                    "checked_at": checked, "note": "Checking applications needs the browser engine (see Apply on the job's page)."}
            except Exception as exc:
                logger.warning("job %s: the application check failed: %s", run_id[:6], exc, exc_info=True)
                found = {"checked_at": checked, "start_url": url,
                         "error": f"Couldn't look at the application ({type(exc).__name__}). Check again later, or open it yourself."}
                feed.emit("warn", f"! {found['error']}")
        with self.store.locked():
            run = self.store.find_run(run_id)
            if run is None:
                return
            run.scan = found
            self.store.update_run(run)
        for line in scout.summary(found):
            feed.emit("ok", f"✓ application {line}")

    # ---- queueing (called from requests) ----

    def _mark(self, run: JobRun, status: str, step: str) -> None:
        run.status, run.step, run.error, run.failed_stage = status, step, "", ""
        self.store.update_run(run)

    def queue_parse(self, upload: CvUpload) -> None:
        upload.status, upload.error = "parsing", ""
        self.store.update_upload(upload)
        self.runner.submit(self._parse_upload, upload.id)

    def queue_fetch(self, run: JobRun) -> None:
        self._mark(run, "fetching", "Fetching the job page…")
        self.runner.submit(self._fetch_then_analyze, run.id)

    def queue_analyze(self, run: JobRun) -> None:
        self._mark(run, "analyzing", "Queued for analysis…")
        self.runner.submit(self._analyze, run.id)

    def queue_generate(self, run: JobRun, *, alongside: bool = True) -> int:
        """Start writing the next version; returns its number. With `apply_alongside`, the application starts now too."""
        version = self.store.next_version(run.id)  # the version about to be written, before writing can finish
        self._mark(run, "generating", "Queued for writing…")
        if alongside and run.apply_alongside and self.on_generate is not None:
            self.on_generate(run, version)  # the application starts now and waits for this version's CV
        # Prep notes follow the CV; marked under way now, so the page doesn't offer to write them meanwhile.
        prep_after = self._allowed("PREP_AUTO") and not run.prep.get("notes") and self._claim(run.id, "prep")
        self.runner.submit(self._generate, run.id, prep_after)
        return version

    # Each returns False when that work is already under way for the job.
    def queue_scan(self, run: JobRun) -> bool:
        return self._submit(self.scan_runner, "scan", self._scan_task, run.id)

    def queue_prep(self, run: JobRun) -> bool:
        return self._submit(self.runner, "prep", self._prep_task, run.id)

    def queue_reviews(self, run: JobRun, text: str) -> bool:
        return self._submit(self.runner, "reviews", self._reviews, run.id, text)

    def queue_improve(self, run: JobRun) -> bool:
        """Check the items you picked from a version's ATS check against your profile (one LLM request)."""
        return self._submit(self.runner, "improve", self._improve_task, run.id)

    def queue_research(self, run: JobRun, depth: str) -> bool:
        return self._submit(self.runner, "research", self._research_task, run.id, depth, True)

    def _research_task(self, run_id: str, depth: str | None, force: bool) -> None:
        run = self.store.find_run(run_id)
        if run is None or not run.company or self.researcher is None:
            return
        with feed.task(f"research {run_id[:6]}", f"Researching {run.company}"):
            try:
                self.researcher(run.company, depth or self._depth(), run.job_url or "", force)
            except Exception as exc:
                logger.warning("job %s: company research failed: %s", run_id[:6], exc, exc_info=True)
                feed.emit("error", f"✗ research failed: {friendly_error(exc)}")

    def _company_research(self, run: JobRun) -> dict | None:
        """The research for the prep notes: what's already there, or a quick run now if research runs by itself
        (Settings). Research that fails never stops the notes."""
        if not run.company or self._depth() == "off":
            return None
        found = self.store.get_research(research.company_key(run.company))
        if found is None and self.researcher is not None and self._allowed("RESEARCH_AUTO"):
            try:
                found = self.researcher(run.company, self._depth(), run.job_url or "", False)
            except Exception as exc:
                logger.warning("job %s: company research for the prep notes failed: %s", run.id[:6], exc, exc_info=True)
                feed.emit("warn", f"! company research failed ({friendly_error(exc)}); writing the notes without it")
        return found

    def _analyze(self, run_id: str) -> None:
        with feed.task(f"job {run_id[:6]}", f"Analyzing {self._label(run_id)}"):
            self._analyzed(self.pipeline.analyze(run_id))

    def _generate(self, run_id: str, prep_after: bool = False) -> None:
        try:
            with feed.task(f"job {run_id[:6]}", f"Writing the CV for {self._label(run_id)}"):
                run = self.pipeline.generate(run_id)
            if prep_after and run is not None and run.status == "ready":
                self._prep_task(run_id)
        finally:
            if prep_after:
                self._release(run_id, "prep")

    def _scan_task(self, run_id: str) -> None:
        with feed.task(f"job {run_id[:6]}", f"Checking the application for {self._label(run_id)}"):
            self._scan(run_id)

    def _prep_task(self, run_id: str) -> None:
        with feed.task(f"prep {run_id[:6]}", f"Interview prep for {self._label(run_id)}"):
            self._prep(run_id)

    def _prep(self, run_id: str) -> None:
        run, profile = self.store.find_run(run_id), self.store.get_profile()
        if run is None or profile is None:
            return
        try:
            info = self._company_research(run)
            notes = prep.write_notes(run, _prompt_profile(_to_plain(profile)), self.get_model(), info)
            error = ""
        except Exception as exc:
            notes, error = {}, friendly_error(exc)
            logger.warning("job %s: interview prep failed: %s", run_id[:6], exc, exc_info=True)
            feed.emit("error", f"✗ interview prep failed: {error}")
        with self.store.locked():
            run = self.store.find_run(run_id)
            if run is None:
                return
            run.prep = {**run.prep, "notes": notes or run.prep.get("notes", {}), "error": error,
                        "written_at": datetime.now(timezone.utc).isoformat()}
            self.store.update_run(run)
        if notes:
            feed.emit("ok", f"✓ interview prep: {len(notes['brush_up'])} topics, {len(notes['likely_questions'])} likely questions")

    def _improve_task(self, run_id: str) -> None:
        with feed.task(f"job {run_id[:6]}", f"Checking what can be improved in {self._label(run_id)}"):
            run, profile = self.store.find_run(run_id), self.store.get_profile()
            if run is None or profile is None or (run.improve or {}).get("status") != "checking":
                return
            asked = run.improve
            check = asked.get("check") or {}
            picked = asked.get("picked") or []
            try:
                plan, error = improve.plan(self.get_model(), profile, picked, check, title=run.title, jd_text=run.jd_text), ""
                asks = sum(len(i["questions"]) for i in plan["items"])
                feed.emit("ok", f"✓ checked {len(picked)} item{'s' if len(picked) != 1 else ''}: "
                                f"{asks} question{'s' if asks != 1 else ''} for you")
            except Exception as exc:  # the standard questions still work without the LLM's check
                plan, error = improve.standard_plan(profile, picked, check), friendly_error(exc)
                logger.warning("job %s: checking the improvements failed: %s", run_id[:6], exc, exc_info=True)
                feed.emit("warn", f"! the LLM couldn't check your picks ({error}); asking the standard questions")
            with self.store.locked():
                run = self.store.find_run(run_id)
                if run is None or (run.improve or {}).get("status") != "checking":
                    return  # cancelled meanwhile
                run.improve = {**run.improve, "status": "ready", "plan": plan, "error": error}
                self.store.update_run(run)

    def _reviews(self, run_id: str, text: str) -> None:
        with feed.task(f"prep {run_id[:6]}", f"Summarising reviews for {self._label(run_id)}"):
            run = self.store.find_run(run_id)
            if run is None:
                return
            try:
                summary, error = prep.summarise_reviews(text, run.company, self.get_model()), ""
            except Exception as exc:
                summary, error = None, friendly_error(exc)
                logger.warning("job %s: the review summary failed: %s", run_id[:6], exc, exc_info=True)
                feed.emit("error", f"✗ review summary failed: {error}")
            with self.store.locked():
                run = self.store.find_run(run_id)
                if run is not None:
                    run.prep = {**run.prep, "reviews": summary or run.prep.get("reviews"), "reviews_error": error}
                    self.store.update_run(run)

    def _label(self, run_id: str) -> str:
        run = self.store.find_run(run_id)
        return (run.title or "a pasted job") if run else "a job"
