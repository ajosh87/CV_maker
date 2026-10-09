"""Company research: a separate agent that gathers what's publicly, and permissibly, available about an
employer, and says where every point came from.

Depth (Settings → Company research):
- off: nothing.
- simple: a summary from Wikipedia (one or two requests) and links to the review sites you picked. No LLM tokens.
- thorough: also facts from Wikidata, the company's own About page (only if its robots.txt allows),
  discussions from Hacker News's public API and, with your own Tavily key, this year's news and what people say
  about working there (two Tavily searches), then one LLM request that organises it, citing a source per point.
Sites whose terms or robots.txt don't allow automated reading (Glassdoor, Indeed, AmbitionBox, Blind, Levels.fyi,
Reddit) are linked, never fetched; paste what you read there to have it summarised.
Results are kept per company for two weeks and shared by every job at that company.

A company's name is often something else too ("Prodigal" is also a film). Every search says it means the company,
with what the job tells about it (its field, its website); Wikipedia pages about films, books, songs and the like are
never taken for it; a Wikipedia page whose official website differs from the employer's is set aside; and the LLM
first sorts out which findings are about this employer at all, and those that aren't are dropped before anything
is shown.
"""
import re
import time
import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import quote, quote_plus, urljoin, urlparse

import httpx

from cv_maker.events import feed
from cv_maker.jobs.html import page_links, page_text
from cv_maker.jobs.polite import sites
from cv_maker.jobs.urls import site_of
from cv_maker.llm.chat import complete_json

# Identify honestly, with a URL, as Wikimedia's robot policy (and good manners) require: the project's public page,
# so site owners can see what the app is and how to reach its maintainers.
PROJECT_URL = "https://github.com/ajosh87/CV_maker"
USER_AGENT = f"CVTailor/0.1 (+{PROJECT_URL}; personal job-research tool, one person, low volume) httpx/{httpx.__version__}"
FRESH_DAYS = 14
VERSION = 2  # research saved by an earlier version, which didn't tell the company from its namesakes, is redone
DEPTHS = ("off", "simple", "thorough")
# key: (label, read automatically?, what it gives, domain for link-only sources)
SOURCES = {
    "wikipedia": ("Wikipedia", True, "a short summary of the company", ""),
    "wikidata": ("Wikidata", True, "founded, size, headquarters, industry, website", ""),
    "website": ("Company website", True, "its own About page, if its robots.txt allows", ""),
    "hackernews": ("Hacker News", True, "discussions, through its public API", ""),
    "tavily": ("Tavily search", True, "news and what people say, with your own Tavily key (2 credits per company)", ""),
    "glassdoor": ("Glassdoor", False, "reviews, salaries and interview reports", "glassdoor.com"),
    "indeed": ("Indeed", False, "reviews", "indeed.com"),
    "ambitionbox": ("AmbitionBox", False, "reviews, popular in India", "ambitionbox.com"),
    "reddit": ("Reddit", False, "discussions", "reddit.com"),
    "blind": ("Blind", False, "discussions among employees", "teamblind.com"),
    "levels": ("Levels.fyi", False, "pay levels", "levels.fyi"),
}
NOT_FETCHED = "linked only: the site's terms or robots.txt don't allow automated reading"
# Job boards and application systems: a job link on these says nothing about the employer's own website.
_NOT_EMPLOYER = {"linkedin.com", "indeed.com", "glassdoor.com", "myworkdayjobs.com", "myworkday.com", "greenhouse.io",
                 "lever.co", "eightfold.ai", "smartrecruiters.com", "ashbyhq.com", "icims.com", "taleo.net",
                 "successfactors.com", "workable.com", "jobvite.com", "bamboohr.com", "recruitee.com", "teamtailor.com",
                 "personio.de", "naukri.com", "monster.com", "example.com"}
