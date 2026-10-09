# CV tailor

Local tool: upload a CV, paste LinkedIn job URLs, answer questions, download ATS DOCX.
It runs on your own computer, for you only. There is no server, account or cloud storage.

## Setup

**Windows, the easy way:** double-click `CV Tailor.bat`. The first time, it creates `.venv`, installs
everything (and offers the optional apply assistant); after that it starts the app and opens it in your
browser, or just opens the tab if it's already running. Needs Python 3.11+ from python.org.

By hand (any OS):

```
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```
python -m cv_maker              # or: cv-tailor   (add --port 5001 or --no-browser if needed)
```

This opens http://127.0.0.1:5000. Pick a provider, model and API key on the **Settings** page and click
**Test connection**. Values saved there take priority over `.env` and apply immediately (no restart).
Optionally copy `.env.example` to `.env` for defaults. `flask --app cv_maker.app:create_app run` also works.

## Privacy & your data

Each person who runs this app keeps their own data on their own computer. Nothing personal is part of
the code, and nothing is shared through this repository.

- **Where data lives:** a per-user folder outside the project:
  `%LOCALAPPDATA%\CVTailor` (Windows), `~/Library/Application Support/CVTailor` (macOS),
  `~/.local/share/cv-tailor` (Linux). It holds your profile, uploaded CVs, generated documents and
  settings (including your API key). Set `DATA_DIR` to use another folder (keep it outside the repo).
  Settings → *Your data* shows the exact path. Delete that folder to remove everything. Installs that
  already keep a `./data` folder with a database continue to use it (it is git-ignored).
- **What leaves your computer:** your work history, education, skills, job descriptions and your
  answers go to the LLM provider you choose, under that provider's terms. With **Ollama** nothing leaves
  your machine. Job links are fetched from the job site without cookies or any information about you.
  Company research sends only the company's name (to Wikipedia, Wikidata, Hacker News's search and,
  if you add your own key, Tavily, with the field the posting names) and reads the employer's own website. There is no telemetry.
- **Personal details are replaced before sending:** your name, email, phone number, address and profile
  links become placeholders such as `[[NAME_1]]` before any request is sent. The app fills them back in
  on your computer when the answer arrives, so the documents are the same. Your contact details are
  never part of the requests that write CVs and letters; the app adds the CV header and the letter's
  signature itself.
  - Before a CV is read, a *Check before sending* page lists what will be hidden (found by format, and
    from the CV's header and labels such as "Address:"), shows the exact text the LLM will receive, and
    lets you untick items or add more.
  - Parts the app never uses (references, declarations, lines such as "Date of birth:" or
    "Passport no:") are left out unless you choose to keep them.
  - Settings → *Privacy* has an "Also never send" list for any other words, and *See exactly what was
    sent* shows the text of the last 50 requests.
  - Your career details are still sent, because the LLM needs them to tailor the CV, and together they
    can identify you. Other people's names in job descriptions are not detected; add them to the list
    if needed.
- **Built-in protections:** the app only accepts connections from this computer and only answers to
  `localhost` addresses (DNS-rebinding defence). It rejects state-changing requests that come from
  other websites or other local ports (CSRF defence via Fetch Metadata/Origin), refuses to be framed,
  and marks personal pages as non-cacheable. On macOS/Linux the data folder is owner-only (0700).
  The session key is random per install. A short `SECRET_KEY` is ignored.
- **What it deliberately does not add:** a login screen or app-level encryption. On a single-user
  laptop your OS account is the access boundary, and the data folder is only readable by you. Turn on
  full-disk encryption (BitLocker / FileVault / LUKS) to protect it if the laptop is lost. If you
  share the computer with other OS users, do not run the app while they are logged in. Any local
  process can reach a localhost port.
- **Unusual setups** (containers, remote dev boxes): `CV_TAILOR_ALLOWED_HOSTS=name1,name2` and
  `CV_TAILOR_ALLOW_REMOTE=1` relax the localhost-only rules. Only use them on a network you trust.

The app has four sections:

- **Profile:** add your CV, check what will be hidden from the LLM, then check what was read. Edit any fact (you can add, change or remove
  roles, skills and links), update from a newer CV by merging or replacing, and manage uploaded files
  (on the right). While you edit, the right half shows the text of your CV file and what helps a CV rank.
  Only facts in the profile are ever used.
- **Jobs:** add job links (or paste a description). Each job shows its status, a progress bar, what's
  running in the background (prep notes, research, the application check), its next step and the latest
  CV's ATS score, and updates live. With more than one job, a bar across the top shows how far they've
  come together (and how many need you). You can filter by status (Needs you, Working, Ready, Archived),
  search, sort, and archive or delete in bulk. On the right, **Add jobs**; click a job to see it there
  instead: its next step, how each part of it stands and how its must-haves match.
- **A job's page:** in two halves. On the left, the work: its steps (job read → analysed → your answers →
  CV written → applied) with the next thing to do, then one card for each part, each with a badge saying
  where it stands: how your profile matches each requirement, applying (what the application needs,
  caveats from the posting such as a closing date or visa sponsorship, and the assistant), interview prep,
  company research, and your answers. The menu under the steps shows the same states as coloured dots. A
  button answers in its own card (pasting the application link, research, prep notes, reviews), without
  reloading the page. On the right, what you've got: every CV version (pick one; download, delete), and for
  it the CV (with your original wording next to each rewritten bullet), the letter, its ATS check (where
  you improve it), its fact check, and the posting. Versions are kept as `output/cv_<job>_v<n>.docx` in
  your data folder, never overwritten. Results that arrive while you read (a new version, prep notes,
  research) appear in place, keeping what you have open and anything you're typing; a new version opens
  by itself. "New version…" lets you change answers and write again.
- **Questions:** on the right, the posting with each question's skill marked; the question you're on
  lights up where the posting asks for it and what your profile already shows for it.
- **Documents:** all CV and letter versions and uploaded CVs, with type categories, search, company
  filter, latest-only, sort and grouping. Click one to read it on the right (a CV with its ATS score).
- **Settings:** the LLM provider (with *Test connection*), appearance (Auto, Light or Dark; the ◐ button
  next to Settings switches too), privacy (the never-send list and the record of what was sent), pace and
  automation (LLM limits, what runs by itself, company research, politeness toward websites), application
  details and job-site accounts, the log, where your data lives and what leaves your computer, **Export
  my data** (.zip) and **Delete all my data**. The left half lists them at a glance, each with a coloured
  dot (set up, needs attention, off); pick one to change it on the right. The log and the record of what
  was sent show the entry you click in full on the right; application details show your job-site accounts
  there.

There is no separate help section. Guidance appears where it's needed: a short "how it works" while a
page is still empty, and plain explanations next to choices such as Yes / No / Skip.

**Two halves, on every page:** on wide screens what you're doing is on the left, and on the right what
helps you do it: adding a job or the one you picked (the job list), your CV versions (a job), a preview
(Documents), the posting (questions), the live browser (an application), the setting you picked
(Settings), your accounts (application details), the full entry (the log). Each thing is said once, on
one side. The **nerdbar** is a pill in the bottom-right corner showing what's running;
clicking it (or <kbd>`</kbd>) splits the right half again, page's right half above, nerdbar below. Drag
either divider (or focus it and use the arrow keys; double-click resets); the sizes are remembered on this
computer and set before the page shows, so nothing jumps. On narrow screens the halves stack.

