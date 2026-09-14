# CV tailor (v1) design

Local Flask app that tailors an existing CV to one or more job descriptions. It rewrites wording, order, and keywords from **confirmed facts only**. It does not invent experience. It does not log into LinkedIn, scrape with a session, or submit applications.

## Goal

Given a CV file and LinkedIn job page URL(s), produce ATS-readable DOCX CVs (optional cover letters) that emphasize overlap with each posting. Gaps become questions. Skipped questions add nothing.

## Non-goals (v1)

- LinkedIn login, cookies, passwords, or third-party scrapers
- Easy Apply or any apply-form fill
- Multi-user accounts, cloud hosting, or a public website
- PDF export (upload PDF is allowed; output is DOCX only)

## Architecture

Single-user local process. Flask is the wizard UI. LangGraph is the per-job engine. SQLite holds the master profile and job runs. LLM calls go through a provider-agnostic adapter; API keys and model name come from environment variables later.

```
app/          Flask routes, templates, uploads
pipeline/     LangGraph graph and nodes (no HTTP)
profile/      CV parse, profile merge, allowed-fact set
jobs/         URL fetch (no auth), HTML → text, JD extract
export/       ATS-safe DOCX
llm/          ChatModel protocol + env-backed factory
data/         cv_maker.sqlite, uploads, generated files
```

### Graph (one thread per job run)

`ingest_cv` (first run of a batch only, or if profile empty) → `upsert_profile` → `load_job` → `extract_requirements` → `match_and_gaps` → **interrupt: questions** → `apply_answers` → `rewrite_cv` → `optional_letter` → `export_docx`

Checkpoints persist in SQLite so Flask can stop at questions and resume after POST.

### Flask wizard

1. `/` — upload CV (PDF or DOCX)
2. `/jobs` — job URL list (one URL per line); optional paste blocks keyed by URL or by a job title if there is no URL
3. `/jobs/paste` — only jobs whose fetch failed or returned empty description
4. `/questions` — current job’s questions; Skip = do not add that fact
5. `/result` — batch status and downloads. Cover letter: checkbox on the questions page for that job (`generate_letter`). If checked, letter DOCX is produced in `optional_letter`; otherwise no letter file.

A batch is a list of `run_id`s sharing the same profile snapshot at start; each run has its own graph thread.

## Data model

v1 stores **one master profile** (local user). Email/name from the CV fill identity fields; they do not create multiple profiles.

### Profile

- Identity: name, email, phone, location, LinkedIn / GitHub / portfolio URLs
- Optional target role/seniority: only if the user typed it in the UI, never inferred as a claim
- Experience: company, title, location, start, end or current, fact bullets
- Education: school, credential, field, dates
- Skills: name + source (`cv` | `clarification`)
- Extra confirmed facts: certifications, languages, work authorization, notice period — only after a clarification answer
- `updated_at`

### Job run

- `id`, timestamps, `status`: `needs_paste` | `needs_answers` | `ready` | `failed`
- Job URL (optional), raw JD text, fetch ok/fail reason
- Extracted requirements: must-have, nice-to-have, tools, seniority, domain, education, language, work-auth phrases (verbatim kept for keyword overlap)
- Gaps: requirement → `covered` | `partial` | `missing`
- Questions and answers for this run only (v1 does not share a question set across the batch)
- `generate_letter` boolean
- Paths: source CV, output CV DOCX, optional letter DOCX

### Allowed facts

Rewrite and letter nodes may read only: profile after `apply_answers` for that run. Job description is used for ordering, section emphasis, and synonym alignment. It is never a source of new employers, titles, dates, degrees, metrics, or skills.

## Pipeline behavior

### CV ingest

- PDF: extract text with pdfminer.six.
- DOCX: read paragraphs with python-docx.
- LLM maps text into the profile schema.
- Merge into the existing profile: fill empty fields; add roles not already present; do not overwrite a user-confirmed fact unless the new CV supplies a clear supersede (for example an end date on a role that was “current”).

### Job load (batch, no login)

- Split the textarea into URLs (ignore blank lines).
- For each URL: HTTP GET with a standard browser User-Agent. No cookies, no password, no browser profile.
- Success: HTTP 200 and a non-empty job description extracted from the main job HTML (LinkedIn description container; nav/chrome stripped).
- Failure: non-200, login/auth wall, captcha, or empty description → that run is `needs_paste`; other URLs continue.
- User-pasted text for a run always replaces a failed or empty fetch.
- Jobs can be paste-only (no URL) if the user pastes description under a label.

### Requirements and matching

- Extract must-haves, nice-to-haves, tools, years/seniority, domain, education, language, work-auth if present.
- Match each must-have to the profile: `covered` / `partial` / `missing`.
- Partial means related wording without the JD term.
- Questions: missing and unclear partials, must-haves first, **maximum 8 per job**. Nice-to-haves become questions only if a yes would change a bullet.

### Questions

- Yes/no or a short fact bound to a named requirement.
- Skip: no profile write, requirement stays unmatched (do not write it as experience).
- New employer/title/dates: only if the user explicitly fills company, title, and dates (add-role path). Free-text that contradicts the CV without that path is rejected and the fact is not stored.

### Rewrite and export

- Outline from allowed facts only.
- Reorder bullets and skills toward JD keywords; synonym swap when the fact is the same.
- No new metrics, titles, or tools.
- DOCX: single column; Word heading styles for section titles (at least Experience, Education, Skills); no text boxes; no tables used for layout; no headers/footers that drop ATS text; Calibri body font.

### Optional letter

- Generated only if the user opts in for that job.
- Same fact gate; about one page.
- Skip: no file.

## LLM

- Nodes that need language use `llm.ChatModel` (messages in, text or JSON out).
- Factory reads env (for example `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`). v1 ships with a stub that fails with “set provider in .env” until configured.
- LangGraph orchestrates; prompts include the allowed-fact JSON and an explicit ban on adding facts.

## Errors

- Unreadable CV: stop the batch, no runs created.
- Empty JD after fetch and no paste: run stays on `/jobs/paste`.
- Provider unset or LLM error on one job: that run `failed`; other jobs continue.
- Export error: keep structured draft on the run row; do not write a fake DOCX.

## Tests

No live LinkedIn calls in CI.

- Fixture PDF/DOCX → expected profile fields
- Fetch mock: 200 with description vs 403/authwall → paste path
- Match: covered vs missing; skip-answer means skill absent from export
- Honesty: rewrite output cannot contain a skill not in allowed facts (e.g. Kubernetes planted in the model output is stripped)
- DOCX: heading styles present; no layout tables
- Batch: three URLs, one fetch fail → two proceed, one `needs_paste`

## Implementation notes

- Python 3.11 or newer.
- Dependencies (indicative): Flask, langgraph, langchain-core, python-docx, pdfminer.six, httpx, sqlalchemy or sqlite3, python-dotenv.
- LinkedIn HTML selectors live in one module and fail closed (empty text → paste), not with guessed body text from the whole page.