_ABOUT = re.compile(r"^(about( us)?|who we are|our (company|story)|company)$", re.I)
_ORGANISATION = re.compile(r"\b(company|companies|corporation|firm|organi[sz]ation|business|manufacturer|retailer|bank|group|"
                           r"provider|developer|maker|publisher|conglomerate|multinational|start-?up|agency|university|"
                           r"hospital|consultancy|insurer|airline|brand|subsidiary|enterprise)\b")
# A Wikipedia page about one of these is never the employer, whatever else its text says.
_NOT_A_COMPANY = re.compile(r"\b(film|movie|album|song|single|novel|book|band|television|tv series|series|episode|"
                            r"character|video game|play|musical|soundtrack|poem|painting|comic|manga|anime|surname|"
                            r"given name|parable|river|village|town|city|genus|species|ship|horse|racehorse|wrestler|"
                            r"footballer|singer|rapper|actor|actress)\b", re.I)
_ABOUT_PATH = re.compile(r"/(about|about-us|who-we-are|our-company|our-story)(/|$)", re.I)
_WIKIDATA = {"P571": "Founded", "P159": "Headquarters", "P1128": "Employees", "P452": "Industry", "P856": "Website",
             "P169": "Chief executive", "P414": "Stock exchange"}


def company_key(company: str) -> str:
    return " ".join((company or "").casefold().split())


def chosen_sources(setting: str) -> list[str]:
    return [s for s in (setting or "").split(",") if s in SOURCES]


def hint(context: dict | None) -> str:
    """A few words that say which company is meant: its field, from the job (e.g. "fintech collections software")."""
    context = context or {}
    words = " ".join(str(context.get("domain") or "").split()[:6])
    return words or " ".join(str(context.get("title") or "").split()[:4])


def _query(company: str, context: dict | None, *more: str) -> str:
    """'"Prodigal" company fintech collections ...': the name as a phrase, said to be a company, in its field."""
    return " ".join(x for x in (f'"{company}"', "company", hint(context), *more) if x)


def links(company: str, sources: list[str]) -> list[dict]:
    """Searches (on DuckDuckGo, which doesn't profile you) for the company on the link-only sites you picked."""
    named = f'"{company}"'
    return [{"site": SOURCES[s][0], "about": SOURCES[s][2],
             "url": "https://duckduckgo.com/?q=" + quote_plus(f"site:{SOURCES[s][3]} {named} company reviews")}
            for s in sources if not SOURCES[s][1]]


# ---- open sources ----

class Refused(Exception):
    """A source said no (rate limit, or refusing automated visitors). It is left alone; never worked around."""


def _get(client: httpx.Client, throttle, url: str, **params) -> httpx.Response:
    if throttle.paused_for(url):
        raise Refused("asked the app to slow down earlier, so it's being left alone for now")
    with throttle.turn(url):
        response = client.get(url, params=params or None)
    if response.status_code in (429, 503):
        throttle.pause(url)
        raise Refused(f"asked the app to slow down (HTTP {response.status_code}), so it's being left alone for a while")
    return response


def _summary(client, throttle, title: str) -> dict | None:
    response = _get(client, throttle, f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}")
    if response.status_code == 403:
        raise Refused("refused the request (HTTP 403)")
    if response.status_code != 200:
        return None
    page = response.json()
    return None if page.get("type") == "disambiguation" or not page.get("extract") else page


def _organisation(page: dict | None, company: str) -> bool:
    """Only a page that is plainly about this organisation: a company's name often matches something else."""
    if not page:
        return False
    description = page.get("description", "") or ""
    if _NOT_A_COMPANY.search(description):  # "2019 American film": the description says what the page is about
        return False
    first = re.split(r"(?<=[.!?])\s", page["extract"], maxsplit=1)[0]
    if _NOT_A_COMPANY.search(first) and not _ORGANISATION.search(first):
        return False
    about = f"{description} {page['extract'][:300]}".casefold()
    return company_key(company).split()[0] in page.get("title", "").casefold() and bool(_ORGANISATION.search(about))


