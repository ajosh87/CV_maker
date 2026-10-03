"""One supervised application: a private browser on this computer, driven a page at a time, that you
can watch, pause, take over and resume from the app.

Playwright isn't thread-safe, so every browser call happens on the session's own thread. The web app
talks to it through a queue of commands, and reads its state and latest screenshot.
"""
import logging
import queue
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from cv_maker.apply import accounts
from cv_maker.apply import details as app_details
from cv_maker.apply import page as pg
from cv_maker.apply import planner
from cv_maker.events import feed
from cv_maker.honesty import allowed_facts_text, filter_text
from cv_maker.jobs import polite
from cv_maker.llm.chat import LLMError, ProviderUnsetError, complete_json
from cv_maker.pipeline.graph import _prompt_profile, _to_plain, friendly_error
from cv_maker.privacy import PrivateModel, mask

logger = logging.getLogger("cv_maker.apply")

VIEWPORT = {"width": 1280, "height": 860}
MAX_STEPS = 80  # page reads per application before handing back to you
MAX_SAME_PAGE = 3  # reads of an unchanged page before asking for help
PAUSED = "You're in control: use the view like a normal browser. Press Resume when the assistant should carry on."
# Human pace: a short pause after each field and after each page (the politeness level in Settings sets them).


class SetupError(Exception):
    """The browser engine isn't installed."""


