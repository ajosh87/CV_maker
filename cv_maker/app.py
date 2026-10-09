import importlib.util
import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename

from cv_maker import logs as log_file
from cv_maker import ats, improve, prep, progress, research, validate
from cv_maker import skills as sk
from cv_maker.apply import accounts as apply_accounts
from cv_maker.apply import details as app_details
from cv_maker.apply import scout
from cv_maker.apply.session import VIEWPORT, ApplySession, Sessions
from cv_maker.llm.limits import Governor, GovernedModel
from cv_maker.events import feed
from cv_maker.export.archive import build_export
from cv_maker.jobs import polite
from cv_maker.jobs.fetch import fetch_job_url
from cv_maker.jobs.match import match_profile, ranked
from cv_maker.jobs.requirements import JobRequirements
from cv_maker.jobs.urls import parse_url_lines
from cv_maker.library import DOC_GROUPS, DOC_KINDS, DOC_SORTS, JOB_BUCKETS, JOB_SORTS, document_rows, job_label, job_rows
from cv_maker.llm.chat import LLMError, ProviderUnsetError, check_connection, get_chat_model
from cv_maker.models import ACTIVE_STATUSES, JobRun
from cv_maker.paths import make_private_dir, resolve_data_dir, user_data_dir, warn_if_committable
from cv_maker.pipeline.graph import Pipeline
from cv_maker.progress import job_progress
from cv_maker.privacy import KIND_LABELS, PLACEHOLDER_TEXT, PrivateModel, cv_request, describe_hidden, identity_secrets
from cv_maker.profile.answers import LEVELS as ANSWER_LEVELS
from cv_maker.profile.answers import answers_from_dicts, levels_of
from cv_maker.profile.edit import EXTRA_LISTS, links_text, profile_from_form
from cv_maker.profile.extract_text import SUPPORTED_SUFFIXES, UnreadableCvError, extract_text
from cv_maker.security import install_local_only_guard
from cv_maker.settings import (SETTING_KEYS, automation_settings, effective_settings, get_secret_key, llm_limits,
                               save_automation, save_settings, save_tavily_key, tavily_key)
from cv_maker.store import Store, profile_signature
from cv_maker.tasks import Runner, Tasks

logger = logging.getLogger("cv_maker")

STATUS_LABELS = {
    "fetching": "Fetching",
    "needs_paste": "Needs description",
    "analyzing": "Analyzing",
    "needs_answers": "Needs answers",
    "generating": "Writing",
    "ready": "Ready",
    "failed": "Failed",
}

# Appearance: Auto follows the system's light or dark setting.
THEMES = {"auto": ("Auto", "◐", "Follows your system's light or dark setting."), "light": ("Light", "☀", "Light everywhere."),
          "dark": ("Dark", "☾", "Dark everywhere.")}
_NEXT_THEME = {"auto": "light", "light": "dark", "dark": "auto"}

# Top navigation: endpoint -> section it belongs to.
NAV = [("jobs", "Jobs"), ("documents", "Documents"), ("profile", "Profile"), ("settings", "Settings")]
_SECTION = {
    "jobs": "jobs", "paste": "jobs", "run_detail": "jobs", "run_questions": "jobs",
    "documents": "documents",
    "profile": "profile", "profile_edit": "profile", "upload": "profile", "review_upload": "profile",
    "settings": "settings", "sent_log": "settings", "apply_details_page": "settings", "log_page": "settings",
    "apply_start": "jobs", "application_page": "jobs",
}


def _when(iso: str) -> str:
    """UTC ISO timestamp -> local '2 Oct 2026, 22:49'."""
    try:
        moment = datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        return iso or ""
    return f"{moment.day} {moment:%b %Y, %H:%M}"


def _mark_terms(text: str, terms: list[str]) -> Markup:
    """The posting with each term (under any of its names) marked; `data-q` says which question it belongs to."""
    found = sorted(((start, end, i) for i, term in enumerate(terms) for start, end in sk.spans(term, text or "")),
                   key=lambda span: (span[0], span[0] - span[1]))
    out, pos = [], 0
    for start, end, i in found:
        if start < pos:  # inside a longer mark already
            continue
        out += [escape(text[pos:start]), Markup('<mark class="q-mark" data-q="%d">%s</mark>') % (i, text[start:end])]
        pos = end
    out.append(escape((text or "")[pos:]))
    return Markup("").join(out)


def _llm_configured(values: dict) -> bool:
    provider = (values.get("LLM_PROVIDER") or "").lower()
    return bool(provider) and (provider in ("ollama", "bedrock") or bool(values.get("LLM_API_KEY")))


_REAL = object()  # "use the real thing": the default for the browser look and the Wikipedia lookup