def wikipedia(company: str, client, throttle) -> dict | None:
    # The company's own page first (one request); search only if that misses or is about something else
    # ("Apple" is the fruit; the search finds "Apple Inc.").
    page = _summary(client, throttle, company)
    if not _organisation(page, company):
        found = _get(client, throttle, "https://en.wikipedia.org/w/api.php", action="opensearch", search=company, limit=5,
                     namespace=0, format="json")
        tried = {company.casefold(), (page or {}).get("title", "").casefold()}
        titles = [t for t in (found.json()[1] if found.status_code == 200 else []) if t.casefold() not in tried]
        page = None
        for title in titles[:2]:  # at most two more pages: the company's is near the top
            candidate = _summary(client, throttle, title)
            if _organisation(candidate, company):
                page = candidate
                break
    if page is None:
        return None
    return {"title": page.get("title", company), "summary": page["extract"][:1200], "description": page.get("description", ""),
            "url": (page.get("content_urls") or {}).get("desktop", {}).get("page", ""), "wikidata": page.get("wikibase_item", "")}


def _when(statement: dict) -> str:
    """When a statement was true (point in time, else start time) in Wikidata's sortable form, '+2024-01-01T…'."""
    for prop in ("P585", "P580"):
        for qualifier in (statement.get("qualifiers") or {}).get(prop) or []:
            value = (qualifier.get("datavalue") or {}).get("value")
            if isinstance(value, dict) and value.get("time"):
                return value["time"]
    return ""


def _current(statements: list) -> dict:
    """The statement that holds now: the preferred one, else those without an end date, the latest of them.
    (Past CEOs and old headcounts are listed too, often first.)"""
    live = [s for s in statements if isinstance(s, dict) and s.get("rank") != "deprecated"]
    live = [s for s in live if s.get("rank") == "preferred"] or live
    live = [s for s in live if "P582" not in (s.get("qualifiers") or {})] or live
    return max(live, key=_when, default={})