class ApplySession(threading.Thread):
    def __init__(self, *, application, store, get_model, browser_dir, files, job_label: str, job_context: str,
                 on_finish=None) -> None:
        super().__init__(daemon=True, name=f"apply-{application.id[:6]}")
        self.app_id = application.id
        self.run_id = application.run_id
        self.store = store
        self.get_model = get_model
        self.browser_dir = browser_dir
        # {"cv": path, "letter": path or ""}, or a function returning that once the documents exist (None until then):
        # applying alongside the writing starts before the CV is ready.
        self._files = files
        self.job_label = job_label
        self.job_context = job_context
        self.on_finish = on_finish
        self.commands: queue.Queue = queue.Queue()
        self.pause_requested = threading.Event()
        self.stop_requested = threading.Event()
        self.frame, self.frame_seq, self.target = b"", 0, None
        self.mode = "agent"  # agent | paused | documents (waiting for the CV being written)
        self.page = None
        self.last_observation: dict | None = None
        self.new_password = ""  # shown to you once when there's no system password store; never saved by the app
        self._last_frame_at = 0.0
        self._last_signature = None
        self._same_page = 0
        self._steps = 0
        self._errors = 0
        self._submit_clicked = False
        self._cookie_sites: set[str] = set()
        self._details: dict = {}
        self._secrets: list = []
        self._signed_in: dict[str, int] = {}  # site -> sign-ins tried with your saved account (never loops)
        self._last_watch = 0.0

    # ---- talking to the web app (any thread) ----

    def send(self, command: dict) -> None:
        action = command.get("action")
        if action == "pause":
            self.pause_requested.set()
        elif action == "stop":
            self.stop_requested.set()
        else:
            self.commands.put(command)

    # ---- state shared with the app ----

    def _save(self, **changes):
        with self.store.locked():
            application = self.store.get_application(self.app_id)
            if application is None:  # deleted while running
                self.stop_requested.set()
                return None
            for key, value in changes.items():
                setattr(application, key, value)
            self.store.update_application(application)
            return application

    def _wait_for_you(self, kind: str, text: str, **extra) -> None:
        self.mode = "paused"
        self.pause_requested.clear()
        url = self.page.url if self.page is not None else ""
        self._save(status="needs_you", waiting={"kind": kind, "text": text, "url": url, **extra})
        feed.emit("user", f"⏸ {text}")

    def _allow(self, site: str, why: str) -> None:
        with self.store.locked():
            application = self.store.get_application(self.app_id)
            if application is not None and site not in application.allowed_sites:
                application.allowed_sites.append(site)
                self.store.update_application(application)
        feed.emit("apply", f"carrying on at {site} ({why})")

    def _watch(self) -> None:
        """While you do something only you can do (LinkedIn's Apply, a sign-in, a CAPTCHA), notice when it's done and
        carry on by itself: no Resume needed. Never when you took over yourself (then you say when)."""
        now = time.monotonic()
        if now - self._last_watch < 2.0 or self.page is None:
            return
        self._last_watch = now
        application = self.store.get_application(self.app_id)
        waiting = (application.waiting or {}) if application else {}
        kind, site = waiting.get("kind"), planner.site_of(self.page.url)
        if kind == "site" and waiting.get("linkedin") and site and not site.endswith("linkedin.com"):
            self._allow(site, "you opened it from LinkedIn's Apply")
            return self._carry_on()
        if kind in ("human", "account", "account_check", "verify"):
            moved = self.page.url != waiting.get("url")
            if kind == "human" or moved:
                info = pg.observe(self.page)
                if not planner.blocker(info) and (moved or not info.get("captcha")):
                    feed.emit("apply", "looks like you're through: carrying on")
                    return self._carry_on()

    def _carry_on(self) -> None:
        self.mode = "agent"
        self.pause_requested.clear()
        self._same_page, self._last_signature = 0, None
        self._save(status="working", waiting={}, error="")
        if self.page is not None:
            pg.settle(self.page)  # whatever you just did in the view may still be loading

    def _shown(self, text: str) -> str:
        """Log text with personal details replaced, the same way the LLM would see it."""
        return mask(text, self._secrets)[0]

    def documents(self) -> dict | None:
        files = self._files() if callable(self._files) else self._files
        return files if files and files.get("cv") else None

    # ---- waiting for the CV that's being written ----

    def _wait_for_documents(self) -> None:
        self.mode = "documents"
        self.target = None
        self._save(status="working", waiting={"kind": "documents", "text": "Waiting for your tailored CV, which is still being "
                                                                           "written. Everything else on this page is filled in."})
        feed.emit("apply", "⧗ waiting for the tailored CV before uploading it (the writer is on it)")

    def _check_documents(self) -> None:
        if self.documents() is not None:
            feed.emit("ok", "✓ the tailored CV is ready; attaching it")
            return self._carry_on()
        run = self.store.find_run(self.run_id)
        if run is None or run.status == "failed":
            reason = run.error if run else "The job was deleted."
            self._wait_for_you("stuck", f"The CV couldn't be written: {reason} Retry on the job's page; then press Resume "
                                        "here to attach it.")

    def _require_letter(self) -> None:
        """Tell the writer the form needs a cover letter (once)."""
        with self.store.locked():
            run = self.store.find_run(self.run_id)
            if run is None or run.letter_required or run.generate_letter:
                return
            run.letter_required = True
            self.store.update_run(run)
        feed.emit("apply", "the form requires a cover letter: asked the writer to include one")

    # ---- the loop ----

    def run(self) -> None:
        with feed.task(f"apply {self.app_id[:6]}", f"Applying: {self.job_label}"):
            try:
                self._run()
            except Exception as exc:
                logger.exception("apply session failed app=%s", self.app_id)
                message = str(exc) if isinstance(exc, SetupError) else friendly_error(exc)
                if "Executable doesn't exist" in str(exc):
                    message = "The browser engine is missing. Run: python -m playwright install chromium"
                self._save(status="failed", waiting={}, error=message)
                feed.emit("error", f"✗ {message}")
                feed.set_status("failed")
            finally:
                if self.on_finish is not None:
                    self.on_finish(self)

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SetupError("The apply assistant needs Playwright: pip install playwright, "
                             "then python -m playwright install chromium") from exc
        application = self.store.get_application(self.app_id)
        self._details = app_details.load(self.store)
        self._secrets = app_details.secrets(self._details)
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(self.browser_dir), headless=not application.show_window, viewport=VIEWPORT, accept_downloads=False)
            try:
                self.page = context.pages[0] if context.pages else context.new_page()
                context.on("page", self._follow_new_tab)
                start = application.current_url or application.start_url
                feed.emit("apply", f"opening {urlparse(start).hostname} in a private browser on this computer")
                self.page.goto(start, wait_until="domcontentloaded", timeout=45000)
                pg.settle(self.page)
                self._carry_on()
                while not self.stop_requested.is_set():
                    if self.page.is_closed():  # the site closed its tab: carry on in whichever is left
                        self.page = context.pages[-1] if context.pages else context.new_page()
                    self._handle_commands()
                    if self.stop_requested.is_set():
                        break
                    if self.mode == "agent":
                        self._safe_step()
                    elif self.mode == "documents":
                        self._check_documents()
                    elif self.mode == "paused":
                        try:
                            self._watch()
                        except Exception:
                            pass  # the page is mid-navigation; the next look will do
                    self._snap()
                    self.page.wait_for_timeout({"paused": 120, "documents": 1000}.get(self.mode, 40))
                self._snap(force=True)
            finally:
                context.close()
        application = self.store.get_application(self.app_id)
        if application is not None and application.status not in ("submitted", "failed"):
            self._save(status="stopped", waiting={})
            feed.emit("user", "■ stopped; resume any time from the job's page")

    def _follow_new_tab(self, new_page) -> None:
        self.page = new_page
        feed.emit("apply", "the site opened a new tab; following it")

    def _snap(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_frame_at < (0.25 if self.mode == "paused" else 0.5):
            return
        try:
            self.frame = pg.screenshot(self.page)
            self.frame_seq += 1
            self._last_frame_at = now
        except Exception:
            pass  # mid-navigation; the next one will do

    def _interrupted(self) -> bool:
        if self.stop_requested.is_set():
            return True
        if self.pause_requested.is_set():
            if self.mode == "agent":
                self.target = None
                self._wait_for_you("paused", PAUSED)
            return True
        return False

    # ---- one step: read the page, fill it, move on ----

    def _safe_step(self) -> None:
        """A step that fails (a page mid-navigation, an LLM error) hands back to you instead of ending the session."""
        try:
            self._step()
            self._errors = 0
        except (LLMError, ProviderUnsetError) as exc:
            self._wait_for_you("stuck", f"The LLM call failed: {exc} Fix it in Settings if needed, then press Resume.")
        except Exception as exc:
            self._errors += 1
            logger.warning("application %s: a step failed: %s", self.app_id[:6], exc, exc_info=True)
            feed.emit("warn", f"! the page changed while the assistant was working ({type(exc).__name__}); reading it again")
            pg.settle(self.page)
            if self._errors >= 3:
                self._errors = 0
                self._wait_for_you("stuck", "The assistant keeps getting interrupted on this page. Have a look, then press Resume.")

    def _step(self) -> None:
        if self._interrupted():
            return
        self._steps += 1
        if self._steps > MAX_STEPS:
            return self._wait_for_you("stuck", "That's a lot of steps for one application. Check the page, then press Resume.")
        page, application = self.page, self.store.get_application(self.app_id)
        if application is None:
            return self.stop_requested.set()
        site = planner.site_of(page.url)
        if site.endswith("linkedin.com"):
            return self._wait_for_you("site", "This part is on LinkedIn, which allows no assistant to sign in or click for "
                                              "you, so it's yours: in the view, sign in if LinkedIn asks (only the first time: "
                                              "this browser remembers you), then click Apply. The assistant carries on by "
                                              "itself as soon as you reach the company's site.", site=site, allow=False,
                                      linkedin=True)
        if site not in set(application.allowed_sites) | {planner.site_of(application.start_url)}:
            if planner.known_ats(site):
                self._allow(site, "an application system employers use")
            else:
                return self._wait_for_you("site", f"The application continued on {site}. Let the assistant fill in forms there?",
                                          site=site, allow=True)
        info = self._read()
        if self._submit_clicked and planner.confirmed(info):
            return self._done("assistant")
        banner = planner.cookie_banner(info)
        if banner and site not in self._cookie_sites:
            self._cookie_sites.add(site)
            if banner[0] == "accept-only":
                return self._wait_for_you("human", "This site's cookie banner has no way to refuse optional cookies, so "
                                                   "the assistant won't choose for you. Pick an option in the view, then press Resume.")
            feed.emit("apply", "declined optional cookies (the privacy-preserving choice)")
            pg.perform(page, planner.Action("click", banner[1]), None)
            pg.settle(page)
            info = self._read()
        block = planner.blocker(info)
        if block == "human":
            return self._wait_for_you("human", "This page wants proof that you're human. Solve it in the view, then press Resume.")
        if block == "verify":
            return self._wait_for_you("human", "The site sent you an email to confirm. Follow its link or enter its code in "
                                               "the view, then press Resume.")
        if block == "account":
            return self._account_needed(info)
        if self._no_progress(info):
            return self._wait_for_you("stuck", "The assistant isn't getting further on this page. Have a look (there may be "
                                               "an error to fix), then press Resume.")

        docs = self.documents()
        files = docs or {"cv": "", "letter": ""}  # until the CV exists, everything but the uploads
        actions, _ = planner.by_rule(info, self._details, files)
        missed_ids, missed_labels = set(), set()  # fields that couldn't be filled: asked about below, never skipped
        if actions:
            missed_labels = {f.get("label") or f["id"] for f in self._act(actions, info)}  # ids change on the next read
            if self._interrupted():
                return
            info = self._read()
        pending = planner.pending_documents(info)
        letter_needed = pending["letter"] is not None and pending["letter"].get("required")
        if letter_needed and docs is None:
            self._require_letter()
        elif letter_needed and not docs.get("letter"):
            return self._wait_for_you("letter", "This application requires a cover letter, but this version has none. Write "
                                                "one now (it adds a new version with a letter), or attach your own in the view.")
        if docs is None and (pending["cv"] is not None or letter_needed):
            return self._wait_for_documents()
        rule_actions, questions = planner.by_rule(info, self._details, files)
        fields = planner.for_llm(info, handled={a.id for a in rule_actions})
        navigation = self._navigation_by_rule(info)
        plan = self._ask_llm(info, fields) if fields or navigation is None else planner.Plan(*navigation)
        if self._interrupted():
            return
        missed_ids = {f["id"] for f in self._act(plan.actions, info)}
        if self._interrupted():
            return
        questions += plan.ask
        asked = {q["id"] for q in questions}
        questions += [planner.ask_failed(f) for f in info["fields"] if not f.get("filled") and f["id"] not in asked
                      and (f["id"] in missed_ids or (f.get("label") or f["id"]) in missed_labels)]
        # Never press on with a required field still empty: whatever the LLM thinks, ask instead.
        decided = {a.id for a in plan.actions} | {q["id"] for q in questions}
        questions += [planner.ask_required(f) for f in fields if f.get("required") and f["id"] not in decided]
        if questions:
            return self._wait_for_you("question", "A few questions need your answer.", questions=questions)
        if not plan.actions and plan.next in ("wait", "done"):
            return self._wait_for_you("stuck", "The assistant isn't sure how to continue on this page. Have a look, then "
                                               "press Resume (or finish it yourself in the view).")
        self._move_on(plan, info)

    def _read(self) -> dict:
        info = pg.observe(self.page)
        self.last_observation = info
        self._save(current_url=self.page.url)
        empty = sum(not f.get("filled") for f in info["fields"])
        feed.emit("apply", f"read {re.sub(r'[?#].*$', '', self.page.url)[:80]} · {len(info['fields'])} fields "
                           f"({empty} empty) · {len(info['buttons'])} buttons")
        return info

    def _no_progress(self, info: dict) -> bool:
        signature = (info["url"], tuple((f["label"], f.get("filled")) for f in info["fields"]),
                     tuple(b["text"] for b in info["buttons"]), tuple(info.get("errors") or []))
        self._same_page = self._same_page + 1 if signature == self._last_signature else 0
        self._last_signature = signature
        return self._same_page >= MAX_SAME_PAGE

    def _navigation_by_rule(self, info: dict) -> tuple | None:
        found = planner.navigation(info)
        return ([], [], found[0], found[1], "") if found else None

    def _ask_llm(self, info: dict, fields: list) -> planner.Plan:
        profile = self.store.get_profile()
        model = self.get_model()
        if isinstance(model, PrivateModel):
            model = model.with_extra(self._secrets)
        prompt = planner.build_prompt(info, fields, _prompt_profile(_to_plain(profile)) if profile else "{}", self._details,
                                      self.job_label)
        data = complete_json(model, prompt)
        allowed = allowed_facts_text(profile, self.job_context) if profile else ""
        plan = planner.parse_plan(data, info, fields, check_text=lambda text: filter_text(text, [], None, "application", allowed))
        if plan.note:
            feed.emit("apply", f"llm: {self._shown(plan.note)}")
        return plan

    def _act(self, actions: list, info: dict) -> list[dict]:
        """Do `actions` on the page; returns the fields that couldn't be filled, so you're asked about them."""
        fields = {f["id"]: f for f in info["fields"]}
        filled: list[dict] = []
        failed: list[dict] = []
        for action in actions:
            if self._interrupted():
                break
            field = fields.get(action.id)
            self.target = (field or {}).get("box")
            what = action.label or "a field"
            try:
                pg.perform(self.page, action, field)
            except Exception as exc:
                feed.emit("warn", f"! couldn't fill “{what[:60]}” ({type(exc).__name__}); you'll be asked about it")
                if field is not None:
                    failed.append(field)
                continue
            shown = self._shown(action.shown or action.value)
            feed.emit("apply", f"{what[:60]} ← {shown[:80]}" + (" · by rule" if action.source == "rule" else ""))
            self.page.wait_for_timeout(polite.level()["field_pause_ms"])
            filled.append({"page": info.get("title") or "", "label": what, "value": action.value if action.op != "upload"
                           else action.shown, "source": action.source})
            self._snap(force=True)
        self.target = None
        if filled:
            with self.store.locked():
                application = self.store.get_application(self.app_id)
                if application is not None:
                    # One row per field: filling a page again (after a restart, say) updates it, so the review stays short.
                    latest = {(f["page"], f["label"]): f for f in application.filled + filled}
                    application.filled = list(latest.values())[-300:]
                    self.store.update_application(application)
        return failed

    def _move_on(self, plan: planner.Plan, info: dict) -> None:
        buttons = {b["id"]: b for b in info["buttons"]}
        if plan.next == "click" and plan.next_id in buttons:
            button = buttons[plan.next_id]
            self.target = button.get("box")
            feed.emit("apply", f"click “{button['text']}”")
            pg.perform(self.page, planner.Action("click", plan.next_id), None)
            pg.settle(self.page)
            self.page.wait_for_timeout(polite.level()["page_pause_ms"])
            self.target = None
        elif plan.next == "ready_to_submit":
            button = buttons.get(plan.next_id, {"text": "Submit"})
            self._wait_for_you("submit", "Everything is filled in. Check the summary and the page, then approve the submit.",
                               target=plan.next_id, button=button["text"])
        elif plan.next == "account":
            self._account_needed(info)
        elif plan.next == "human":
            self._wait_for_you("human", "This page needs a person. Do what it asks in the view, then press Resume.")

    # ---- accounts ----

    def _saved_account(self, site: str) -> dict | None:
        """Your saved account for this page (Settings → Application details) and its password, if there is one."""
        keys = list(dict.fromkeys([accounts.account_key(self.page.url) if self.page is not None else site, site]))
        for key in keys:
            for a in accounts.list_accounts(self.store):
                if a.get("site") == key and a.get("in_keychain"):
                    password = accounts.get_password(key, a["email"])
                    if password:
                        return {"email": a["email"], "password": password, "key": key}
        email = self._details["standard"].get("email", "")
        for key in keys:
            password = accounts.get_password(key, email) if email else None
            if password:
                return {"email": email, "password": password, "key": key}
        return None

    def _account_needed(self, info: dict) -> None:
        site = planner.site_of(self.page.url)
        heading = " ".join(info.get("headings") or []) + " " + (info.get("title") or "")
        creating = info["password_fields"] >= 2 or bool(re.search(r"create|sign ?up|register", heading, re.I))
        saved = self._saved_account(site)
        # Like a password manager: your own saved account, on its own site only, at most twice (email-first sign-ins
        # take two pages), never to create an account (its terms are yours to accept) and never on LinkedIn.
        if saved and not creating and self._details.get("auto_sign_in", True) and self._signed_in.get(site, 0) < 2 \
                and not site.endswith("linkedin.com"):
            self._signed_in[site] = self._signed_in.get(site, 0) + 1
            return self._sign_in(info, site, saved)
        self._wait_for_you("account", f"{site} wants you to {'create an account' if creating else 'sign in'}.", site=site,
                           creating=creating, saved=bool(saved), keychain=accounts.keychain_name() if accounts.can_store() else "")

    def _sign_in(self, info: dict, site: str, saved: dict) -> None:
        users = [f for f in info["fields"] if f["kind"] in ("email", "text") and not f.get("filled")
                 and re.search(r"e-?mail|user ?name|login|account|sign", f["label"], re.I)]
        passwords = [f for f in info["fields"] if f["kind"] == "password"]
        if not users and not passwords:
            return self._wait_for_you("account", f"{site} wants you to sign in, and its form wasn't recognised. Sign in in "
                                                 "the view, then press Resume.", site=site, creating=False, saved=True, keychain="")
        for f in users[:1]:
            pg.perform(self.page, planner.Action("fill", f["id"], saved["email"]), f)
        for f in passwords[:1]:
            pg.perform(self.page, planner.Action("fill", f["id"], saved["password"]), f)
        button = next((b for b in info["buttons"] if planner.SIGN_IN_BUTTON.fullmatch(b["text"].strip())), None)
        feed.emit("apply", f"signing in to {site} with your saved account (password from "
                           f"{accounts.keychain_name() if accounts.can_store() else 'your password store'})")
        if button is not None:
            pg.perform(self.page, planner.Action("click", button["id"]), None)
        elif passwords:
            self.page.keyboard.press("Enter")
        pg.settle(self.page)
        self.page.wait_for_timeout(polite.level()["page_pause_ms"])
        self._snap(force=True)

    def _fill_account(self, use_saved: bool) -> None:
        info = self._read()
        site, email = planner.site_of(self.page.url), self._details["standard"].get("email", "")
        if use_saved and self._saved_account(site):
            email = self._saved_account(site)["email"]
        emails = [f for f in info["fields"] if f["kind"] in ("email", "text") and re.search(r"e-?mail|user ?name|login", f["label"], re.I)]
        passwords = [f for f in info["fields"] if f["kind"] == "password"]
        if not email or not emails or not passwords:
            return self._wait_for_you("account", "The assistant couldn't find the sign-in fields. Do it in the view, then "
                                                 "press Resume.", site=site, creating=False, saved=False, keychain="")
        if use_saved:
            password = (self._saved_account(site) or {}).get("password") or ""
        else:
            key = accounts.account_key(self.page.url)  # a new account belongs to this employer's own address
            password = accounts.generate_password()
            stored = accounts.store_password(key, email, password)
            accounts.remember_account(self.store, key, email, stored)
            self.new_password = "" if stored else password
        for f in emails:
            pg.perform(self.page, planner.Action("fill", f["id"], email), f)
        for f in passwords:
            pg.perform(self.page, planner.Action("fill", f["id"], password), f)
        where = accounts.keychain_name() if accounts.can_store() else "nowhere: copy it now"
        feed.emit("apply", f"filled the {'sign-in' if use_saved else 'sign-up'} form · password {'from' if use_saved else 'saved to'} {where}")
        self._snap(force=True)
        self._wait_for_you("account_check", "Check the form, tick the site's terms only if you agree to them, then press the "
                                            "site's own button in the view. Press Resume once you're through.",
                           site=site, new_password=bool(self.new_password))

    # ---- commands from you ----

    def _handle_commands(self) -> None:
        if self.pause_requested.is_set() and self.mode != "paused":
            self.target = None
            self._wait_for_you("paused", PAUSED)
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._command(command)
            except Exception as exc:
                feed.emit("warn", f"! that didn't work ({type(exc).__name__})")
                logger.warning("application %s: “%s” failed: %s", self.app_id[:6], command.get("action"), exc, exc_info=True)
            self._snap(force=True)

    def _command(self, command: dict) -> None:
        action = command.get("action")
        if action == "resume":
            self.new_password = ""
            feed.emit("user", "▶ resumed")
            self._carry_on()
        elif action == "input" and self.mode == "paused":
            self._input(command)
        elif action == "nav":  # the view's Back / Forward / Reload / address bar: you take over, then it happens
            if self.mode != "paused":
                self.target = None
                self._wait_for_you("paused", PAUSED)
            self._input(command)
        elif action == "answer":
            self._answer(command.get("answers") or [])
        elif action == "allow_site":
            site = planner.site_of(self.page.url)
            with self.store.locked():
                application = self.store.get_application(self.app_id)
                if application is not None and site not in application.allowed_sites:
                    application.allowed_sites.append(site)
                    self.store.update_application(application)
            feed.emit("user", f"✓ allowed {site}")
            self._carry_on()
        elif action == "account_fill":
            self._fill_account(use_saved=bool(command.get("saved")))
        elif action == "approve_submit":
            self._approve_submit()
        elif action == "await_documents":  # a new version (with a letter) is being written for this application
            self._wait_for_documents()
        elif action == "mark_submitted":
            self._done("you")

    def _input(self, command: dict) -> None:
        """You, driving the browser from the live view."""
        kind, page = command.get("type"), self.page
        if kind == "click":
            page.mouse.click(float(command["x"]), float(command["y"]))
        elif kind == "type":
            page.keyboard.type(str(command.get("text", ""))[:2000])
        elif kind == "key" and re.fullmatch(r"[A-Za-z0-9+]{1,30}", str(command.get("key", ""))):
            page.keyboard.press(command["key"])
        elif kind == "scroll":
            page.mouse.wheel(0, max(-3000, min(3000, float(command.get("dy", 0)))))
        elif kind == "back":
            page.go_back(timeout=15000)
        elif kind == "forward":
            page.go_forward(timeout=15000)
        elif kind == "reload":
            page.reload(timeout=30000)
        elif kind == "goto":
            url = str(command.get("url", "")).strip()
            if urlparse(url).scheme in ("http", "https") and urlparse(url).netloc:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(250)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        self._save(current_url=page.url)  # so the address above the view follows what you do

    def _answer(self, answers: list) -> None:
        fields = {f["id"]: f for f in (self.last_observation or {}).get("fields", [])}
        actions = []
        for item in answers:
            field, value = fields.get(str(item.get("id"))), str(item.get("value") or "").strip()
            if field is None or not value:
                continue
            if field.get("kind") == "file":
                docs = self.documents()
                if value.casefold() in ("yes", "true", "on") and docs:
                    actions.append(planner.Action("upload", field["id"], docs["cv"], field.get("label", ""), "you",
                                                  "your tailored CV"))
                continue
            if field.get("kind") == "checkbox":
                actions.append(planner.Action("check" if value.casefold() in ("yes", "true", "on") else "uncheck",
                                              field["id"], "", field.get("label", ""), "you", "ticked by you"))
            elif field.get("kind") in planner.CHOICE_KINDS:
                option = planner.pick_option(value, field.get("options") or []) or value
                actions.append(planner.Action("choose", field["id"], option, field.get("label", ""), "you", option))
            else:
                actions.append(planner.Action("fill", field["id"], value, field.get("label", ""), "you", value))
            if item.get("save") and field.get("label"):
                app_details.remember_answer(self.store, field["label"], value)
        self._details = app_details.load(self.store)
        self._secrets = app_details.secrets(self._details)
        feed.emit("user", f"✓ you answered {len(actions)} question{'s' if len(actions) != 1 else ''}")
        self._act(actions, self.last_observation or {"fields": []})
        self._carry_on()

    def _approve_submit(self) -> None:
        application = self.store.get_application(self.app_id)
        if application is None or (application.waiting or {}).get("kind") != "submit":
            return
        target = application.waiting.get("target", "")
        feed.emit("user", f"✓ you approved: {application.waiting.get('button', 'Submit')}")
        self._submit_clicked = True
        pg.perform(self.page, planner.Action("click", target), None)
        pg.settle(self.page)
        info = self._read()
        if planner.confirmed(info):
            return self._done("assistant")
        self._wait_for_you("check", "Submitted, but the site hasn't confirmed it yet. Check the page: if the application "
                                    "went through, mark it as submitted; if there's an error, fix it in the view and Resume.")

    def _done(self, by: str) -> None:
        self._save(status="submitted", waiting={}, submitted_at=datetime.now(timezone.utc).isoformat(), submitted_by=by)
        feed.emit("ok", "✓ application submitted" + (" (marked by you)" if by == "you" else ""))
        self.stop_requested.set()


class Sessions:
    """The running assistants (one at a time), and the last screenshot of each finished one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, ApplySession] = {}
        self._last_frames: dict[str, bytes] = {}

    def start(self, session: ApplySession) -> str:
        with self._lock:
            busy = [s for s in self._running.values() if s.is_alive()]
            if busy:
                return "Another application is in progress. Stop it first."
            self._running[session.app_id] = session
        session.on_finish = self._finished
        session.start()
        return ""

    def _finished(self, session: ApplySession) -> None:
        with self._lock:
            if session.frame:
                self._last_frames[session.app_id] = session.frame
            self._running.pop(session.app_id, None)

    def get(self, app_id: str) -> ApplySession | None:
        with self._lock:
            return self._running.get(app_id)

    def frame(self, app_id: str) -> bytes:
        session = self.get(app_id)
        with self._lock:
            return session.frame if session and session.frame else self._last_frames.get(app_id, b"")

    def stop_all(self, wait: float = 10.0) -> None:
        with self._lock:
            running = list(self._running.values())
        for session in running:
            session.send({"action": "stop"})
        for session in running:
            session.join(timeout=wait)