def create_app(data_dir: Path | None = None, *, model_factory=None, fetcher=None, sync_jobs: bool = False,
               scanner=_REAL, researcher=_REAL) -> Flask:
    """`model_factory`, `fetcher`, `scanner` and `researcher` replace the LLM, job-page fetching, the read-only
    look at applications and company research (tests). A custom fetcher means no network: the look and the
    research are then off unless given too."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    app = Flask(__name__)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.filters["when"] = _when
    app.jinja_env.filters["hidden_summary"] = describe_hidden
    app.jinja_env.filters["mark_terms"] = _mark_terms
    app.jinja_env.globals["job_label"] = job_label
    app.jinja_env.globals["ats_weights"] = ats.WEIGHTS
    app.jinja_env.filters["placeholders"] = lambda text: Markup(
        PLACEHOLDER_TEXT.sub(lambda m: f'<mark class="ph">{m.group()}</mark>', str(escape(text))))

    data_dir, data_source = resolve_data_dir(data_dir)
    uploads_dir = data_dir / "uploads"
    output_dir = data_dir / "output"
    for folder in (data_dir, uploads_dir, output_dir):
        make_private_dir(folder)
    app.secret_key = get_secret_key(data_dir)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["MAX_CONTENT_LENGTH"] = validate.UPLOAD_MAX_MB * 1024 * 1024
    app.data_dir = data_dir
    install_local_only_guard(app)

    store = Store(data_dir / "cv_maker.sqlite")
    app.store = store

    def hidden_everywhere():
        return identity_secrets(store.get_profile(), store.get_never_send())

    # The log (Settings → Log): every step, warning and error with its job, masked like requests to the LLM.
    log_path = log_file.setup(data_dir, log_file.Redactor(
        secrets=hidden_everywhere, keys=lambda: [effective_settings(data_dir)[0].get("LLM_API_KEY") or "", tavily_key(data_dir)[0]],
        version=lambda: store.revision))
    logger.info("Personal data folder: %s (%s)", data_dir.resolve(), data_source)
    if data_source == "legacy":
        logger.info("Using ./data inside the project folder because it already holds your data. "
                    "Set DATA_DIR or move it to %s to keep personal data out of the project folder.", user_data_dir())
    warn_if_committable(data_dir)
    interrupted = store.reset_interrupted()
    if interrupted:
        logger.warning("Marked %d item(s) interrupted by the last shutdown as retryable", interrupted)
    if store.backfill_documents():
        logger.info("Registered previously generated files as version 1")

    # Every LLM request: under the pace limits (Settings → Pace and automation), masked, recorded, and answered from
    # earlier identical read-only requests when possible.
    daily = SimpleNamespace(get=lambda: store.get_preference("llm_daily"), set=lambda v: store.set_preference("llm_daily", v))
    governor = Governor(lambda: llm_limits(data_dir), counter=daily)
    cache = SimpleNamespace(get=store.llm_cache_get, put=store.llm_cache_put)

    def get_model():
        # Resolved per call, so Settings changes apply without a restart.
        inner = model_factory() if model_factory is not None else get_chat_model(effective_settings(data_dir)[0])
        return PrivateModel(GovernedModel(inner, governor), hidden_everywhere, store.log_llm_call, cache=cache,
                            scope=describe_model())

    def fetch(url: str):
        return (fetcher or fetch_job_url)(url)

    def describe_model() -> str:
        if model_factory is not None:
            return "custom model"
        values, _ = effective_settings(data_dir)
        return f"{values.get('LLM_PROVIDER')} · {values.get('LLM_MODEL')}"

    def engine_ready() -> bool:
        return importlib.util.find_spec("playwright") is not None

    if scanner is _REAL:
        scanner = None if fetcher is not None else (lambda url, details: scout.look(url, details) if engine_ready() else None)
    if researcher is _REAL:
        def researcher(company, depth, job_url, force, context=None):
            sources = research.chosen_sources(automation_settings(data_dir)["RESEARCH_SOURCES"])
            return research.research(company, depth=depth, sources=sources, store=store, get_model=get_model,
                                     job_url=job_url, force=force, tavily_key=tavily_key(data_dir)[0], context=context)
        researcher = None if fetcher is not None else researcher
    polite.configure(lambda: automation_settings(data_dir)["POLITENESS"])

    pipeline = Pipeline(store, get_model, output_dir, describe_model)
    tasks = Tasks(store, pipeline, get_model, fetch, Runner(sync=sync_jobs), automation=lambda: automation_settings(data_dir),
                  scanner=scanner, on_generate=lambda run, version: start_alongside(run, version), researcher=researcher)
    app.tasks = tasks
    sessions = Sessions()  # the apply assistant's browsers (one at a time)
    app.apply_sessions = sessions
    browser_dir = data_dir / "browser"  # the assistant's own browser profile: cookies and sign-ins for job sites

    # ---- helpers ----

    def batch_runs() -> list[JobRun]:
        return [r for r in (store.find_run(rid) for rid in session.get("run_ids", [])) if r is not None]

    def get_run_or_404(run_id: str) -> JobRun:
        run = store.find_run(run_id)
        if run is None:
            abort(404)
        return run

    def current_upload():
        upload_id = session.get("upload_id")
        return store.get_upload(upload_id) if upload_id else None

    def wants_json() -> bool:
        return request.headers.get("X-Requested-With") == "XMLHttpRequest"

    _PARTS = {"status": "status", "before": "Applying", "prep": "Interview prep", "research": "Company research"}

    def note(section: str, message: str, error: bool = False) -> None:
        """A message for one part of a job's page, shown in that part (where you pressed the button), not at the top."""
        flash(message, f"{'error' if error else 'message'}|{section}")
        if error:
            run_id = (request.view_args or {}).get("run_id", "")
            logger.warning("job %s · %s: %s", run_id[:6] or "?", _PARTS.get(section, section), message)

    def safe_next(default: str) -> str:
        target = request.form.get("next") or request.args.get("next") or ""
        # Browsers treat "\" like "/", so "/\evil.com" would leave the site; reject it and control characters.
        if "\\" in target or any(ord(c) < 32 for c in target):
            return default
        parsed = urlparse(target)
        return target if target.startswith("/") and not target.startswith("//") and not parsed.netloc else default

    def remove_files(paths) -> int:
        """Delete files, but only ones inside this user's data folder."""
        root = data_dir.resolve()
        removed = 0
        for p in paths:
            if not p:
                continue
            path = Path(p).resolve()
            if root in path.parents and path.is_file():
                path.unlink()
                removed += 1
        return removed

    def run_json(run: JobRun) -> dict:
        return {
            "id": run.id,
            "status": run.status,
            "label": STATUS_LABELS.get(run.status, run.status),
            "step": run.step,
            "error": run.error,
            "fetch_ok": run.fetch_ok,
            "fetch_reason": run.fetch_reason,
            "jd_text": run.jd_text,
            "version": run.updated_at,
        }

    @app.context_processor
    def inject_globals():
        if "llm_configured" not in g:  # once per request, not once per rendered template
            g.llm_configured = model_factory is not None or _llm_configured(effective_settings(data_dir)[0])
            theme = store.get_preference("theme")
            g.theme = theme if theme in THEMES else "auto"
        return {
            "status_labels": STATUS_LABELS,
            "nav": NAV,
            "section": _SECTION.get(request.endpoint or "", ""),
            "llm_configured": g.llm_configured,
            "theme": g.theme, "themes": THEMES, "next_theme": _NEXT_THEME[g.theme],
        }

    @app.template_global()
    def came_from(default_url: str, default_label: str) -> tuple[str, str]:
        """Where a page's "back" goes: the page you came from (a job, an application...) when the link said so."""
        target = safe_next("")
        if not target:
            return default_url, default_label
        path = urlparse(target).path
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "runs":
            run = store.find_run(parts[1])
            return (target, run.title or "the job") if run else (default_url, default_label)
        if len(parts) == 2 and parts[0] == "applications":
            return target, "the application"
        return target, {"settings": "Settings", "jobs": "Jobs", "profile": "Profile", "documents": "Documents"}.get(parts[0], "Back")

    @app.template_global()
    def url_with(**changes) -> str:
        """Current page URL with some query args changed (None/"" removes one); used by filters."""
        args = request.args.to_dict()
        for key, value in changes.items():
            if value in (None, ""):
                args.pop(key, None)
            else:
                args[key] = value
        return url_for(request.endpoint, **(request.view_args or {}), **args)

    @app.errorhandler(404)
    def not_found(_):
        return render_template("message.html", title="Not found", message="That page or job does not exist."), 404

    @app.errorhandler(413)
    def too_large(_):
        flash(f"That's larger than {validate.UPLOAD_MAX_MB} MB. A CV is usually well under 1 MB: save it as .docx or a "
              "text-based PDF.", "error")
        return redirect(url_for("profile"))

    @app.errorhandler(500)
    def server_error(_):
        # Flask has already written the traceback to the log.
        return render_template("message.html", title="Something went wrong",
                               message="The app hit an unexpected error. Nothing you saved was lost. The log has the "
                                       "details: what happened, where, and the traceback.",
                               link=(url_for("log_page", level="errors"), "Open the log")), 500

    @app.route("/favicon.ico")
    def favicon():
        return redirect(url_for("static", filename="logo.svg"))

    @app.route("/api/events")
    def api_events():
        """The nerdbar's live feed: what ran, for how long, and how many tokens it used (in memory only)."""
        return jsonify({"events": feed.since(request.args.get("after", default=0, type=int)), "stats": feed.stats()})

    # ---- home & old addresses ----

    @app.route("/", methods=["GET"])
    def home():
        return redirect(url_for("jobs" if store.get_profile() else "profile"))

    @app.route("/result")
    @app.route("/questions")
    def old_results():
        pending = [r for r in batch_runs() if r.status == "needs_answers"]
        if request.path == "/questions" and pending:
            return redirect(url_for("run_questions", run_id=pending[0].id))
        return redirect(url_for("jobs"))

    @app.route("/library")
    def old_library():
        return redirect(url_for("documents", **request.args.to_dict()))

    @app.route("/library/jobs")
    def old_library_jobs():
        args = request.args.to_dict()
        args["bucket"] = {"action": "needs", "progress": "working"}.get(args.get("bucket", ""), args.get("bucket", ""))
        return redirect(url_for("jobs", **{k: v for k, v in args.items() if v}))

    # ---- profile ----

    def render_profile(status: int = 200):
        saved = store.get_profile()
        return render_template("profile.html", upload=current_upload(), profile=saved,
                               uploads=store.list_uploads(), extra_lists=EXTRA_LISTS, stale=stale_runs()), status

    @app.route("/", methods=["POST"], endpoint="upload_root")
    @app.route("/profile/upload", methods=["POST"])
    def upload():
        file = request.files.get("cv")
        if not file or not file.filename:
            flash("Choose a CV file first.", "error")
            return render_profile(400)
        suffix = Path(file.filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            flash(f"Upload a PDF or DOCX file (got {suffix or 'no extension'}). In Word: File → Save As → .docx.", "error")
            return render_profile(400)
        safe_name = secure_filename(file.filename) or f"cv{suffix}"
        path = uploads_dir / f"{uuid4().hex}_{safe_name}"
        file.save(path)
        try:
            text = extract_text(path)
        except UnreadableCvError as exc:
            path.unlink(missing_ok=True)
            flash(f"Could not read text from that file ({exc}). If it is a scanned PDF, export a text-based PDF or DOCX.", "error")
            return render_profile(400)
        if len(text.strip()) < validate.CV_MIN:
            path.unlink(missing_ok=True)
            flash(f"Only {len(text.strip())} characters of text were found in {file.filename}. If it's a scanned PDF or an "
                  "image, export a text-based PDF or a .docx.", "error")
            return render_profile(400)
        if len(text) > 30_000:
            flash(f"Your CV is long ({len(text):,} characters); the first 30,000 are read into your profile.")
        record = store.create_upload(file.filename, str(path), text, replace_profile=request.form.get("mode") == "replace",
                                     status="review")
        session["upload_id"] = record.id
        logger.info("CV uploaded upload=%s file=%s chars=%d", record.id, path.name, len(text))
        return redirect(url_for("review_upload", upload_id=record.id))

    def review_data(upload_rec):
        return cv_request(upload_rec.text, keep_unused=upload_rec.keep_unused, also_hide=upload_rec.also_hide,
                          send_as_is=upload_rec.send_as_is, base=hidden_everywhere())

    def llm_is_local() -> bool:
        values, _ = effective_settings(data_dir)
        host = urlparse(values.get("OLLAMA_HOST") or "http://localhost:11434").hostname
        return model_factory is None and values.get("LLM_PROVIDER") == "ollama" and host in ("localhost", "127.0.0.1", "::1")

    @app.route("/uploads/<upload_id>/review", methods=["GET", "POST"])
    def review_upload(upload_id: str):
        """Check before sending: what will be hidden from the LLM, and what the LLM will receive."""
        upload_rec = store.get_upload(upload_id)
        if upload_rec is None:
            abort(404)
        if upload_rec.status not in ("review", "failed"):
            flash(f"{upload_rec.filename} is being read." if upload_rec.status == "parsing" else f"{upload_rec.filename} was already read.")
            return redirect(url_for("profile"))
        if request.method == "GET":
            return render_template("review.html", upload=upload_rec, req=review_data(upload_rec), kind_labels=KIND_LABELS,
                                   model_name=describe_model(), local_llm=llm_is_local())
        with store.locked():
            upload_rec = store.get_upload(upload_id)
            if upload_rec is None:
                abort(404)
            if request.form.get("items_shown"):
                # Indexes refer to the list as it was shown, so read it before applying other changes.
                items = review_data(upload_rec)["items"]
                ticked = set(request.form.getlist("hide"))
                upload_rec.send_as_is = [it["text"] for i, it in enumerate(items) if str(i) not in ticked]
                unticked = {t.casefold() for t in upload_rec.send_as_is}
                upload_rec.also_hide = [t for t in upload_rec.also_hide if t.casefold() not in unticked]
            added, problems = validate.terms(request.form.get("also", ""), upload_rec.also_hide)
            for problem in problems:
                flash(problem, "error")
            for term in added[len(upload_rec.also_hide):]:
                upload_rec.also_hide.append(term)
                upload_rec.send_as_is = [t for t in upload_rec.send_as_is if t.casefold() != term.casefold()]
            if request.form.get("unused_shown"):
                upload_rec.keep_unused = not request.form.get("leave_out")
            store.update_upload(upload_rec)
        if request.form.get("action") == "send":
            tasks.queue_parse(upload_rec)
            return redirect(url_for("profile"))
        return redirect(url_for("review_upload", upload_id=upload_id) + "#preview")

    def latest_source():
        """The most recent CV you uploaded that was read, for reference while you edit."""
        return next((u for u in sorted(store.list_uploads(), key=lambda u: u.created_at, reverse=True) if u.text), None)

    @app.route("/profile")
    def profile():
        return render_profile()

    @app.route("/profile/edit", methods=["GET", "POST"])
    def profile_edit():
        saved = store.get_profile()
        if request.method == "POST":
            draft = profile_from_form(request.form, store.get_profile())
            problems = validate.profile(draft)
            if problems:
                for problem in problems:
                    flash(problem, "error")
                return render_template("profile_edit.html", profile=draft, links_text=links_text(draft.links),
                                       extra_lists=EXTRA_LISTS, source=latest_source()), 400
            with store.locked():  # don't overwrite a confirmation a running job saved meanwhile
                store.save_profile(profile_from_form(request.form, store.get_profile()))
            waiting = len(stale_runs())
            flash("Profile saved." + (f" {waiting} job{'s were' if waiting != 1 else ' was'} written from your earlier profile: "
                                      "write new versions from the Profile page or each job." if waiting else
                                      " New CVs will use these facts; existing versions are unchanged."))
            return redirect(url_for("profile"))
        return render_template("profile_edit.html", profile=saved, links_text=links_text(saved.links) if saved else "",
                               extra_lists=EXTRA_LISTS, source=latest_source())

    @app.route("/profile/retry", methods=["POST"])
    def profile_retry():
        upload_rec = current_upload()
        if upload_rec is not None and upload_rec.status == "failed":  # not yet sent: that goes through the check page
            tasks.queue_parse(upload_rec)
        return redirect(url_for("profile"))

    @app.route("/uploads/<upload_id>/download")
    def download_upload(upload_id: str):
        upload_rec = store.get_upload(upload_id)
        if upload_rec is None:
            abort(404)
        if not Path(upload_rec.path).exists():
            flash("The original file is no longer on disk.", "error")
            return redirect(url_for("profile"))
        return send_file(Path(upload_rec.path).absolute(), as_attachment=True, download_name=secure_filename(upload_rec.filename) or "cv")

    @app.route("/uploads/<upload_id>/delete", methods=["POST"])
    def delete_upload(upload_id: str):
        upload_rec = store.delete_upload(upload_id)
        if upload_rec is None:
            abort(404)
        remove_files([upload_rec.path])
        if session.get("upload_id") == upload_id:
            session.pop("upload_id")
        flash(f"Deleted {upload_rec.filename}. Facts already in your profile stay until you edit them.")
        return redirect(url_for("profile"))

    @app.route("/api/upload/<upload_id>")
    def api_upload(upload_id: str):
        upload_rec = store.get_upload(upload_id)
        if upload_rec is None:
            abort(404)
        return jsonify({"status": upload_rec.status, "error": upload_rec.error, "version": upload_rec.updated_at})

    # ---- jobs ----

    @app.route("/jobs", methods=["GET", "POST"])
    def jobs():
        has_profile = store.get_profile() is not None
        if request.method == "POST":
            if not has_profile:
                flash("Add your CV on the Profile page first.", "error")
                return redirect(url_for("profile"))
            urls, invalid = parse_url_lines(request.form.get("urls", ""))
            urls, links_note = validate.links(urls)
            pasted = request.form.get("pasted_jd", "").strip()
            pasted_title = request.form.get("pasted_title", "").strip()[:200]
            if invalid:
                shown = ", ".join(invalid[:3]) + ("…" if len(invalid) > 3 else "")
                flash(f"Ignored {len(invalid)} line(s) that are not http(s) links: {shown}", "error")
            if not urls and not pasted:
                flash("Add at least one job link, or paste a job description.", "error")
                return render_jobs(form=request.form, status=400, open_add=True)
            jd_note = ""
            if pasted:
                problems, jd_note = validate.job_description(pasted)
                if problems:
                    for problem in problems:
                        flash(problem, "error")
                    return render_jobs(form=request.form, status=400, open_add=True)
                pasted = pasted[:validate.JD_MAX]
            runs = []
            for url in urls:
                run = store.create_run(job_url=url, status="fetching")
                tasks.queue_fetch(run)
                runs.append(run)
            if pasted:
                run = store.create_run(job_url=None, status="analyzing")
                run.jd_text, run.title, run.fetch_reason = pasted, pasted_title, ""
                store.update_run(run)
                tasks.queue_analyze(run)
                runs.append(run)
            session["run_ids"] = [r.id for r in runs]
            logger.info("Jobs added runs=%d", len(runs))
            flash(f"Added {len(runs)} job{'s' if len(runs) != 1 else ''}. Progress updates below.")
            for extra in (links_note, jd_note):
                if extra:
                    flash(extra)
            return redirect(url_for("jobs"))
        return render_jobs()

    def render_jobs(form=None, status: int = 200, open_add: bool = False):
        args = request.args
        data = job_rows(store, bucket=args.get("bucket", "all"), q=args.get("q", "").strip(),
                        company=args.get("company", ""), sort=args.get("sort", "updated"))
        for row in data["rows"]:
            row["busy"] = tasks.busy(row["run"].id)
        has_profile = store.get_profile() is not None
        any_jobs = any(data["counts"].values())
        return render_template(
            "jobs.html", data=data, args=args, buckets=JOB_BUCKETS, sorts=JOB_SORTS, form=form or {}, overall=progress.all_jobs(store),
            has_profile=has_profile, any_jobs=any_jobs, open_add=open_add or (has_profile and not any_jobs),
            upload=current_upload(),
        ), status

    @app.route("/jobs/bulk", methods=["POST"])
    @app.route("/library/jobs/archive", methods=["POST"])
    def jobs_bulk():
        ids = request.form.getlist("run_id")
        action = request.form.get("action", "archive")
        if not ids:
            flash("Select at least one job first.", "error")
        elif action == "delete":
            paths = []
            for run_id in ids:
                paths += store.delete_run(run_id)
            remove_files(paths)
            flash(f"Deleted {len(ids)} job{'s' if len(ids) != 1 else ''} and their files.")
        else:
            archive = action != "unarchive"
            changed = store.set_archived(ids, archive)
            flash(f"{'Archived' if archive else 'Restored'} {changed} job{'s' if changed != 1 else ''}.")
        return redirect(safe_next(url_for("jobs")))

    @app.route("/jobs/paste", methods=["GET", "POST"])
    def paste():
        paste_runs = [r for r in store.list_runs() if r.status == "needs_paste" and not r.archived]
        if request.method == "POST":
            updated = 0
            for run in paste_runs:
                text = request.form.get(f"jd_{run.id}", "").strip()
                if text:
                    problems, _ = validate.job_description(text)
                    if problems:
                        flash(f"{run.title or run.job_url or 'A job'}: {problems[0]}", "error")
                        continue
                    run.jd_text = text[:validate.JD_MAX]
                    tasks.queue_analyze(run)
                    updated += 1
            if not updated:
                flash("Nothing was pasted.", "error")
                return redirect(url_for("paste"))
            return redirect(url_for("jobs"))
        if not paste_runs:
            flash("No jobs need a pasted description.")
            return redirect(url_for("jobs"))
        return render_template("paste.html", runs=paste_runs)

    @app.route("/api/runs")
    def api_runs():
        ids = [i for i in request.args.get("ids", "").split(",") if i]
        rows, active = {}, False
        for run in (store.find_run(i) for i in ids):
            if run is None:
                continue
            active = active or run.status in ACTIVE_STATUSES
            cvs = [d for d in store.list_documents(run.id) if d.kind == "cv"]  # only the polled jobs
            versions = len(cvs)
            latest_ats = ((max(cvs, key=lambda d: d.version).meta or {}).get("ats") or {}).get("score") if cvs else None
            applications = store.list_applications(run.id)
            applied = any(a.status == "submitted" for a in applications)
            shown = job_progress(run, versions=versions,
                                 application="submitted" if applied else (applications[-1].status if applications else ""))
            html = render_template("_job_row.html", row={"run": run, "label": job_label(run), "versions": versions,
                                                          "applied": applied, "progress": shown, "busy": tasks.busy(run.id),
                                                          "ats": latest_ats})
            rows[run.id] = {**run_json(run), "busy": tasks.busy(run.id), "progress": shown, "html": html}
        return jsonify({"active": active, "runs": rows, "overall": progress.all_jobs(store) if ids else None})

    # ---- one job ----

    def _can_answer(run: JobRun) -> bool:
        # needs_answers: first pass. ready / failed-at-generate: answers can be changed for a new version.
        return run.status in ("needs_answers", "ready") or (run.status == "failed" and run.failed_stage == "generate")

    def job_gaps(run: JobRun) -> list[dict]:
        """Each requirement with its evidence, scored against your profile as it is now, strongest first."""
        profile_now = store.get_profile()
        if run.requirements and profile_now is not None:
            result = match_profile(profile_now, JobRequirements.from_dict(run.requirements), levels_of(answers_from_dicts(run.answers)),
                                   run.assessment)
            return ranked([g.to_dict() for g in result.gaps])
        return ranked([{"strength": {"covered": "strong", "partial": "partial"}.get(g.get("status"), "none"), "score": 0, "found": [],
                        "required": True, **g} for g in run.gaps or []])

    def version_entries(run: JobRun, gaps: list[dict]) -> list[dict]:
        """Every version of a job's documents, newest first, with what its preview, ATS check and fact check need."""
        versions = {}
        for d in store.list_documents(run.id):
            entry = versions.setdefault(d.version, {"version": d.version, "created_at": d.created_at, "cv": None, "letter": None, "yes": [],
                                                    "draft": {}, "ats": None, "changes": [], "rewrites": [], "letter_text": "", "model": "",
                                                    "improved_from": None, "improve_items": [], "tailoring": {},
                                                    "profile_sig": ""})
            entry[d.kind] = d
            meta = d.meta or {}
            check = meta.get("fact_check") or {}
            entry["changes"] = entry["changes"] + (check.get("items") or [])
            if d.kind == "cv":
                entry["yes"] = [a.term for a in answers_from_dicts(meta.get("answers", [])) if a.is_yes]
                entry.update(draft=meta.get("draft") or {}, ats=meta.get("ats") or None, model=meta.get("model", ""),
                             rewrites=check.get("rewrites") or [], improved_from=meta.get("improved_from"),
                             improve_items=(meta.get("improve") or {}).get("items") or [],
                             tailoring=meta.get("tailoring") or {}, profile_sig=meta.get("profile_sig", ""))
            else:
                entry["letter_text"] = meta.get("text", "")
        for entry in versions.values():  # versions written before the ATS check: checked now, against the same posting
            if entry["ats"] is None and entry["draft"] and run.requirements:
                entry["ats"] = ats.check(entry["draft"], ats.plan(run.requirements, gaps), run.title, run.company)
            if entry["ats"] and "file" not in entry["ats"] and entry["cv"] and Path(entry["cv"].path).exists():
                entry["ats"] = {**entry["ats"], "file": ats.read_back(entry["cv"].path, entry["draft"], entry["ats"])}
            entry["improvable"] = improve.improvable(entry["ats"]) if entry["ats"] else []
        for entry in versions.values():
            base = versions.get(entry["improved_from"]) if entry["improved_from"] else None
            entry["compare"] = improve.compare(base["ats"], entry["ats"]) if base and base["ats"] and entry["ats"] else {}
            entry["outcomes"] = improve.outcomes(entry["improve_items"], entry["ats"]) if entry["improve_items"] else []
        return sorted(versions.values(), key=lambda v: v["version"], reverse=True)

    def written_before_change(run: JobRun, profile_sig: str | None = None) -> int | None:
        """The latest version's number when it was written from an older profile than yours now (else None).
        Versions written before this was recorded say nothing either way."""
        if run.status != "ready" or run.archived:
            return None
        latest = max((d for d in store.list_documents(run.id) if d.kind == "cv"), key=lambda d: d.version, default=None)
        sig = (latest.meta or {}).get("profile_sig", "") if latest else ""
        now = profile_sig if profile_sig is not None else profile_signature(store.get_profile())
        return latest.version if sig and now and sig != now else None

    def stale_runs() -> list[tuple[JobRun, int]]:
        now = profile_signature(store.get_profile())
        return [(r, v) for r in store.list_runs() if (v := written_before_change(r, now)) is not None]

    def job_states(run: JobRun, versions: list[dict], gaps: list[dict], applications: list) -> dict:
        found = store.get_research(research.company_key(run.company)) if run.company else None
        return progress.section_states(run, versions=versions, gaps=gaps, applications=applications, busy=tasks.busy(run.id),
                                       research=found, research_depth=automation_settings(data_dir)["RESEARCH_DEPTH"])

    @app.route("/runs/<run_id>")
    def run_detail(run_id: str):
        run = get_run_or_404(run_id)
        gaps = job_gaps(run)
        versions = version_entries(run, gaps)
        answers = []
        for a in answers_from_dicts(run.answers):
            if a.skipped:
                shown = "Skipped"
            elif a.is_yes:
                head = a.level.capitalize() if a.level else "Yes"
                shown = f"{head} — {a.detail}" if a.detail else head
            else:
                shown = "No experience"
            answers.append((a.term, shown))
        applications = store.list_applications(run.id)
        submitted = any(a.status == "submitted" for a in applications)
        shown = job_progress(run, versions=len(versions),
                             application="submitted" if submitted else (applications[-1].status if applications else ""))
        return render_template(
            "run_detail.html",
            run=run,
            versions=versions,
            states=job_states(run, versions, gaps, applications),
            answers=answers,
            gaps=gaps,
            can_answer=_can_answer(run),
            applications=applications,
            progress=shown,
            start=scout.start_url(run),
            can_scan=tasks.scanner is not None,
            research=store.get_research(research.company_key(run.company)) if run.company else None,
            research_links=research.links(run.company, research.chosen_sources(automation_settings(data_dir)["RESEARCH_SOURCES"]))
            if run.company else [],
            research_depth=automation_settings(data_dir)["RESEARCH_DEPTH"],
            can_research=tasks.researcher is not None,
            reviews_limit=prep.REVIEWS_LIMIT,
            busy=tasks.busy(run.id),
            saved_sites={a["site"] for a in apply_accounts.list_accounts(store) if a.get("in_keychain")},
            linkedin_offsite=scout.linkedin_offsite(run),
            stale_version=written_before_change(run),
            next_version=store.next_version(run.id),
        )

    @app.route("/runs/<run_id>/peek")
    def run_peek(run_id: str):
        """One job at a glance, for the right half of the job list: where it is, what's next, how each part stands."""
        run = get_run_or_404(run_id)
        gaps = job_gaps(run)
        versions = version_entries(run, gaps)
        applications = store.list_applications(run.id)
        submitted = any(a.status == "submitted" for a in applications)
        shown = job_progress(run, versions=len(versions),
                             application="submitted" if submitted else (applications[-1].status if applications else ""))
        return render_template("_job_peek.html", run=run, gaps=gaps, versions=versions, progress=shown,
                               states=job_states(run, versions, gaps, applications), applications=applications,
                               busy=tasks.busy(run.id))

    @app.route("/runs/<run_id>/paste", methods=["POST"])
    def paste_one(run_id: str):
        run = get_run_or_404(run_id)
        text = request.form.get("jd_text", "").strip()
        problems, jd_note = validate.job_description(text)
        if run.status in ACTIVE_STATUSES:
            note("status", "That job is still being processed.", error=True)
        elif not text:
            note("status", "Paste the job description first.", error=True)
        elif problems:
            note("status", problems[0], error=True)
        else:
            run.jd_text = text[:validate.JD_MAX]
            tasks.queue_analyze(run)
            if jd_note:
                note("status", jd_note)
        return redirect(safe_next(url_for("run_detail", run_id=run.id)))

    @app.route("/runs/<run_id>/rewrite", methods=["POST"])
    def rewrite(run_id: str):
        """A new version from your profile as it is now, with the answers you already gave."""
        run = get_run_or_404(run_id)
        if run.status in ACTIVE_STATUSES:
            note("status", "That job is already being processed.", error=True)
        elif not _can_answer(run) or run.status == "needs_answers":
            note("status", "Answer the questions first.", error=True)
        else:
            upcoming = tasks.queue_generate(run, alongside=False)
            note("status", f"Writing v{upcoming} from your profile as it is now, with your earlier answers. Earlier versions are kept.")
        return redirect(safe_next(url_for("run_detail", run_id=run.id)))

    @app.route("/profile/rewrite", methods=["POST"])
    def profile_rewrite():
        """New versions, from your updated profile, of every job last written from an older one."""
        started = 0
        for run, _ in stale_runs():
            if run.status not in ACTIVE_STATUSES:
                tasks.queue_generate(run, alongside=False)
                started += 1
        flash(f"Writing new versions for {started} job{'s' if started != 1 else ''} from your updated profile. "
              "Earlier versions are kept; the nerdbar shows each one." if started else "Every job is already up to date.")
        return redirect(url_for("profile"))

    @app.route("/retry/<run_id>", methods=["POST"])
    @app.route("/runs/<run_id>/retry", methods=["POST"])
    def retry(run_id: str):
        run = get_run_or_404(run_id)
        if run.status in ACTIVE_STATUSES:
            message = "That job is already being processed."
        elif run.status == "failed" and run.failed_stage == "generate":
            tasks.queue_generate(run)
            message = ""
        elif run.status == "failed" and run.jd_text.strip():
            tasks.queue_analyze(run)
            message = ""
        elif run.job_url:
            tasks.queue_fetch(run)
            message = ""
        else:
            message = "Paste the job description to continue."
        run = store.get_run(run_id)
        if wants_json():
            return jsonify({**run_json(run), "message": message})
        if message:
            note("status", message, error=True)
        return redirect(safe_next(url_for("run_detail", run_id=run.id)))

    @app.route("/runs/<run_id>/questions", methods=["GET", "POST"])
    def run_questions(run_id: str):
        run = get_run_or_404(run_id)
        if not _can_answer(run):
            flash("That job is not waiting for answers.")
            return redirect(url_for("run_detail", run_id=run.id))
        regenerating = run.status != "needs_answers"
        if request.method == "POST":
            answers, cut = [], []
            for i, q in enumerate(run.questions):
                choice = request.form.get(f"answer_{i}", "skip")
                choice = "none" if choice == "no" else choice  # older forms said yes / no
                detail = " ".join(request.form.get(f"detail_{i}", "").split())
                if len(detail) > validate.ANSWER_MAX:
                    detail, cut = detail[:validate.ANSWER_MAX], cut + [q["term"]]
                has = choice in ("yes", *ANSWER_LEVELS)
                text = (f"{choice}: {detail}" if detail else choice) if has else ("no" if choice == "none" else "")
                answers.append({"term": q["term"], "skipped": not has and choice != "none", "text": text, "add_role": None,
                                "level": choice if choice in (*ANSWER_LEVELS, "none") else ""})
            run.answers = answers
            run.generate_letter = bool(request.form.get("generate_letter"))
            run.apply_alongside = bool(request.form.get("apply_alongside"))
            upcoming = tasks.queue_generate(run)
            logger.info("Answers submitted run=%s answers=%d letter=%s", run.id, len(answers), run.generate_letter)
            if cut:
                flash(f"Kept the first {validate.ANSWER_MAX} characters of your answer about {', '.join(cut)}.")
            if regenerating:
                flash(f"Writing version {upcoming}. Earlier versions are kept.")
                return redirect(url_for("run_detail", run_id=run.id))
            nxt = next((r for r in batch_runs() if r.status == "needs_answers" and r.id != run.id), None)
            if nxt is not None:
                flash(f"Writing the CV for {run.title or 'that job'}. Next job below.")
                return redirect(url_for("run_questions", run_id=nxt.id))
            flash(f"Writing the CV for {run.title or 'that job'}.")
            return redirect(url_for("jobs"))
        ids = session.get("run_ids", [])
        position = (ids.index(run.id) + 1, len(ids)) if run.id in ids and len(ids) > 1 and not regenerating else None
        previous = {  # older yes/no answers show as Intermediate / No experience
            a.term: {"choice": "skip" if a.skipped else (a.level or ("intermediate" if a.is_yes else "none")), "detail": a.detail}
            for a in answers_from_dicts(run.answers)
        }
        start, no_start = scout.start_url(run, assistant=True)
        alongside = {"possible": bool(start) and engine_ready(), "site": scout.site_of(start) if start else "",
                     "why_not": no_start if not start else "" if engine_ready() else "the browser engine isn't installed",
                     "default": run.apply_alongside if regenerating else automation_settings(data_dir)["APPLY_ALONGSIDE"] == "1",
                     "busy": any(s.is_alive() for s in [sessions.get(a.id) for a in store.list_applications()] if s)}
        return render_template("questions.html", run=run, position=position, previous=previous,
                               regenerating=regenerating, next_version=store.next_version(run.id), alongside=alongside,
                               evidence={g["term"].casefold(): g for g in job_gaps(run)})

    # ---- improving a version from its ATS check ----

    @app.route("/runs/<run_id>/versions/<int:version>/improve", methods=["POST"])
    def improve_start(run_id: str, version: int):
        """The items you picked from a version's ATS check: checked against your profile (one LLM request), then asked."""
        run = get_run_or_404(run_id)
        entry = next((v for v in version_entries(run, job_gaps(run)) if v["version"] == version), None)
        if entry is None or not entry["ats"]:
            flash(f"v{version} has no ATS check to improve from.", "error")
            return redirect(url_for("run_detail", run_id=run.id))
        if run.status in ACTIVE_STATUSES:
            flash("This job is busy. Improve it once the current step is done.", "error")
            return redirect(url_for("run_detail", run_id=run.id))
        if store.get_profile() is None:
            flash("Add your CV on the Profile page first.", "error")
            return redirect(url_for("profile"))
        wanted = set(request.form.getlist("item"))
        picked = [i for i in entry["improvable"] if i["id"] in wanted]
        if not picked:
            flash("Pick at least one thing to improve in the ATS check.", "error")
            return redirect(url_for("run_detail", run_id=run.id))
        run.improve = {"version": version, "status": "checking", "picked": picked, "check": entry["ats"], "plan": {}, "error": "",
                       "started_at": datetime.now().astimezone().isoformat()}
        store.update_run(run)
        logger.info("job %s: improving v%d from its ATS check: %s", run.id[:6], version, ", ".join(i["id"] for i in picked))
        tasks.queue_improve(run)
        return redirect(url_for("improve_page", run_id=run.id))

    @app.route("/runs/<run_id>/improve", methods=["GET", "POST"])
    def improve_page(run_id: str):
        run = get_run_or_404(run_id)
        state = run.improve or {}
        if state.get("status") not in ("checking", "ready"):
            return redirect(url_for("run_detail", run_id=run.id))
        base = next((v for v in version_entries(run, job_gaps(run)) if v["version"] == state.get("version")), None)
        profile_now = store.get_profile()
        if request.method == "POST":
            if state.get("status") != "ready" or profile_now is None:
                flash("Your picks are still being checked. Answer once the questions are here.", "error")
                return redirect(url_for("improve_page", run_id=run.id))
            with store.locked():
                profile_now = store.get_profile()
                updated, levels, focus, problems = improve.apply(profile_now, state["plan"], request.form)
                if updated != profile_now:
                    store.save_profile(updated)  # your answers are facts now, for this CV and every one after it
                run = store.find_run(run.id)
                if levels:  # a skill's level is the job's answer to it, as on the questions page (and shown there)
                    terms = {lv["term"] for lv in levels}
                    run.answers = [a for a in run.answers if a.get("term") not in terms]
                    required = {k["term"]: k.get("required", True) for k in (state.get("check") or {}).get("keywords") or []}
                    for lv in levels:
                        has = lv["level"] != "none"
                        run.answers.append({"term": lv["term"], "skipped": False, "text": lv["level"] if has else "no",
                                            "add_role": None, "level": lv["level"]})
                        if not any(q.get("term") == lv["term"] for q in run.questions):
                            run.questions.append({"term": lv["term"], "prompt": f"How much experience do you have with {lv['term']}?",
                                                  "kind": "level", "required": required.get(lv["term"], True), "score": 0,
                                                  "why": "Asked when improving a version from its ATS check.",
                                                  "optional": False, "context": ""})
                run.generate_letter = bool(request.form.get("generate_letter"))
                run.improve = {**state, "status": "writing", "focus": improve.keyword_focus(state["plan"]) + focus}
                store.update_run(run)
            for problem in problems:
                flash(problem, "error")
            logger.info("job %s: improvement answers saved (%d skill levels, %d notes for the writer)", run.id[:6],
                        len(levels), len(focus))
            upcoming = tasks.queue_generate(run)
            flash(f"Your answers are in your profile. Writing v{upcoming}: its ATS check is compared with v{state['version']}'s "
                  "when it's done.")
            return redirect(url_for("run_detail", run_id=run.id))
        return render_template("improve.html", run=run, state=state, base=base, busy=tasks.busy(run.id),
                               next_version=store.next_version(run.id),
                               questions=improve.numbered_questions(state.get("plan") or {}),
                               roles=profile_now.experiences if profile_now else [], profile=profile_now)

    @app.route("/runs/<run_id>/improve/again", methods=["POST"])
    def improve_again(run_id: str):
        """Have the LLM check the picks again (after it failed, or to get the questions worded afresh)."""
        run = get_run_or_404(run_id)
        if (run.improve or {}).get("status") == "ready":
            run.improve = {**run.improve, "status": "checking", "plan": {}, "error": ""}
            store.update_run(run)
            tasks.queue_improve(run)
        return redirect(url_for("improve_page", run_id=run.id))

    @app.route("/runs/<run_id>/improve/cancel", methods=["POST"])
    def improve_cancel(run_id: str):
        run = get_run_or_404(run_id)
        if (run.improve or {}).get("status") in ("checking", "ready"):
            run.improve = {}
            store.update_run(run)
            flash("Improvement cancelled. Nothing was changed.")
        return redirect(url_for("run_detail", run_id=run.id))

    @app.route("/runs/<run_id>/archive", methods=["POST"])
    def archive_run(run_id: str):
        run = get_run_or_404(run_id)
        archive = request.form.get("archived") == "1"
        store.set_archived([run.id], archive)
        flash("Job archived. It is hidden from lists; find it under Jobs → Archived." if archive else "Job restored.")
        return redirect(safe_next(url_for("run_detail", run_id=run.id)))

    @app.route("/runs/<run_id>/delete", methods=["POST"])
    def delete_run(run_id: str):
        run = get_run_or_404(run_id)
        for application in store.list_applications(run.id):
            if sessions.get(application.id) is not None:
                sessions.get(application.id).send({"action": "stop"})
        remove_files(store.delete_run(run.id))
        flash(f"Deleted {run.title or 'the job'} and all its versions.")
        return redirect(url_for("jobs"))

    # ---- documents ----

    @app.route("/documents")
    def documents():
        args = request.args
        data = document_rows(
            store,
            kind=args.get("kind", "all"),
            q=args.get("q", "").strip(),
            company=args.get("company", ""),
            job=args.get("job", ""),
            latest=args.get("latest") == "1",
            include_archived=args.get("archived") == "1",
            sort=args.get("sort", "newest"),
            group=args.get("group", "none"),
        )
        return render_template("documents.html", data=data, args=args, kinds=DOC_KINDS, sorts=DOC_SORTS, groups=DOC_GROUPS)

    @app.route("/documents/<doc_id>/preview")
    def document_preview(doc_id: str):
        """A CV or letter version, as the job's page previews it, for the right half of Documents."""
        doc = store.get_document(doc_id)
        if doc is None:
            abort(404)
        run = store.find_run(doc.run_id)
        if run is None:
            abort(404)
        entry = next((v for v in version_entries(run, job_gaps(run)) if v["version"] == doc.version), None)
        return render_template("_document_preview.html", doc=doc, run=run, v=entry or {}, upload=None)

    @app.route("/uploads/<upload_id>/preview")
    def upload_preview(upload_id: str):
        """The text read from a CV you uploaded, for the right half of Documents and the profile editor."""
        upload_rec = store.get_upload(upload_id)
        if upload_rec is None:
            abort(404)
        return render_template("_document_preview.html", doc=None, run=None, v={}, upload=upload_rec)

    def _download_name(run: JobRun | None, kind: str, version: int | None = None) -> str:
        profile = store.get_profile()
        who = profile.name if profile and profile.name else "CV"
        where = (run.company or run.title or run.id[:8]) if run else "job"
        tag = f" v{version}" if version else ""
        return secure_filename(f"{who} {kind}{tag} - {where}.docx") or f"{kind}.docx"

    @app.route("/documents/<doc_id>/download")
    def download_document(doc_id: str):
        doc = store.get_document(doc_id)
        if doc is None:
            abort(404)
        # Files saved by earlier versions may have relative paths (from the folder the app started in). Flask would
        # read those from its package folder, so they're made absolute the same way this check reads them.
        if not Path(doc.path).exists():
            flash("That file is no longer on disk.", "error")
            return redirect(url_for("run_detail", run_id=doc.run_id))
        run = store.find_run(doc.run_id)
        kind = "CV" if doc.kind == "cv" else "Cover letter"
        return send_file(Path(doc.path).absolute(), as_attachment=True, download_name=_download_name(run, kind, doc.version))

    def _repoint_latest(run_id: str) -> None:
        """After deleting files, point the job at its newest remaining CV and that same version's letter."""
        with store.locked():
            run = store.find_run(run_id)
            if run is None:
                return
            remaining = store.list_documents(run.id)
            cvs = [d for d in remaining if d.kind == "cv"]
            newest = max(cvs, key=lambda d: d.version) if cvs else None
            letter = next((d for d in remaining if d.kind == "letter" and newest and d.version == newest.version), None)
            run.output_cv_path = newest.path if newest else ""
            run.letter_path = letter.path if letter else ""
            if newest is None and run.status == "ready":
                run.status = "needs_answers"  # nothing left to download; the questions are still there
            store.update_run(run)

    @app.route("/documents/<doc_id>/delete", methods=["POST"])
    def delete_document(doc_id: str):
        doc = store.delete_document(doc_id)
        if doc is None:
            abort(404)
        remove_files([doc.path])
        _repoint_latest(doc.run_id)
        flash(f"Deleted {'CV' if doc.kind == 'cv' else 'cover letter'} v{doc.version}.")
        return redirect(safe_next(url_for("run_detail", run_id=doc.run_id)))

    @app.route("/runs/<run_id>/versions/<int:version>/delete", methods=["POST"])
    def delete_version(run_id: str, version: int):
        run = get_run_or_404(run_id)
        docs = [d for d in store.list_documents(run.id) if d.version == version]
        if not docs:
            abort(404)
        for d in docs:
            store.delete_document(d.id)
        remove_files([d.path for d in docs])
        _repoint_latest(run.id)
        flash(f"Deleted v{version}.")
        return redirect(url_for("run_detail", run_id=run.id))

    def _download(run: JobRun, path: str, kind: str):
        if not path or not Path(path).exists():
            flash(f"The {kind} for that job is not available.", "error")
            return redirect(url_for("run_detail", run_id=run.id))
        latest = next((d for d in reversed(store.list_documents(run.id)) if d.path == path), None)
        return send_file(Path(path).absolute(), as_attachment=True, download_name=_download_name(run, kind, latest.version if latest else None))

    @app.route("/download/<run_id>/cv")
    def download_cv(run_id: str):
        run = get_run_or_404(run_id)
        return _download(run, run.output_cv_path, "CV")

    @app.route("/download/<run_id>/letter")
    def download_letter(run_id: str):
        run = get_run_or_404(run_id)
        return _download(run, run.letter_path, "Cover letter")

    # ---- apply (a separate, optional step after the documents) ----

    def apply_files(run: JobRun, version: int) -> dict:
        docs = [d for d in store.list_documents(run.id) if d.version == version]
        return {"cv": next((d.path for d in docs if d.kind == "cv"), ""),
                "letter": next((d.path for d in docs if d.kind == "letter"), "")}

    def documents_for(application_id: str):
        """The application's documents once they exist on disk (None while its CV version is still being written)."""
        def documents():
            application = store.get_application(application_id)
            run = store.find_run(application.run_id) if application else None
            files = apply_files(run, application.version) if run else {"cv": ""}
            return files if files["cv"] and Path(files["cv"]).exists() else None
        return documents

    def start_session(application, wait_for_documents: bool = False) -> str:
        run = store.find_run(application.run_id)
        if run is None:
            return "That job no longer exists."
        files = apply_files(run, application.version)
        if not wait_for_documents and (not files["cv"] or not Path(files["cv"]).exists()):
            return f"The CV file for v{application.version} is missing. Write a new version first."
        make_private_dir(browser_dir)
        session = ApplySession(application=application, store=store, get_model=get_model, browser_dir=browser_dir,
                               files=documents_for(application.id),
                               job_label=(run.title or "the job") + (f" at {run.company}" if run.company else ""),
                               job_context=f"{run.title} {run.company}\n{run.jd_text}")
        return sessions.start(session)

    def start_alongside(run: JobRun, version: int) -> None:
        """Applying alongside the writing: fill in everything that doesn't need the CV while it's written."""
        why = ""
        url, missing = scout.start_url(run, assistant=True)
        if not engine_ready():
            why = "the browser engine isn't installed (see Apply on the job's page)"
        elif not url:
            why = missing
        if why:
            feed.emit("warn", f"! not applying alongside: {why}")
            return
        application = store.create_application(run, version, url)
        error = start_session(application, wait_for_documents=True)
        if error:
            store.delete_application(application.id)
            feed.emit("warn", f"! not applying alongside: {error}")
        else:
            feed.emit("apply", f"▶ applying alongside the writing of v{version}: filling in what doesn't need the CV first")

    @app.route("/runs/<run_id>/apply", methods=["GET", "POST"])
    def apply_start(run_id: str):
        run = get_run_or_404(run_id)
        versions = sorted({d.version for d in store.list_documents(run.id) if d.kind == "cv"}, reverse=True)
        if not versions:
            flash("Write a CV for this job first; the assistant attaches it to the application.", "error")
            return redirect(url_for("run_detail", run_id=run.id))
        if request.method == "POST":
            start_url = request.form.get("start_url", "").strip()
            if urlparse(start_url).scheme not in ("http", "https") or not urlparse(start_url).netloc:
                flash("Enter the link to the job's application page (http or https).", "error")
                return redirect(url_for("apply_start", run_id=run.id))
            version = request.form.get("version", type=int) or versions[0]
            application = store.create_application(run, version if version in versions else versions[0], start_url,
                                                    show_window=bool(request.form.get("show_window")))
            error = start_session(application)
            if error:
                store.delete_application(application.id)
                flash(error, "error")
                return redirect(url_for("apply_start", run_id=run.id))
            return redirect(url_for("application_page", app_id=application.id))
        details = app_details.load(store)
        missing = [label for key, label, _ in app_details.STANDARD[:4] if not details["standard"].get(key)]
        start = scout.start_url(run, assistant=True)[0] or run.job_url or ""
        return render_template("apply_start.html", run=run, versions=versions, details=details, missing=missing, start=start,
                               saved_sites={a["site"] for a in apply_accounts.list_accounts(store) if a.get("in_keychain")},
                               linkedin_start=bool(start) and scout.site_of(start) == "linkedin.com",
                               engine_ready=importlib.util.find_spec("playwright") is not None,
                               applications=list(reversed(store.list_applications(run.id))))

    @app.route("/applications/<app_id>")
    def application_page(app_id: str):
        application = store.get_application(app_id)
        if application is None:
            abort(404)
        return render_template("apply.html", application=application, run=store.find_run(application.run_id),
                               viewport=VIEWPORT)

    @app.route("/api/applications/<app_id>")
    def api_application(app_id: str):
        application = store.get_application(app_id)
        if application is None:
            abort(404)
        session = sessions.get(app_id)
        return jsonify({
            "status": application.status, "waiting": application.waiting, "url": application.current_url,
            "error": application.error, "submitted_at": application.submitted_at, "submitted_by": application.submitted_by,
            "live": bool(session and session.is_alive()), "mode": session.mode if session else "",
            "frame": session.frame_seq if session else -1, "target": session.target if session else None,
            "filled": application.filled, "new_password": session.new_password if session else "",
        })

    @app.route("/api/applications/<app_id>/frame")
    def application_frame(app_id: str):
        frame = sessions.frame(app_id)
        if not frame:
            return "", 204
        return app.response_class(frame, mimetype="image/jpeg")

    @app.route("/api/applications/<app_id>/control", methods=["POST"])
    def application_control(app_id: str):
        application = store.get_application(app_id)
        if application is None:
            abort(404)
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        session = sessions.get(app_id)
        if action == "start":
            if session is not None:
                return jsonify({"ok": True})
            application.status, application.error, application.waiting = "starting", "", {}
            store.update_application(application)
            error = start_session(application)
            if error:
                application.status, application.error = "stopped", error
                store.update_application(application)
            return jsonify({"ok": not error, "message": error})
        if action == "write_letter" and session is not None:
            # The form requires a cover letter this version doesn't have: write a new version with one, then attach it.
            run = store.find_run(application.run_id)
            run.generate_letter = True
            application.version = tasks.queue_generate(run, alongside=False)
            store.update_application(application)
            session.send({"action": "await_documents"})
            return jsonify({"ok": True})
        if session is None:
            if action == "mark_submitted":
                application.status, application.submitted_by = "submitted", "you"
                application.submitted_at = datetime.now().astimezone().isoformat()
                store.update_application(application)
                feed.emit("ok", "✓ application marked as submitted by you")
                return jsonify({"ok": True})
            return jsonify({"ok": False, "message": "The assistant isn't running. Resume it first."}), 409
        if action in ("pause", "resume", "stop", "input", "nav", "answer", "allow_site", "account_fill", "approve_submit",
                      "mark_submitted"):
            if action == "answer":
                answers = body.get("answers")
                if not isinstance(answers, list) or len(answers) > 50:
                    return jsonify({"ok": False, "message": "Those answers couldn't be read. Try again."}), 400
                body["answers"] = [{**a, "value": str(a.get("value") or "")[:2000]} for a in answers if isinstance(a, dict)]
            session.send(body)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "message": "Unknown action."}), 400

    @app.route("/runs/<run_id>/scan", methods=["POST"])
    def scan_application(run_id: str):
        run = get_run_or_404(run_id)
        back = redirect(url_for("run_detail", run_id=run.id) + "#before")
        typed = request.form.get("apply_url", "").strip()
        if "apply_url" in request.form and not typed:
            note("before", "Paste the link of the company's application page first.", error=True)
            return back
        if typed:
            link = scout.application_link(typed)
            if not link:
                note("before", f"“{typed[:80]}” isn't a web address. Copy the application page's address from your browser's "
                     "address bar (it starts with https://).", error=True)
                return back
            if scout.site_of(link) == "linkedin.com":
                note("before", "That's LinkedIn's page, not the company's application: LinkedIn shows the company's link only "
                     "after you sign in. On LinkedIn (signed in, in your own browser), click Apply, then copy the address of "
                     "the page it opens and paste that here. Or press Apply with the assistant below and click Apply in "
                     "its window.", error=True)
                return back
            run.apply_url = link
            store.update_run(run)
            logger.info("job %s: application link added (%s)", run.id[:6], scout.site_of(link))
        if tasks.scanner is None:
            note("before", "Checking applications needs the browser engine: pip install playwright, then "
                 "python -m playwright install chromium.", error=True)
        elif tasks.queue_scan(run):
            note("before", f"Checking the application on {scout.site_of(run.apply_url or run.job_url or '')} (read-only: "
                 "nothing is entered). What it needs appears here in a moment.")
        else:
            note("before", "The application is already being checked; what it needs appears here in a moment.")
        return back

    @app.route("/runs/<run_id>/research", methods=["POST"])
    def research_company(run_id: str):
        run = get_run_or_404(run_id)
        depth = request.form.get("depth", "simple")
        if not run.company:
            note("research", "The company isn't known for this job yet.", error=True)
        elif tasks.researcher is None or depth not in ("simple", "thorough"):
            note("research", "Company research isn't available here.", error=True)
        elif tasks.queue_research(run, depth):
            note("research", f"Researching {run.company} ({depth}). Results appear here in a moment; the nerdbar shows each source.")
        else:
            note("research", f"Research on {run.company} is already under way.")
        return redirect(url_for("run_detail", run_id=run.id) + "#research")

    @app.route("/runs/<run_id>/prep", methods=["POST"])
    def write_prep(run_id: str):
        run = get_run_or_404(run_id)
        if tasks.queue_prep(run):
            note("prep", "Writing interview prep notes. They appear here in a moment (one LLM request).")
        else:
            note("prep", "The prep notes are already being written.")
        return redirect(url_for("run_detail", run_id=run.id) + "#prep")

    @app.route("/runs/<run_id>/prep/reviews", methods=["POST"])
    def summarise_reviews(run_id: str):
        run = get_run_or_404(run_id)
        text = request.form.get("reviews", "").strip()
        if len(text) < 80:
            note("research", "Paste a few reviews first (at least a couple of sentences).", error=True)
        else:
            cut = (f" Only the first {prep.REVIEWS_LIMIT:,} of the {len(text):,} characters fit in it, so put the reviews that "
                   "matter most first." if len(text) > prep.REVIEWS_LIMIT else "")
            if tasks.queue_reviews(run, text):
                note("research", "Summarising the reviews you pasted (one LLM request; your name and contact details are hidden "
                     "as usual)." + cut)
            else:
                note("research", "Reviews are already being summarised. Paste these again once that's done.", error=True)
        return redirect(url_for("run_detail", run_id=run.id) + "#research")

    @app.route("/settings/automation", methods=["POST"])
    def settings_automation():
        values = {key: request.form.get(key, "").strip() for key in ("LLM_RPM", "LLM_CONCURRENCY", "LLM_DAILY_CAP")}
        problems = validate.pace(values)
        if problems:
            for problem in problems:
                flash(problem + " Nothing was saved.", "error")
            return redirect(url_for("settings") + "#automation")
        values.update({key: "1" if request.form.get(key) else "0"
                       for key in ("SCAN_APPLICATIONS", "APPLY_ALONGSIDE", "PREP_AUTO", "RESEARCH_AUTO")})
        if request.form.get("RESEARCH_DEPTH") in research.DEPTHS:
            values["RESEARCH_DEPTH"] = request.form["RESEARCH_DEPTH"]
        if request.form.get("POLITENESS") in polite.LEVELS:
            values["POLITENESS"] = request.form["POLITENESS"]
        if request.form.get("sources_shown"):
            values["RESEARCH_SOURCES"] = ",".join(s for s in request.form.getlist("RESEARCH_SOURCES") if s in research.SOURCES)
        new_key = request.form.get("TAVILY_API_KEY", "").strip()
        if new_key and not research.TAVILY_KEY.fullmatch(new_key):
            flash("That doesn't look like a Tavily key (they start with tvly-). Nothing was saved.", "error")
            return redirect(url_for("settings") + "#automation")
        extra = ""
        if new_key and not tavily_key(data_dir)[0] and request.form.get("sources_shown"):
            chosen = [s for s in values["RESEARCH_SOURCES"].split(",") if s]
            if "tavily" not in chosen:  # a first key: use it (untick Tavily search to stop)
                values["RESEARCH_SOURCES"] = ",".join(chosen + ["tavily"])
            extra = " Tavily search is now one of the research sources (Thorough research)."
        save_automation(data_dir, values)
        if request.form.get("clear_tavily_key"):
            save_tavily_key(data_dir, clear=True)
            extra = " The Tavily key was removed."
        elif new_key:
            save_tavily_key(data_dir, new_key)
        flash("Saved. The new limits apply to the next request." + extra)
        return redirect(url_for("settings") + "#automation")

    @app.route("/settings/tavily/test", methods=["POST"])
    def tavily_test():
        key = request.form.get("TAVILY_API_KEY", "").strip() or tavily_key(data_dir)[0]
        if not key:
            return jsonify({"ok": False, "message": "Paste your Tavily key first."})
        if not research.TAVILY_KEY.fullmatch(key):
            return jsonify({"ok": False, "message": "That doesn't look like a Tavily key (they start with tvly-)."})
        ok, message = research.tavily_usage(key)
        return jsonify({"ok": ok, "message": message})

    @app.route("/settings/cache/clear", methods=["POST"])
    def clear_cache():
        flash(f"Forgot {store.clear_llm_cache()} earlier answer(s). Repeated requests will go to the LLM again.")
        return redirect(url_for("settings") + "#automation")

    @app.route("/settings/apply", methods=["GET", "POST"])
    def apply_details_page():
        if request.method == "POST":
            standard = {key: " ".join(request.form.get(key, "").split()) for key, _, _ in app_details.STANDARD}
            custom, line_problems = validate.custom_answers(request.form.get("custom", ""))
            problems = validate.application_details(standard)
            if problems:
                for problem in problems:
                    flash(problem, "error")
                return render_template("apply_details.html", details={"standard": standard, "custom": custom},
                                       standard=app_details.STANDARD, private=app_details.PRIVATE_KINDS,
                                       accounts=apply_accounts.list_accounts(store),
                                       keychain=apply_accounts.keychain_name() if apply_accounts.can_store() else ""), 400
            app_details.save(store, standard, custom)
            for problem in line_problems:
                flash(problem, "error")
            flash("Application details saved. They stay on this computer; the LLM only sees placeholders for your contact details.")
            return redirect(safe_next(url_for("apply_details_page")))
        return render_template("apply_details.html", details=app_details.load(store), standard=app_details.STANDARD,
                               private=app_details.PRIVATE_KINDS, accounts=apply_accounts.list_accounts(store),
                               keychain=apply_accounts.keychain_name() if apply_accounts.can_store() else "")

    @app.route("/settings/apply/passwords.csv", methods=["POST"])
    def export_passwords():
        """Your job-site passwords, for a password manager. A POST you confirm, so no link or other site can start it."""
        if not request.form.get("understood"):
            flash("Tick the box first: the file has your passwords in plain text.", "error")
            return redirect(url_for("apply_details_page") + "#accounts")
        rows, missing = apply_accounts.export_rows(store)
        if not rows:
            flash("There are no saved passwords to export" + (f" ({missing} account(s) have none saved)." if missing else "."),
                  "error")
            return redirect(url_for("apply_details_page") + "#accounts")
        feed.emit("user", f"exported {len(rows)} job-site password{'s' if len(rows) != 1 else ''} for a password manager")
        return Response(apply_accounts.to_csv(rows), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=cv-tailor-passwords-{date.today():%Y-%m-%d}.csv",
                                 "Cache-Control": "no-store"})

    @app.route("/settings/apply/accounts", methods=["POST"])
    def add_account():
        """An account you already have on a job site, so the assistant can sign you in there."""
        back = url_for("apply_details_page", next=safe_next("") or None) + "#accounts"
        site = apply_accounts.account_key(request.form.get("site", ""))
        user = " ".join(request.form.get("username", "").split())
        password = request.form.get("password", "")
        if not site or "." not in site:
            flash("Enter the site, like careers.acme.com or acme.wd3.myworkdayjobs.com.", "error")
        elif site.endswith("linkedin.com"):
            flash("LinkedIn isn't saved here: LinkedIn allows no assistant to sign in for you. Sign in once yourself in the "
                  "assistant's window; its browser stays signed in.", "error")
        elif not user or len(user) > 200:
            flash("Enter the email address or username you sign in with.", "error")
        elif len(password) > 300:
            flash("That password is longer than 300 characters.", "error")
        else:
            existing = next((a for a in apply_accounts.list_accounts(store) if a["site"] == site and a["email"] == user), None)
            stored = False
            if password and apply_accounts.can_store():
                stored = apply_accounts.store_password(site, user, password)
            apply_accounts.remember_account(store, site, user, stored or bool(existing and existing.get("in_keychain")))
            feed.emit("user", f"saved an account for {site}")
            if password and not stored:
                flash(f"Saved the {site} account, but not its password: no system password store was found on this "
                      "computer, so it couldn't be kept safely.", "error")
            else:
                flash(f"Saved your {site} account" + (f", with its password in {apply_accounts.keychain_name()}." if stored else "."))
        return redirect(back)

    @app.route("/settings/apply/auto-sign-in", methods=["POST"])
    def auto_sign_in():
        app_details.set_auto_sign_in(store, bool(request.form.get("auto_sign_in")))
        flash("Saved.")
        return redirect(url_for("apply_details_page", next=safe_next("") or None) + "#accounts")

    @app.route("/settings/apply/forget", methods=["POST"])
    def forget_account():
        apply_accounts.forget_account(store, request.form.get("site", ""), request.form.get("email", ""))
        flash("Forgotten. The saved password was removed from your system's password store, if there was one.")
        return redirect(url_for("apply_details_page"))

    # ---- settings & your data ----

    def _form_settings() -> dict:
        return {key: request.form.get(key, "") for key in SETTING_KEYS}

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        typed = None
        if request.method == "POST":
            typed = _form_settings()
            problems = validate.llm_settings(typed)
            if not problems:
                saved = save_settings(data_dir, typed, clear_api_key=bool(request.form.get("clear_api_key")))
                logger.info("Settings updated provider=%s model=%s", saved.get("LLM_PROVIDER"), saved.get("LLM_MODEL"))
                flash("Settings saved. They apply to the next step that uses the LLM.")
                return redirect(url_for("settings"))
            for problem in problems:
                flash(problem + " Nothing was saved.", "error")
        values, sources = effective_settings(data_dir)
        if typed:  # show what was typed (never the key) so it can be corrected
            values = {**values, **{k: v.strip() for k, v in typed.items() if k != "LLM_API_KEY"}}
        key = values.get("LLM_API_KEY") or ""
        key_hint = f"…{key[-4:]} (from {'Settings' if sources['LLM_API_KEY'] == 'settings' else '.env'})" if key else ""
        counts = {
            "jobs": len(store.list_runs()),
            "documents": len(store.list_documents()),
            "uploads": len(store.list_uploads()),
            "applications": len(store.list_applications()),
            "profile": store.get_profile() is not None,
        }
        today = store.get_preference("llm_daily") or {}
        return render_template("settings.html", settings=values, sources=sources, key_hint=key_hint,
                               problems=log_file.recent_problems(data_dir),
                               data_dir=data_dir.resolve(), data_source=data_source, counts=counts,
                               never_send=store.get_never_send(), sent_count=len(store.list_llm_calls()),
                               automation=automation_settings(data_dir), limits=llm_limits(data_dir),
                               used_today=today.get("count", 0) if today.get("date") == date.today().isoformat() else 0,
                               engine=engine_ready(), levels=polite.LEVELS, research_sources=research.SOURCES,
                               keychain=apply_accounts.keychain_name() if apply_accounts.can_store() else "",
                               accounts=apply_accounts.list_accounts(store),
                               tavily_hint=("…" + tavily_key(data_dir)[0][-4:]) if tavily_key(data_dir)[0] else "",
                               tavily_source=tavily_key(data_dir)[1],
                               chosen_sources=research.chosen_sources(automation_settings(data_dir)["RESEARCH_SOURCES"])),             (400 if typed else 200)

    @app.route("/settings/theme", methods=["POST"])
    def set_theme():
        theme = request.form.get("theme", "")
        store.set_preference("theme", theme if theme in THEMES else "auto")
        return redirect(safe_next(url_for("settings") + "#appearance"))

    @app.route("/settings/privacy", methods=["POST"])
    def settings_privacy():
        terms, problems = validate.terms(request.form.get("never_send", ""))
        for problem in problems:
            flash(problem, "error")
        store.set_never_send(terms)
        flash("Saved. These words are hidden from every request from now on." if terms else "Saved. The never-send list is empty.")
        return redirect(url_for("settings") + "#privacy")

    @app.route("/settings/log")
    def log_page():
        level = request.args.get("level", "all")
        level = level if level in log_file.LEVELS else "all"
        ref, q = request.args.get("ref", "").strip()[:40], request.args.get("q", "").strip()[:200]
        return render_template("log.html", data=log_file.read(data_dir, level=level, ref=ref, q=q), level=level, ref=ref,
                               q=q, path=log_path, problems=log_file.recent_problems(data_dir))

    @app.route("/settings/log/download")
    def log_download():
        return Response(log_file.text(data_dir), mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=cv-tailor-log-{date.today():%Y-%m-%d}.txt",
                                 "Cache-Control": "no-store"})

    @app.route("/settings/log/clear", methods=["POST"])
    def log_clear():
        log_file.clear(data_dir)
        flash("The log was cleared.")
        return redirect(url_for("log_page"))

    @app.route("/settings/sent")
    def sent_log():
        return render_template("sent.html", calls=store.list_llm_calls(), size=store.LLM_LOG_SIZE)

    @app.route("/settings/sent/clear", methods=["POST"])
    def clear_sent_log():
        store.clear_llm_log()
        flash("Cleared the record of what was sent.")
        return redirect(url_for("sent_log"))

    @app.route("/settings/test", methods=["POST"])
    def settings_test():
        values, _ = effective_settings(data_dir)
        values.update({k: v.strip() for k, v in _form_settings().items() if v.strip()})
        try:
            return jsonify({"ok": True, "message": check_connection(values)})
        except (ProviderUnsetError, LLMError) as exc:
            return jsonify({"ok": False, "message": str(exc)})
        except Exception as exc:
            return jsonify({"ok": False, "message": f"Unexpected error ({type(exc).__name__}): {exc}"})

    @app.route("/settings/export")
    def export_data():
        return send_file(build_export(store), mimetype="application/zip", as_attachment=True,
                         download_name=f"cv-tailor-export-{date.today():%Y-%m-%d}.zip")

    @app.route("/settings/delete-all", methods=["POST"])
    def delete_all():
        if request.form.get("confirm", "").strip().upper() != "DELETE":
            flash('Nothing was deleted. Type DELETE to confirm.', "error")
            return redirect(url_for("settings") + "#your-data")
        files = [d.path for d in store.list_documents()] + [u.path for u in store.list_uploads()]
        files += [p for folder in (uploads_dir, output_dir) for p in folder.iterdir()]
        sessions.stop_all()
        for account in apply_accounts.list_accounts(store):  # passwords live in the system's password store
            apply_accounts.forget_password(account["site"], account["email"])
        store.delete_everything()
        removed = remove_files(set(map(str, files)))
        if browser_dir.exists():
            shutil.rmtree(browser_dir, ignore_errors=True)
        if request.form.get("include_settings"):
            remove_files([data_dir / "settings.json"])
        log_file.clear(data_dir)  # it names jobs and companies
        session.clear()
        logger.info("All personal data deleted (%d files)", removed)
        flash("All your data was deleted from this computer." +
              ("" if request.form.get("include_settings") else " Your LLM settings were kept."))
        return redirect(url_for("profile"))

    return app
