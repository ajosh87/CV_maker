"""Per-job engine, split at the questions step into two LangGraph graphs.

analyze:  load inputs -> extract requirements -> match & gaps        => run.status needs_answers
generate: load inputs -> apply answers -> rewrite -> export -> letter => run.status ready

Every input is read from the Store and every output written back to it, so nothing depends on
in-memory checkpoints: a server restart or a failed attempt never leaves a run stuck, and Retry
simply runs the graph again. Any node error ends the graph and is recorded on the run.
"""
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from langgraph.graph import END, StateGraph

from cv_maker import ats
from cv_maker.events import feed
from cv_maker.export.docx_cv import ExportError, write_cv_docx
from cv_maker.export.letter import write_letter_docx
from cv_maker.honesty import CvDocument, FactCheck, allowed_facts_text, contact_line, filter_cv_document, filter_text
from cv_maker.jobs.match import Question, match_profile
from cv_maker.jobs.requirements import JobRequirements, extract_requirements
from cv_maker.llm.chat import ChatModel, LLMError, ProviderUnsetError, complete_json
from cv_maker.models import Education, Experience, JobRun
from cv_maker.pipeline.state import TailorState
from cv_maker.profile.answers import answers_from_dicts, apply_answers, levels_of
from cv_maker.store import Store, _profile_from_dict

logger = logging.getLogger("cv_maker.pipeline")


class PipelineInputError(Exception):
    """Something the user has to supply first (CV, job description, answers)."""


@dataclass
class PipelineContext:
    store: Store
    get_model: Callable[[], ChatModel]
    output_dir: Path


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, (ProviderUnsetError, LLMError, PipelineInputError)):
        return str(exc)
    if isinstance(exc, ExportError):
        return f"Could not write the DOCX file: {exc}"
    return f"Unexpected error ({type(exc).__name__}): {exc}"