**nerdbar:** a live, terminal-style view of what the app is doing: every step, every LLM request (what was
hidden, tokens in and out, time taken), task timers and session totals. It shows placeholders, never
your details.

**Finding your way back:** pages you open from a job (application details, the log, Settings) lead back
to that job, and every job page shows where you are (Jobs › job › applying).

## Ranking in applicant tracking systems (ATS)

The point of the app is a CV that an applicant tracking system ranks well for the job, honestly. ATS
rank CVs mostly by the posting's own keywords: whether each one appears, in the posting's wording, and in
context (a role's bullets) rather than only in a skills list. They also need standard sections, a title
that matches, contact details they can read and dates on every role. So:

- **Requirements are split into single skills.** "Python, Java, JavaScript/TypeScript, React" is five
  requirements, each matched, asked about and counted on its own. Phrases that aren't lists (CI/CD,
  UI/UX design, Research and development) stay whole.
- **Each is matched with evidence you can trace:** found in your skills list (also under other names: RAG
  is retrieval-augmented generation, K8s is Kubernetes, Azure OpenAI is one of the Azure AI services,
  PostgreSQL relies on SQL), in a role's bullets or title (recent roles count more), in certifications or
  projects, or in the level you gave. The job's page lists every requirement strongest first with a 0–100
  score, a strength (strong, partial, weak, not found) and the lines that earned it.