def wikidata_facts(qid: str, client, throttle) -> dict:
    claims = (_get(client, throttle, f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json").json()
              .get("entities", {}).get(qid, {}).get("claims", {}))
    raw, need = {}, set()
    for prop, label in _WIKIDATA.items():
        statement = _current(claims.get(prop) or [])
        snak = (statement.get("mainsnak") or {}).get("datavalue", {})
        value = snak.get("value")
        if isinstance(value, dict) and "id" in value:
            raw[label] = value["id"]
            need.add(value["id"])
        elif isinstance(value, dict) and "time" in value:
            raw[label] = value["time"].lstrip("+")[:4]
        elif isinstance(value, dict) and "amount" in value:
            year = _when(statement)[1:5]  # a headcount means little without its year
            raw[label] = f"{int(float(value['amount'])):,}" + (f" ({year})" if year else "")
        elif isinstance(value, str):
            raw[label] = value
    if need:
        names = _get(client, throttle, "https://www.wikidata.org/w/api.php", action="wbgetentities", ids="|".join(sorted(need)),
                     props="labels", languages="en", format="json").json().get("entities", {})
        raw = {k: names.get(v, {}).get("labels", {}).get("en", {}).get("value", v) if v in need else v for k, v in raw.items()}
    return raw


_robots: dict[str, urllib.robotparser.RobotFileParser | bool] = {}


def robots_allows(url: str, client, throttle) -> bool:
    """robots.txt as the standard says: missing (4xx) allows everything; unreachable (5xx) allows nothing."""
    parts = urlparse(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots:
        try:
            response = _get(client, throttle, origin + "/robots.txt")
            if response.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
                _robots[origin] = parser
            else:
                _robots[origin] = 400 <= response.status_code < 500
        except httpx.HTTPError:
            _robots[origin] = False
    rule = _robots[origin]
    return rule.can_fetch(USER_AGENT, url) if isinstance(rule, urllib.robotparser.RobotFileParser) else bool(rule)


def employer_site(job_url: str, facts: dict) -> str:
    if facts.get("Website", "").startswith("http"):
        return facts["Website"]
    site = site_of(job_url or "")
    return "" if not site or site in _NOT_EMPLOYER else f"https://www.{site}"


def website_about(home: str, client, throttle) -> dict:
    """The employer's About page (or home page), only where robots.txt allows: two pages at most."""
    if not robots_allows(home, client, throttle):
        return {"url": home, "allowed": False, "note": "its robots.txt asks automated visitors not to read it"}
    response = _get(client, throttle, home)
    if response.status_code in (401, 403, 429):
        return {"url": home, "allowed": False, "note": f"it refused automated reading (HTTP {response.status_code}), so it was left alone"}
    if response.status_code != 200:
        return {"url": home, "allowed": False, "note": f"its home page answered HTTP {response.status_code}"}
    url, markup = str(response.url), response.text
    about = next((urljoin(url, href) for text, href in page_links(markup)
                  if (_ABOUT.match(text) or _ABOUT_PATH.search(href)) and site_of(urljoin(url, href)) == site_of(url)), "")
    if about and robots_allows(about, client, throttle):
        page = _get(client, throttle, about)
        if page.status_code == 200:
            url, markup = str(page.url), page.text
    text = page_text(markup, limit=3000)
    return {"url": url, "allowed": True, "text": text, "chars": len(text)}


def hackernews(company: str, client, throttle, years: int = 3) -> list[dict]:
    """Recent stories about the company (older news says little about working there now). Its name must be in the
    title as a word of its own; whether a story is about this company or a namesake is sorted out after."""
    since = int(time.time()) - years * 365 * 86400
    hits = _get(client, throttle, "https://hn.algolia.com/api/v1/search", query=company, tags="story", hitsPerPage=30,
                numericFilters=f"created_at_i>{since}").json().get("hits", [])
    named = re.compile(rf"(?<!\w){re.escape(' '.join(company.split()))}(?!\w)", re.I) if company.strip() else None
    stories = [h for h in hits if named and named.search(h.get("title") or "")]
    stories.sort(key=lambda h: h.get("points") or 0, reverse=True)
    return [{"title": h["title"], "url": f"https://news.ycombinator.com/item?id={h['objectID']}", "points": h.get("points") or 0,
             "comments": h.get("num_comments") or 0, "date": (h.get("created_at") or "")[:10], "source": "Hacker News"}
            for h in stories[:6]]


TAVILY_URL = "https://api.tavily.com/search"
TAVILY_KEY = re.compile(r"tvly-[A-Za-z0-9_\-]{8,200}")


def tavily_usage(key: str, client: httpx.Client | None = None) -> tuple[bool, str]:
    """Check a key without spending credits: Tavily's usage endpoint says how much of this month's plan is used."""
    own = client is None
    client = client or httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT})
    try:
        response = client.get("https://api.tavily.com/usage", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return False, f"Couldn't reach Tavily ({type(exc).__name__}). Check your connection and try again."
    finally:
        if own:
            client.close()
    if response.status_code == 401:
        return False, "Tavily rejected this key. Copy it again from your Tavily account."
    if response.status_code != 200:
        return False, f"Tavily answered HTTP {response.status_code}. Try again later."
    data = response.json() if "json" in response.headers.get("content-type", "") else {}
    key_use, account = data.get("key") or {}, data.get("account") or {}
    used, limit = key_use.get("usage", account.get("plan_usage")), key_use.get("limit") or account.get("plan_limit")
    plan = f" ({account['current_plan']} plan)" if account.get("current_plan") else ""
    try:
        used, limit = int(used), int(limit)
    except (TypeError, ValueError):
        used = limit = None
    if used is not None and limit:
        return True, f"The key works{plan}: {used:,} of {limit:,} credits used this month. Research uses 2 per company."
    return True, f"The key works{plan}. Research uses 2 credits per company."


def _tavily_search(client, throttle, key: str, **body) -> list[dict]:
    """One Tavily search (1 credit). Your key goes only to Tavily, in the Authorization header."""
    if throttle.paused_for(TAVILY_URL):
        raise Refused("asked the app to slow down earlier, so it's being left alone for now")
    with throttle.turn(TAVILY_URL):
        response = client.post(TAVILY_URL, json=body, headers={"Authorization": f"Bearer {key}"})
    code = response.status_code
    if code == 401:
        raise Refused("rejected the API key (HTTP 401); check it in Settings → Pace and automation")
    if code in (432, 433):
        raise Refused(f"says your plan's credits are used up (HTTP {code}); see your Tavily account")
    if code == 429:
        throttle.pause(TAVILY_URL)
        raise Refused("asked the app to slow down (HTTP 429), so it's being left alone for a while")
    if code != 200:
        raise Refused(f"answered HTTP {code}")
    found = []
    for r in response.json().get("results") or []:
        url = str(r.get("url") or "")
        if url.startswith(("https://", "http://")):
            found.append({"title": " ".join(str(r.get("title") or url).split())[:200], "url": url, "site": site_of(url),
                          "snippet": " ".join(str(r.get("content") or "").split())[:400],
                          "date": str(r.get("published_date") or "")[:16]})
    return found


def tavily(company: str, key: str, client, throttle, context: dict | None = None) -> dict:
    """Two searches (2 credits): this year's news about the company, and what people say about working there.
    Only the company's name and its field (from the posting) are sent, never anything about you. Tavily reads the
    web with its own search index; the app itself still never visits the sites it links to."""
    news = _tavily_search(client, throttle, key, query=_query(company, context), topic="news", time_range="year",
                          max_results=5, search_depth="basic", include_published_date=True)
    people = _tavily_search(client, throttle, key, query=_query(company, context, "employee reviews, work culture and interview process"),
                            topic="general", max_results=6, search_depth="basic")
    if not news and not people:
        return {"news": [], "people": [], "note": "found nothing about the company"}
    return {"news": news, "people": people}


_ORGANISE = """Organise what was found about {company} for a candidate preparing to apply and interview there.

Which {company}: the employer hiring for "{title}"{field}{site}.
What the posting says about the job:
<<<
{about}
>>>

Work in two steps.
1. Sort: names are shared ("Prodigal" is also a film; many firms share a name). Go through the numbered findings
   below and list in "unrelated" the number of every one that is NOT about this employer (another company, a film,
   a book, a person, a word used in its everyday sense). When unsure, judge by the field, the website and the job.
2. Organise only the findings left, and name the source of every point: Wikipedia, Wikidata, Company website,
   Hacker News, or the website a news or web result came from (for example "reuters.com" or "glassdoor.com via Tavily").

[W] Wikipedia: {summary}
[F] Wikidata facts: {facts}
[S] Company website: {website}
Hacker News discussions (title · points · comments · date):
{discussions}
News this year (title · website · date: extract):
{news}
What people say on the web (title · website: extract):
{people}

Return one JSON object:
{{"unrelated": ["W", "D2", "N1", ...],
  "identity": "one line: which organisation this is (field, where), as the findings left show it",
  "what_they_do": "2-3 sentences",
  "points": [{{"point": "something worth knowing", "source": "..."}}],
  "discussions": [{{"point": "what the discussions are about", "source": "Hacker News"}}],
  "what_people_say": [{{"point": "what employees or candidates report", "source": "the website it came from"}}],
  "for_interviews": ["talking points or questions this material suggests"]}}
At most 6 points, 4 discussions, 5 what_people_say and 4 for_interviews. Leave a list empty rather than guess, and
leave what_they_do empty if nothing left is about this employer."""


def _numbered(items: list[dict], letter: str, line) -> str:
    return "\n".join(f"[{letter}{i}] {line(x)}" for i, x in enumerate(items, 1)) or "(none)"


def _organise(company: str, found: dict, model, context: dict | None = None) -> dict:
    context = context or {}
    web = found.get("web") or {}
    data = complete_json(model, _ORGANISE.format(
        company=company, title=context.get("title") or "a role",
        field=f", in {context['domain']}" if context.get("domain") else "",
        site=f", website {context['site']}" if context.get("site") else "",
        about=" ".join(str(context.get("about") or "").split())[:700] or "(not available)",
        summary=(found.get("basics") or {}).get("summary", "") or "(none)",
        facts="; ".join(f"{k}: {v}" for k, v in found.get("facts", {}).items()) or "(none)",
        website=(found.get("website") or {}).get("text", "")[:2500] or "(none)",
        discussions=_numbered(found.get("discussions") or [], "D", lambda d: f"{d['title']} · {d['points']} · {d['comments']} · {d['date']}"),
        news=_numbered(web.get("news") or [], "N", lambda n: f"{n['title']} · {n['site']} · {n['date']}: {n['snippet']}"),
        people=_numbered(web.get("people") or [], "P", lambda n: f"{n['title']} · {n['site']}: {n['snippet']}"),
    ))

    def sourced(items, limit):
        return [{"point": str(i.get("point", ""))[:300], "source": str(i.get("source", ""))[:40]}
                for i in (items or [])[:limit] if isinstance(i, dict) and i.get("point")]

    unrelated = {str(u).strip().upper().strip("[]") for u in data.get("unrelated") or [] if str(u).strip()}
    return {"what_they_do": str(data.get("what_they_do") or "")[:600], "points": sourced(data.get("points"), 6),
            "discussions": sourced(data.get("discussions"), 4), "what_people_say": sourced(data.get("what_people_say"), 5),
            "for_interviews": [str(x)[:300] for x in (data.get("for_interviews") or [])[:4] if x],
            "identity": " ".join(str(data.get("identity") or "").split())[:200], "unrelated": sorted(unrelated)}


def _set_aside(found: dict, unrelated: list[str]) -> int:
    """Drop what the sorting step found to be about a namesake. Returns how many findings went."""
    gone = 0
    if "W" in unrelated and found.get("basics"):
        found["basics"], found["facts"] = None, {}
        gone += 1
    for key, letter in (("discussions", "D"),):
        kept = [x for i, x in enumerate(found.get(key) or [], 1) if f"{letter}{i}" not in unrelated]
        gone += len(found.get(key) or []) - len(kept)
        found[key] = kept
    web = found.get("web")
    if web:
        for key, letter in (("news", "N"), ("people", "P")):
            kept = [x for i, x in enumerate(web.get(key) or [], 1) if f"{letter}{i}" not in unrelated]
            gone += len(web.get(key) or []) - len(kept)
            web[key] = kept
    return gone


def _same_site(a: str, b: str) -> bool:
    return bool(a and b) and site_of(a).removeprefix("www.") == site_of(b).removeprefix("www.")


# ---- the agent ----

def fresh(cached: dict | None, depth: str) -> bool:
    if not cached or not cached.get("researched_at"):
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["researched_at"])
    return (cached.get("version") == VERSION and age.days < FRESH_DAYS
            and DEPTHS.index(cached.get("depth", "off")) >= DEPTHS.index(depth))