def _to_plain(obj):
    if hasattr(obj, "__dict__"):
        return {k: _to_plain(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    return obj


def _cv_document_from_plain(data: dict) -> CvDocument:
    return CvDocument(
        name=data.get("name", ""),
        summary=data.get("summary", ""),
        experiences=[Experience(**e) for e in data.get("experiences", [])],
        education=[Education(**e) for e in data.get("education", [])],
        skills=list(data.get("skills", [])),
        contact=list(data.get("contact", [])),
        headline=data.get("headline", ""),
        extras=dict(data.get("extras", {})),
    )


def _questions(state: TailorState) -> list[Question]:
    return [
        Question(term=q.get("term", ""), prompt=q.get("prompt", ""), kind=q.get("kind", "yesno"), required=q.get("required", False),
                 why=q.get("why", ""), score=q.get("score", 0), optional=q.get("optional", False), context=q.get("context", ""))
        for q in state.get("questions", [])
    ]


# Never sent for writing: the CV header and the letter's sign-off are filled in on this computer.
_LOCAL_ONLY = {"name", "email", "phone", "location", "links", "target_role"}
_PROMPT_EXTRAS = ("certifications", "projects", "languages")


def _prompt_profile(profile: dict) -> str:
    slim = {k: v for k, v in profile.items() if k not in _LOCAL_ONLY}
    slim["experiences"] = [{k: v for k, v in e.items() if k != "confirmed"} for e in profile.get("experiences", [])]
    slim["skills"] = [s["name"] for s in profile.get("skills", [])]
    levels = {s["name"]: s["level"] for s in profile.get("skills", []) if s.get("level")}
    if levels:
        slim["skill_levels"] = levels  # what the candidate said about their level: write at it, never above
    slim["extras"] = {k: v for k, v in (profile.get("extras") or {}).items() if k in _PROMPT_EXTRAS}
    return json.dumps(slim, ensure_ascii=False, indent=1)


_CLOSINGS = {"kind regards", "best regards", "warm regards", "regards", "sincerely", "yours sincerely",
             "yours faithfully", "yours truly", "best", "best wishes", "thank you", "many thanks", "with thanks",
             "respectfully", "with best regards", "cheers"}


def _sign_off(body: str, name: str) -> str:
    """End the letter with a closing and the candidate's name, which the model never saw."""
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    if not paragraphs or not name:
        return body
    lines = paragraphs[-1].splitlines()

    def name_slot(line: str) -> bool:  # what the model wrote where the name goes: "[Your Name]", a stray name
        line = line.strip()
        return (len(line) <= 40 and re.fullmatch(r"[\[{(<].*[\]})>]", line) is not None) or \
            line.casefold() in {name.casefold(), "your name", "candidate", "the candidate"}

    while lines and name_slot(lines[-1]):
        lines.pop()
    closing = lines[-1].strip().rstrip(",.!").casefold() if lines else ""
    if lines and closing in _CLOSINGS:
        paragraphs[-1] = "\n".join(lines + [name])
    else:
        paragraphs = paragraphs[:-1] + (["\n".join(lines)] if lines else []) + [f"Kind regards,\n{name}"]
    return "\n\n".join(paragraphs)


# ---- analyze --------------------------------------------------------------------------------

def load_for_analysis(state: TailorState, ctx: PipelineContext) -> dict:
    profile = ctx.store.get_profile()
    if profile is None:
        raise PipelineInputError("No CV profile yet. Upload your CV first.")
    run = ctx.store.get_run(state["run_id"])
    if not run.jd_text.strip():
        raise PipelineInputError("This job has no description yet. Paste it to continue.")
    return {"profile": _to_plain(profile), "jd_text": run.jd_text}


def extract_requirements_node(state: TailorState, ctx: PipelineContext) -> dict:
    reqs = extract_requirements(state["jd_text"], ctx.get_model())
    logger.info("extract_requirements: run=%s must_have=%d nice_to_have=%d", state["run_id"], len(reqs.must_have), len(reqs.nice_to_have))
    return {"requirements": reqs.to_dict()}


def match_and_gaps_node(state: TailorState, ctx: PipelineContext) -> dict:
    result = match_profile(_profile_from_dict(state["profile"]), JobRequirements.from_dict(state["requirements"]))
    logger.info("match_and_gaps: run=%s gaps=%d questions=%d", state["run_id"], len(result.gaps), len(result.questions))
    return {
        "gaps": [g.to_dict() for g in result.gaps],
        "questions": [q.to_dict() for q in result.questions],
    }


# ---- generate -------------------------------------------------------------------------------

def load_for_generation(state: TailorState, ctx: PipelineContext) -> dict:
    profile = ctx.store.get_profile()
    if profile is None:
        raise PipelineInputError("No CV profile yet. Upload your CV first.")
    run = ctx.store.get_run(state["run_id"])
    if not run.requirements:
        raise PipelineInputError("This job has not been analyzed yet. Click Retry.")
    return {
        "profile": _to_plain(profile),
        "jd_text": run.jd_text,
        "requirements": run.requirements,
        "questions": run.questions,
        "answers": run.answers,
        "generate_letter": run.generate_letter,
        "version": ctx.store.next_version(run.id),
        "job_meta": f"{run.title} {run.company}",
        "job_title": run.title or (run.requirements or {}).get("job_title", ""),
        "company": run.company,
        # Improving a version from its ATS check: what you picked and confirmed, for the writer to act on.
        "improve_focus": (run.improve or {}).get("focus", []) if (run.improve or {}).get("status") == "writing" else [],
    }


def apply_answers_node(state: TailorState, ctx: PipelineContext) -> dict:
    answers = answers_from_dicts(state.get("answers", []))
    with ctx.store.locked():  # read the profile fresh so concurrent jobs/edits aren't overwritten
        profile = ctx.store.get_profile()
        if profile is None:
            raise PipelineInputError("Your profile was deleted. Add your CV again to continue.")
        updated = apply_answers(profile, _questions(state), answers)
        if updated != profile:
            ctx.store.save_profile(updated)  # confirmed clarifications become part of the master profile
    # Anything the job asks for that the (updated) profile doesn't show as this very skill must not be claimed:
    # related isn't enough ("Docker" isn't "Kubernetes"), and "no experience" stands unless your CV shows it plainly.
    result = match_profile(updated, JobRequirements.from_dict(state["requirements"]), levels_of(answers))
    declined = {a.term for a in answers if a.level == "none" or (not a.is_yes and not a.skipped)}
    banned = [g.term for g in result.gaps
              if (not g.same and not g.level) or (g.term in declined and g.strength != "strong")]
    return {"profile": _to_plain(updated), "banned_terms": banned, "gaps": [g.to_dict() for g in result.gaps]}


_REWRITE_PROMPT = """You are tailoring a CV to one job so it ranks well in applicant tracking systems (ATS) and reads well
to a recruiter. Use ONLY facts from the candidate profile below. Never add employers, titles, dates, degrees, numbers,
metrics, tools or skills that are not in the profile.
{banned_line}
{keyword_plan}
{level_rules}
{improve_lines}
Job description (use it to choose emphasis, ordering and wording):
<<<
{jd}
>>>

Candidate profile (the only allowed facts):
{profile}

Return one JSON object:
{{
  "summary": "3 sentences grounded strictly in the profile: open with the target role ({job_title}) or the candidate's closest real title, then their strongest matching experience, using 3-5 of the keywords above in the posting's wording",
  "experiences": [{{"company": "exactly as in the profile", "title": "exactly as in the profile",
                    "bullets": ["one rewritten bullet per original bullet, same count and same order: start with a strong action verb, lead with the result where there is one, keep every number unchanged, and use the posting's wording for a skill when the fact is the same"]}}],
  "skills": ["skills from the profile only, written as the posting writes them when it is the same skill, most relevant to this job first"]
}}
Include every experience from the profile, in the same order. Plain text only: no tables, emoji or decorative symbols."""

_LEVEL_RULES = ("Write each skill in skill_levels at the level the candidate gave: beginner = \"familiar with\" or "
                "\"foundational knowledge of\"; intermediate = \"working knowledge of\" or \"hands-on experience with\"; "
                "expert = \"expert in\" or \"deep expertise in\". Never describe a skill above its level.")


def rewrite_cv_node(state: TailorState, ctx: PipelineContext) -> dict:
    banned = state.get("banned_terms", [])
    banned_line = f"Do not mention these, the candidate has not confirmed them: {', '.join(banned)}." if banned else ""
    keyword_plan = ats.plan(state.get("requirements") or {}, state.get("gaps") or [])
    has_levels = any(s.get("level") for s in state["profile"].get("skills", []))
    focus = state.get("improve_focus") or []
    improve_lines = ("This version improves on an earlier one from its ATS check. The candidate asked for, and confirmed the "
                     "facts behind, these changes:\n" + "\n".join(f"- {line}" for line in focus)) if focus else ""
    prompt = _REWRITE_PROMPT.format(banned_line=banned_line, keyword_plan=ats.prompt_lines(keyword_plan),
                                    level_rules=_LEVEL_RULES if has_levels else "", improve_lines=improve_lines,
                                    job_title=ats.clean_title(state.get("job_title", ""), state.get("company", "")) or "the posting's title",
                                    jd=state.get("jd_text", "")[:15000], profile=_prompt_profile(state["profile"]))
    data = complete_json(ctx.get_model(), prompt)
    draft = CvDocument(
        name="",
        summary=str(data.get("summary") or ""),
        experiences=[
            Experience(str(e.get("company", "")), str(e.get("title", "")), "", "", None, False, [str(b) for b in e.get("bullets") or []], True)
            for e in data.get("experiences") or []
            if isinstance(e, dict)
        ],
        education=[],
        skills=[s for s in data.get("skills") or [] if s],
    )
    check = FactCheck()
    context = f"{state.get('job_meta', '')}\n{state.get('jd_text', '')}"
    filtered = filter_cv_document(draft, _profile_from_dict(state["profile"]), banned, check, context)
    # Every keyword you have, in the posting's wording; the CV titled for the job; then the ATS check on the result.
    optimised = ats.optimise(filtered, keyword_plan, state.get("job_title", ""), state.get("company", ""))
    result = ats.check(_to_plain(optimised), keyword_plan, state.get("job_title", ""), state.get("company", ""))
    logger.info("rewrite_cv: run=%s summary %d->%d chars, fact-check items=%d, ATS %d",
                state["run_id"], len(draft.summary), len(filtered.summary), len(check.items), result["score"])
    feed.emit("step", f"ATS check: {result['score']}/100 ({result['grade'].lower()}) · "
                      f"{sum(k['in_cv'] for k in result['keywords'] if k['required'])} of "
                      f"{sum(1 for k in result['keywords'] if k['required'])} must-have keywords in the CV")
    return {"cv_document": _to_plain(optimised), "fact_check": check.to_dict(), "ats": result}


def export_docx_node(state: TailorState, ctx: PipelineContext) -> dict:
    path = ctx.output_dir / f"cv_{state['run_id']}_v{state.get('version', 1)}.docx"
    write_cv_docx(_cv_document_from_plain(state["cv_document"]), path)
    # The check counted the CV's text; now read the file the employer gets, the way a parser does, to confirm it.
    result = dict(state.get("ats") or {})
    if result:
        result["file"] = ats.read_back(str(path), state["cv_document"], result)
        if not result["file"]["ok"]:
            bad = [i["label"] for i in result["file"]["items"] if not i["ok"]]
            feed.emit("warn", f"! the DOCX doesn't read back as the check counted it: {', '.join(bad)}")
    return {"output_cv_path": str(path), "ats": result}


_LETTER_PROMPT = """Write a cover letter (250-350 words, 3-4 short paragraphs) for this job.
Use ONLY facts from the candidate profile; never invent experience, numbers or skills.
{banned_line}
Never leave template gaps like [Company]; if the company is unknown, say "your team".
End with a closing such as "Kind regards," and nothing after it: the candidate's name is added automatically.

Job description:
<<<
{jd}
>>>

Candidate profile:
{profile}

Return one JSON object: {{"letter": "the full letter, paragraphs separated by blank lines"}}"""


def optional_letter_node(state: TailorState, ctx: PipelineContext) -> dict:
    # Read now, not at the start: an application being filled in alongside may have found the form requires one.
    run_now = ctx.store.find_run(state["run_id"])
    required = bool(run_now and run_now.letter_required)
    if not (state.get("generate_letter") or required):
        return {}
    if required and not state.get("generate_letter"):
        feed.emit("step", "writing a cover letter too: the application form requires one")
    banned = state.get("banned_terms", [])
    try:
        banned_line = f"Do not mention: {', '.join(banned)}." if banned else ""
        prompt = _LETTER_PROMPT.format(banned_line=banned_line, jd=state.get("jd_text", "")[:15000], profile=_prompt_profile(state["profile"]))
        letter = str(complete_json(ctx.get_model(), prompt).get("letter") or "").strip()
        check = FactCheck()
        profile = _profile_from_dict(state["profile"])
        allowed = allowed_facts_text(profile, f"{state.get('job_meta', '')}\n{state.get('jd_text', '')}")
        paragraphs = [filter_text(p, banned, check, "letter", allowed) for p in letter.split("\n\n")]
        body = "\n\n".join(p for p in paragraphs if p)
        if not body:
            raise LLMError("The model returned an empty letter.")
        body = _sign_off(body, profile.name)
        path = ctx.output_dir / f"letter_{state['run_id']}_v{state.get('version', 1)}.docx"
        write_letter_docx(body, path, name=profile.name, contact=contact_line(profile))
        return {"letter_path": str(path), "letter_text": body, "letter_fact_check": check.to_dict()}
    except Exception as exc:  # the CV is the main deliverable; a letter failure is only a warning
        logger.warning("job %s: the cover letter failed: %s", state["run_id"][:6], exc, exc_info=True)
        return {"warning": f"Cover letter not created: {friendly_error(exc)}"}


# ---- wiring ---------------------------------------------------------------------------------

ANALYZE_STEPS = [
    ("load_inputs", load_for_analysis, "Loading profile and job…"),
    ("extract_requirements", extract_requirements_node, "Reading the job's requirements…"),
    ("match_and_gaps", match_and_gaps_node, "Matching requirements against your CV…"),
]
GENERATE_STEPS = [
    ("load_inputs", load_for_generation, "Loading answers…"),
    ("apply_answers", apply_answers_node, "Applying your answers…"),
    ("rewrite_cv", rewrite_cv_node, "Writing the tailored CV…"),
    ("export_docx", export_docx_node, "Saving the DOCX…"),
    ("optional_letter", optional_letter_node, "Writing the cover letter…"),
]


def _set_step(store: Store, run_id: str, label: str) -> None:
    feed.emit("step", label.rstrip("…").lower())
    with store.locked():
        run = store.find_run(run_id)
        if run is not None:
            run.step = label
            store.update_run(run)


def _build(ctx: PipelineContext, steps):
    def wrap(fn, label):
        def node(state: TailorState) -> dict:
            _set_step(ctx.store, state["run_id"], label)
            try:
                return fn(state, ctx) or {}
            except Exception as exc:
                logger.warning("job %s: %s failed: %s", (state.get("run_id") or "")[:6], label.rstrip("…").lower(), exc,
                               exc_info=True)
                return {"error": friendly_error(exc)}
        return node

    g = StateGraph(TailorState)
    for name, fn, label in steps:
        g.add_node(name, wrap(fn, label))
    g.set_entry_point(steps[0][0])
    for (name, _, _), (nxt, _, _) in zip(steps, steps[1:]):
        g.add_conditional_edges(name, lambda s, nxt=nxt: END if s.get("error") else nxt, [nxt, END])
    g.add_edge(steps[-1][0], END)
    return g.compile()


class Pipeline:
    def __init__(
        self,
        store: Store,
        get_model: Callable[[], ChatModel],
        output_dir: Path,
        describe_model: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self._describe_model = describe_model or (lambda: "")
        ctx = PipelineContext(store=store, get_model=get_model, output_dir=output_dir)
        self._analyze = _build(ctx, ANALYZE_STEPS)
        self._generate = _build(ctx, GENERATE_STEPS)

    def _invoke(self, graph, run_id: str) -> dict:
        try:
            return graph.invoke({"run_id": run_id})
        except Exception as exc:
            logger.exception("pipeline crashed run=%s", run_id)
            return {"error": friendly_error(exc)}

    def _fail(self, run: JobRun, stage: str, error: str) -> JobRun:
        run.status, run.failed_stage, run.error, run.step = "failed", stage, error, ""
        self.store.update_run(run)
        feed.emit("error", f"✗ {'analysis' if stage == 'analyze' else 'writing'} failed: {error}")
        feed.set_status("failed")
        return run

    def analyze(self, run_id: str) -> JobRun | None:
        out = self._invoke(self._analyze, run_id)
        with self.store.locked():
            run = self.store.find_run(run_id)
            if run is None:
                logger.info("analyze: run=%s was deleted while working; result discarded", run_id)
                return None
            if out.get("error"):
                return self._fail(run, "analyze", out["error"])
            reqs = out["requirements"]
            run.requirements, run.gaps, run.questions, run.answers = reqs, out["gaps"], out["questions"], []
            run.title = run.title or reqs.get("job_title", "")
            run.company = run.company or reqs.get("company", "")
            run.status, run.error, run.failed_stage, run.step = "needs_answers", "", "", ""
            self.store.update_run(run)
            asks = len(run.questions)
            feed.emit("ok", f"✓ {len(reqs.get('must_have') or [])} must-haves read · "
                            + (f"{asks} question{'s' if asks != 1 else ''} for you" if asks else "nothing to ask"))
            return run

    def generate(self, run_id: str) -> JobRun | None:
        out = self._invoke(self._generate, run_id)
        with self.store.locked():
            return self._finish_generation(run_id, out)

    def _finish_generation(self, run_id: str, out: dict) -> JobRun | None:
        run = self.store.find_run(run_id)
        if run is None:
            # Deleted while the CV was being written: don't leave its files behind.
            for path in (out.get("output_cv_path"), out.get("letter_path")):
                if path:
                    Path(path).unlink(missing_ok=True)
            logger.info("generate: run=%s was deleted while working; files removed", run_id)
            return None
        if out.get("cv_document"):
            run.draft = out["cv_document"]  # kept even if export fails (spec)
        if out.get("error"):
            return self._fail(run, "generate", out["error"])
        # Each generation is kept as a new version; the run points at the latest files.
        version = out.get("version", 1)
        run.output_cv_path = out.get("output_cv_path", "")
        run.letter_path = out.get("letter_path", "")
        run.warning = out.get("warning", "")
        run.last_version = max(run.last_version, version)
        run.status, run.error, run.failed_stage, run.step = "ready", "", "", ""
        self.store.update_run(run)
        # Everything needed to show the user exactly what was written and what the fact check changed.
        meta = {
            "answers": run.answers,
            "letter_requested": run.generate_letter,
            "model": self._describe_model(),
            "draft": out.get("cv_document") or {},
            "fact_check": out.get("fact_check") or {},
            "ats": out.get("ats") or {},
        }
        improving = run.improve or {}
        if improving.get("status") == "writing":  # written to improve an earlier version: say which, and what for
            meta["improved_from"] = improving.get("version")
            meta["improve"] = {"items": [{"id": i["id"], "label": i["label"], "group": i.get("group", "")}
                                         for i in (improving.get("plan") or {}).get("items") or []]}
            run.improve = {"status": "done", "version": version, "base_version": improving.get("version")}
            self.store.update_run(run)
        self.store.add_document(run, "cv", version, run.output_cv_path, meta)
        if run.letter_path:
            self.store.add_document(run, "letter", version, run.letter_path, {
                "model": meta["model"], "text": out.get("letter_text", ""), "fact_check": out.get("letter_fact_check") or {},
                "why": "" if run.generate_letter else "the application form requires one",
            })
        changed = len((out.get("fact_check") or {}).get("items") or []) + len((out.get("letter_fact_check") or {}).get("items") or [])
        score = (out.get("ats") or {}).get("score")
        feed.emit("ok", f"✓ v{version} written: CV{' + letter' if run.letter_path else ''} · "
                        f"fact check changed {changed} thing{'s' if changed != 1 else ''}"
                        + (f" · ATS {score}/100" if score is not None else ""))
        if run.warning:
            feed.emit("warn", f"! {run.warning}")
        return run