- **Then a recruiter's reading** (one LLM request) of what that search didn't settle. For each requirement it
  works through, in order, what the job needs it for, which bullets show it or come close (cited by id, and
  checked to exist), and a verdict: shown in other words ("deployed model services on EKS" is Kubernetes),
  related work, or nothing. It can also see when a keyword hit means something else ("go-to-market" isn't Go).
  Its reading adds one labelled line of evidence to the score, never the whole score, and it stops counting
  once you edit the bullet it relied on.
- **Questions are specific.** Each names what the job uses the skill for and the closest thing in your CV, and
  asks for something concrete ("Your Northwind work deployed model services on EKS. Did you write the
  manifests or run the cluster?"), with a hint of what a useful answer contains. You still answer with a
  level (no experience, beginner, intermediate, expert), which decides the wording ("familiar with",
  "working knowledge of", "expert in"); the fact check removes anything that says more.
- **The CV is planned before it's written** (one LLM request): what the job is mostly about, how much each
  role serves it (core, supporting, peripheral), which bullets prove it, which skills matter and which only
  dilute it. Rules keep the plan honest and bounded: every role keeps its title and dates, a role gets at most
  6, 3 or 1 bullets by how much it serves the job, your latest role keeps at least two, and the bullet that
  shows a must-have keyword always stays. The CV tab shows the plan for each version. Bullets left out stay in
  your profile.
- **The writing follows the plan and a keyword plan:** only the chosen bullets, each written by id (a reply
  that skips one keeps your wording for that bullet only), the posting's keywords you have with the facts that
  support each, and the ones you don't (never claimed). Then the app makes sure every keyword you have appears
  in the posting's own wording (acronyms written out once, like "Retrieval-Augmented Generation (RAG)"),
  puts Skills right under the summary and titles the CV with the posting's job title.
- **Changed your profile?** Each version remembers the profile it was written from. When yours has changed
  since, the job's page offers *Write v2 with your changes*, and the Profile page lists every such job with
  one button to rewrite them all, using the answers you already gave.
- **Every version gets an ATS check** (the job's page, *ATS check* tab): a score out of 100 from must-have
  keywords (45), nice-to-haves (10), keywords in context (10), title (8), standard sections (7), contact
  details (5), dates (5), measurable results (5) and length (5), each with what to fix, plus a table of the
  posting's keywords showing where each appears in the CV. Keywords you don't have are shown as honest
  gaps, never added.
- **The score is counted, not guessed.** No LLM is involved: each part counts what it says in the CV's own
  text (the share of must-have keywords found under any of their names, bullets with numbers, and so on)
  and is weighted as above, so the same CV and posting always get the same score, and every point can be
  traced. *How the score is counted* (in the tab) spells it out. Then **the file is read back**: the DOCX
  the employer gets is opened and read the way an ATS parser reads it, to confirm it holds what the check
  counted: plain text in reading order, standard headings marked as headings, contact details in the body
  (not a header or footer), every counted keyword present, and one column with no tables, text boxes,
  images or header text. Tracking systems differ by vendor and each employer configures theirs, so no tool
  can promise the number a particular employer's system shows; this checks what they commonly look at.
- **Improve a version from its check.** In the *ATS check* tab, tick what to improve: a must-have keyword
  your profile doesn't show, one that's only in the skills list, results without numbers, roles without
  dates, missing contact details or education, the title, the length (each shows how much it can add).
  *Check my picks and ask me* sends one LLM request that checks your picks against your profile: fixable
  from what's there, needs facts only you have, or no sign of it in your profile. It words a short question
  for each that needs you: a number for a result, where you used a skill (and at what level), dates. The
  LLM never writes facts, and its reply is validated: questions must point at bullets, roles and keywords
  that exist, and it can't call a keyword you don't have "fixable". If the check fails, the standard
  questions are asked instead. Your answers go into your profile as confirmed facts (a skill's level also
  becomes the job's answer), then a new version is written (with a letter, if you tick it) and checked
  again: the tab shows the score before and after, what moved, and what each pick came to.

## When something goes wrong: the log

Settings → **Log** keeps a trace of what the app did: every step, warning and error, each tagged with the
job, CV or application it belongs to, and the full traceback when something fails. A failed job, CV or
application links straight to its own entries ("What happened (log)"), and Settings shows how many
errors and warnings there were in the last day. You can filter (errors, warnings, one job), search,
download or clear it.

The log stays in your data folder (`logs/cv-tailor.log`, four files of 1 MB at most) and is never sent
anywhere. Your name, contact details, the never-send words, email addresses and API keys are masked in
it, and your home folder is written as `~`, so you can attach it to a bug report as it is.

## Checks along the way

What you type is checked before it's saved or sent, and each check says what to fix (keeping what you
typed): LLM settings (provider, model, endpoint, region, a key without stray spaces), pace numbers,
job links (25 per batch), pasted descriptions (too short to be one, or so long that only the start is
read), answers, the never-send list (words too short to hide safely), application details (email, phone,
links), profile edits (a name, real links) and uploads (10 MB at most, and enough text: a scanned PDF
says so instead of failing later).

Then each step checks its own work: the *Check before sending* page and placeholders before anything goes
to the LLM; every LLM reply must be the JSON asked for (one retry, then a clear error; a broken reply is
never reused); the honesty filter and fact check on what's written; every DOCX is opened again before it
replaces anything; the apply assistant asks rather than guess, and never submits by itself. Settings are
saved in one step, so a crash can't leave the file half written.

## Interview prep and company research

Once a CV is written, the job's page gets **interview prep** notes (one LLM request): the role in brief,
topics to brush up on (each marked strong, refresh or gap against your profile), likely questions and which part
of your experience answers them, questions to ask, and things to watch out for in the posting.

**Company research** is a separate agent. Its findings are shared by every job at the same company, kept
for two weeks, and every point names its source. Choose the depth in Settings → *Pace and automation*,
or per job with *Simple* / *Thorough*:

| Depth | What it does | LLM requests |
| --- | --- | --- |
| Off | nothing | 0 |
| Simple | a Wikipedia summary, plus links to the review sites you picked | 0 |
| Thorough | also Wikidata facts (founded, size, headquarters, industry), the company's own About page if its robots.txt allows, Hacker News discussions from the last three years and, with your Tavily key, this year's news and what people say about working there, organised with a source for every point | 1 per company |

- **Read automatically:** Wikipedia, Wikidata, the company website and Hacker News. Each one can be
  switched off.
- **Tavily, with your own key (optional):** paste a key from [tavily.com](https://tavily.com) in Settings →
  *Pace and automation* (*Test key* shows your credits without spending any). Thorough research then makes
  two Tavily searches per company (2 credits): news from the last year, and employee reviews, culture and
  interview experiences. Only the company's name and its field from the posting are sent, as
  `"Prodigal" company fintech collections software`, so the search finds the employer, not a namesake. Results show on the job's page with their
  sources, and the review snippets come from Tavily's own search index, so the app still never visits the
  review sites itself. The key stays in your settings file: never in the log or the export.
- **Linked only:** Glassdoor, Indeed, AmbitionBox, Reddit, Blind and Levels.fyi don't allow automated
  reading (in their terms or robots.txt), so the app never fetches them. You get a search link for each
  one you picked. Paste what you read there into *What people say*, and the app summarises it into pros,
  cons, interview experiences and things to ask (one LLM request).
- **The right company, not a namesake.** A company's name is often something else too ("Prodigal" is also a
  film). Wikipedia pages about films, books, songs, places and people are never taken for the company; a
  Wikipedia page whose official website differs from the employer's own (from the job link) is set aside;
  and thorough research first sorts out, with the job's title, field and website, which findings are about
  this employer at all. The rest are dropped and the page says how many. Research saved before this is
  marked as possibly mixed up and redone the next time it's needed.
- When a source refuses or asks the app to slow down, research skips it and says so on the job's page.

Interview prep, company research and the application check each have a switch in Settings, so you can
have them run only when you ask.

## Pace: staying within limits

The app is careful with your LLM provider and with the websites it visits, so neither your API key nor
your computer gets throttled or blocked as a bot.

- **LLM pace** (Settings → *Pace and automation*): requests a minute, how many at the same time, and a
  daily cap. Leave them empty to use the suggestions, which stay a little under your provider's limits;
  for OpenRouter's free models (20 a minute, 50 a day) that's 15 a minute, one at a time, 45 a day.
  When a provider says it's busy, the app waits as long as it asks, three times at most, then shows
  the error.
- **Fewer tokens:** contact details never go into writing prompts. Reading the same CV or job
  description again reuses the earlier answer (stored with placeholders only, for two weeks; *Forget
  reused answers* clears them). The application check uses no LLM, and company research makes one
  request per company, reused for two weeks.
- **Politeness toward websites:** one request at a time per site, with a pause between requests, and
  time off after a site says "slow down". Hover the ⓘ next to the setting to see these values:

  | | Gentle | Standard | Brisk |
  | --- | --- | --- | --- |
  | Between requests to one site | 4 s | 2 s | 1 s |
  | Between requests to LinkedIn | 10 s | 6 s | 4 s |
  | Time off after a site says "slow down" | 15 min | 5 min | 2 min |
  | Assistant: pause after each field | 0.7 s | 0.35 s | 0.15 s |
  | Assistant: pause after each page | 2.5 s | 1.2 s | 0.6 s |

- **No disguises:** the app names itself in its User-Agent, follows robots.txt when researching, never
  solves CAPTCHAs, and doesn't hide that it's automated. If a site blocks it anyway, it stops and tells
  you instead of working around the block.

## Apply assistant (optional)

Once a CV is written you can stop and apply yourself, or press **Apply with the assistant…** on the
job's page. A private browser on your computer fills in the application while you watch it live in the
app. You can pause and take over (click and type in the view) at any time, then resume.

- **Checked in advance:** after a job is analysed, the app looks at its application in a throwaway
  private browser. Nothing is typed, uploaded or submitted, and no LLM is used: it only follows Apply /
  Continue links, declines optional cookies, and stops at the first sign-in wall, CAPTCHA or form. The
  job's page then lists what the application will need: an account (and how you can sign in),
  documents such as a required cover letter, questions you haven't answered yet, and consent or
  diversity sections.
- **Applying while the CV is written:** tick *Fill in the application … while the CV is being written*
  on the questions page (Settings can tick it by default). The assistant fills in everything that
  doesn't need the CV, waits for the CV, then attaches it. If the form requires a cover letter you
  didn't ask for, the writer adds one and the nerdbar says so (if the CV is already finished by then,
  the assistant asks you instead). One application runs at a time.
- **How it fills forms:** your contact details and saved answers (Settings → *Application details*) are
  filled in by rule on your computer. The LLM reads the rest of the form as text (never screenshots), sees
  placeholders for your details, and writes only from your profile.
- **When it stops and asks you:** a required question with no saved answer, a sign-in it can't do with a
  saved account, a CAPTCHA, an email confirmation, or the application moving to an unfamiliar website.
  Forms that move to an application system employers use (Workday, Greenhouse, Lever, SmartRecruiters,
  iCIMS, Taleo, SuccessFactors and others) carry on without asking. After you get past a sign-in, a
  CAPTCHA or LinkedIn, it notices and carries on by itself (a tab LinkedIn opens blank first is followed once
  it loads).
- **Real-world forms:** it reads a field's question even when the site gives it no label (the text next to
  it), and understands dropdowns whose options appear only once opened (it opens them to read the options),
  typeaheads (it types and picks a suggestion), "select all that apply" boxes, scrollable lists, radio groups
  and date fields. Every fill is read back: a value the site didn't keep is asked about, with the options the
  site offered, instead of the assistant moving on.
- **Your answers stick:** each question is headed by the form's own words, with the site's help text, and
  the field is outlined in the view while you answer. Your answers are kept for the whole application, so a
  site that re-renders its form (Oracle, Workday) or a page you come back to is filled from them, not asked
  again. Tick "save" to reuse an answer on later applications.
- **Passwords and ID numbers are yours to type:** fields for passwords, one-time codes, security answers,
  PINs and ID or bank numbers are never filled from an answer, sent to the LLM, saved or logged. The
  assistant asks you to type them in the view yourself. The only exception is your own sign-in, from your
  system's password store.
- **The live browser:** the right half of the application page. **Take over** whenever you like and use
  it as a browser: click, scroll and type in it, or use Back, Forward, Reload and the address bar (using
  them takes over too). **Let the assistant continue** hands it back.
- **LinkedIn:** LinkedIn's user agreement forbids bots and automated sign-ins, and the account at risk
  would be yours, so the assistant never signs in or clicks there, and LinkedIn passwords aren't stored.
  LinkedIn also shows a job's company link only to signed-in members. For a LinkedIn job, the assistant
  opens the posting and you do the LinkedIn part in its window: sign in if asked (only the first time:
  its browser remembers you), then click **Apply**. As soon as you reach the company's site, the
  assistant carries on by itself. Easy Apply jobs are applied for on LinkedIn itself, by you.
- **What it never does:**
  - Submit by itself: you approve the final submit after reviewing everything entered.
  - Tick consent boxes: those are yours.
  - Answer diversity questions: it picks "prefer not to say" when offered, otherwise asks you.
  - Tick job-alert boxes, accept cookies (it takes "reject" when offered), or act on LinkedIn, which
    forbids automated applications.
  - Use tricks to hide that it's automated.
- **Accounts:** it can fill in a sign-up form with a generated password, saved to your system's password
  store (Windows Credential Manager under *Windows Credentials → Generic Credentials*, entries named
  "CV Tailor"; macOS Keychain; the Secret Service on Linux) through `keyring`, never to the app's files,
  its export or the LLM. You tick the site's terms and press its button. You can also add accounts you
  already have (Settings → *Application details* → *Accounts on job sites*). With "Sign me in with a saved
  account" on, the assistant signs in like a password manager would: your account, on its own site only,
  never to create an account and never on LinkedIn. On application systems many employers share
  (Workday, iCIMS, Taleo, SuccessFactors…) each employer has its own login, so an account belongs to that
  employer's address (acme.wd3.myworkdayjobs.com). **Download for a password manager (.csv)** exports them,
  after you confirm, in the CSV that Bitwarden, 1Password, Chrome, Edge, Firefox, Proton Pass and KeePassXC
  import (name, url, username, password, note). The file holds the passwords in plain text: import it,
  then delete it.
- **Progress:** the application page shows its steps (opening → filling in → your review → submitted)
  and how many fields it has filled on how many pages.
- **Setup:** `pip install -e ".[apply]"`, then once `python -m playwright install chromium` (about 150 MB).

Some job sites' terms restrict automated use. The assistant applies only on your behalf, at human pace,
under your supervision.

LinkedIn: public job pages are read without signing in. Search/collection links containing
`currentJobId` are mapped to the job page. The app tells an Apply button that leads to the company's
site from Easy Apply, and picks up a company application link the description itself contains.
Anything that cannot be read asks you to paste the description. For a LinkedIn job, the job's Applying
card takes the company's application link (copy it from the page LinkedIn's Apply opens, signed in, in
your own browser): `https://` may be left off, and a LinkedIn redirect link is unwrapped to the company's
address inside it. LinkedIn's own pages are refused there, with the reason shown in the card.

## Providers

Set the provider on the Settings page (or `LLM_PROVIDER`): `openai`, `azure`, `bedrock`, `ollama`, `groq`, `openrouter`.

Install provider SDK: `pip install "cv-maker[openai]"` (or your provider). `openrouter` and `ollama` need no SDK.

| Provider | Needs |
| --- | --- |
| openai | API key, model |
| azure | API key, model, Azure endpoint |
| bedrock | model, AWS region; credentials from the standard AWS chain |
| ollama | model (default `llama3`), Ollama host; no key |
| groq | API key, model |
| openrouter | API key, model (e.g. `openai/gpt-4o-mini`) |

## Tests

```
pytest -v
```

Tests use temporary folders and fake LLM, fetch and research clients; they never touch your data folder
or the network.
The apply assistant is tested end to end against a small mock job site on localhost (`tests/fixtures/mock_ats.py`).
Those tests are skipped when Playwright's Chromium isn't installed.