def research(company: str, *, depth: str, sources: list[str], store, get_model=None, job_url: str = "",
             client: httpx.Client | None = None, throttle=sites, force: bool = False, tavily_key: str = "",
             context: dict | None = None) -> dict | None:
    """Research one company to `depth` from `sources`; cached per company and reused by every job there.
    `context` (from the job: "title", "domain", "about") says which company of that name is meant."""
    key = company_key(company)
    if not key or depth not in DEPTHS or depth == "off":
        return None
    cached = store.get_research(key)
    if not force and fresh(cached, depth):
        feed.emit("step", f"reusing the research on {company} from {cached['researched_at'][:10]}")
        return cached
    context = dict(context or {})
    own_site = employer_site(job_url, {})
    if own_site:
        context.setdefault("site", own_site)
    found = {"company": company, "version": VERSION, "depth": depth, "researched_at": datetime.now(timezone.utc).isoformat(), "basics": None,
             "facts": {}, "website": None, "discussions": [], "web": None, "summary": None, "links": links(company, sources),
             "used": [], "skipped": [{"source": SOURCES[s][0], "why": NOT_FETCHED, "linked": True}
                                     for s in sources if not SOURCES[s][1]]}
    own = client is None
    client = client or httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        if "wikipedia" in sources:
            found["basics"] = _step("Wikipedia", lambda: wikipedia(company, client, throttle), found)
        if depth == "thorough":
            qid = (found["basics"] or {}).get("wikidata")
            if "wikidata" in sources and qid:
                found["facts"] = _step("Wikidata", lambda: wikidata_facts(qid, client, throttle), found) or {}
            listed = found["facts"].get("Website", "")
            if own_site and listed.startswith("http") and not _same_site(own_site, listed):
                # The job is on the employer's own site, and the Wikipedia page's company has a different one.
                found["skipped"].append({"source": "Wikipedia", "why": f"its page on “{found['basics']['title']}” is about "
                                         f"another organisation (website {site_of(listed)}, not {site_of(own_site)})"})
                feed.emit("warn", f"! research: set aside Wikipedia's “{found['basics']['title']}”: a different organisation")
                found["basics"], found["facts"] = None, {}
                found["used"] = [u for u in found["used"] if u not in ("Wikipedia", "Wikidata")]
            home = employer_site(job_url, found["facts"])
            if "website" in sources and home:
                found["website"] = _step("Company website", lambda: website_about(home, client, throttle), found)
            if "hackernews" in sources:
                found["discussions"] = _step("Hacker News", lambda: hackernews(company, client, throttle), found) or []
            if "tavily" in sources and tavily_key:
                web = _step("Tavily", lambda: tavily(company, tavily_key, client, throttle, context), found)
                found["web"] = web if web and not web.get("note") else None
            elif "tavily" in sources:
                found["skipped"].append({"source": "Tavily", "why": "add your Tavily API key in Settings to use it"})
            material = (found["basics"] or found["facts"] or (found["website"] or {}).get("text") or found["discussions"]
                        or found["web"])
            if material and get_model is not None:
                found["summary"] = _step("LLM", lambda: _organise(company, found, get_model(), context), found)
                gone = _set_aside(found, (found["summary"] or {}).get("unrelated") or [])
                if gone:
                    feed.emit("step", f"research: set aside {gone} finding{'s' if gone != 1 else ''} about something else named {company}")
                    found["set_aside"] = gone
    finally:
        if own:
            client.close()
    store.save_research(key, found)
    feed.emit("ok", f"✓ research on {company}: " + (", ".join(found["used"]) or "nothing found") +
              (f" · {len(found['links'])} review links" if found["links"] else ""))
    return found


def _step(name: str, fetch, found: dict):
    try:
        result = fetch()
    except Refused as exc:
        found["skipped"].append({"source": name, "why": str(exc)})
        feed.emit("warn", f"! research: {name} {exc}")
        return None
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        found["skipped"].append({"source": name, "why": f"couldn't be read ({type(exc).__name__})"})
        feed.emit("warn", f"! research: {name} couldn't be read ({type(exc).__name__})")
        return None
    except Exception as exc:  # an LLM error: keep everything else that was found
        found["skipped"].append({"source": name, "why": str(exc)[:200]})
        feed.emit("warn", f"! research: {name}: {str(exc)[:120]}")
        return None
    if isinstance(result, dict) and (result.get("allowed") is False or ("note" in result and not result.get("text"))):
        found["skipped"].append({"source": name, "why": result["note"]})
        feed.emit("step", f"research: {name} skipped, {result['note']}")
        return result
    if result:
        found["used"].append(name)
        feed.emit("step", f"research: {name} ✓")
    return result
