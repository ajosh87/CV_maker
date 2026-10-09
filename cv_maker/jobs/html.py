"""Job page HTML -> description text.

All site selectors live here. Extraction fails closed: if no known container (or JobPosting
structured data) is found, the result is empty and the run goes to the paste step instead of
guessing from the whole page.
"""
import html as html_lib
import html.parser
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

# Containers that hold the job description, in priority order: (attribute, value).
_DESCRIPTION_SELECTORS = [
    ("class", "show-more-less-html__markup"),  # LinkedIn public/guest page
    ("class", "description__text"),  # LinkedIn public/guest page (outer wrapper)
    ("class", "jobs-description__content"),  # LinkedIn signed-in page
    ("class", "jobs-description-content__text"),
    ("class", "jobs-box__html-content"),
    ("class", "jobs-description"),
    ("id", "jobDescriptionText"),  # Indeed
    ("class", "job-description"),
    ("id", "job-description"),
    ("class", "jobDescription"),
    ("id", "jobDescription"),
]
_TITLE_SELECTORS = [("class", "top-card-layout__title"), ("class", "topcard__title"), ("class", "jobsearch-JobInfoHeader-title")]
_COMPANY_SELECTORS = [("class", "topcard__org-name-link"), ("class", "topcard__flavor")]
_AUTH_MARKERS = re.compile(r"authwall|join to view|sign in to view|sign in to see|login-form", re.IGNORECASE)

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SKIP = {"script", "style", "noscript", "template", "svg", "button", "head"}
_BLOCK = {
    "p", "div", "section", "article", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "table", "blockquote", "pre", "header", "footer", "dd", "dt",
}


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent: "_Node | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list = []
        self.parent = parent

    def matches(self, attr: str, value: str) -> bool:
        raw = self.attrs.get(attr) or ""  # valueless attributes (<div class>) come through as None
        if attr == "class":
            return value in raw.split()
        return raw == value


class _TreeBuilder(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list) -> None:
        node = _Node(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self._stack[-1].children.append(_Node(tag, dict(attrs), self._stack[-1]))

    def handle_endtag(self, tag: str) -> None:
        # Close the nearest matching open element, implicitly closing anything left open inside it.
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def _parse(markup: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(markup)
    builder.close()
    return builder.root


def _find(node: _Node, attr: str, value: str) -> _Node | None:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.matches(attr, value):
            return current
        stack.extend(reversed([c for c in current.children if isinstance(c, _Node)]))
    return None


def _find_all(node: _Node, tag: str) -> list[_Node]:
    found, stack = [], [node]
    while stack:
        current = stack.pop()
        if current.tag == tag:
            found.append(current)
        stack.extend(c for c in current.children if isinstance(c, _Node))
    return found


def _text(node: _Node) -> str:
    parts: list[str] = []

    def walk(n: _Node) -> None:
        for child in n.children:
            if isinstance(child, str):
                parts.append(child)
                continue
            if child.tag in _SKIP:
                continue
            if child.tag == "br":
                parts.append("\n")
                continue
            block = child.tag in _BLOCK
            if block:
                parts.append("\n")
            if child.tag == "li":
                parts.append("• ")
            walk(child)
            if block:
                parts.append("\n")

    walk(node)
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(parts).split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_fragment_text(fragment: str) -> str:
    return _text(_parse(html_lib.unescape(fragment) if "&lt;" in fragment else fragment))


def _job_posting_ld(root: _Node) -> dict | None:
    for script in _find_all(root, "script"):
        if (script.attrs.get("type") or "").lower() != "application/ld+json":
            continue
        raw = "".join(c for c in script.children if isinstance(c, str)).strip()
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict):
                kind = item.get("@type")
                kinds = kind if isinstance(kind, list) else [kind]
                if "JobPosting" in kinds:
                    return item
    return None


@dataclass
class JobPage:
    description: str = ""
    title: str = ""
    company: str = ""
    auth_wall: bool = False
    apply_url: str = ""  # the employer's own application page, when the posting links to it
    easy_apply: bool = False  # the posting says you apply on this site itself
    offsite_apply: bool = False  # its Apply button leads to the employer's own site (LinkedIn shows the link signed in)


# LinkedIn's public page used to keep the "Apply on company website" link in a hidden comment; it now shows the link
# only to signed-in members, and marks the button as leading off LinkedIn.
_LINKEDIN_APPLY = re.compile(r'id="applyUrl"[^>]*>\s*<!--\s*"([^"]+)"\s*-->')
_OFFSITE = re.compile(r"apply-button__offsite|apply-link-offsite|offsite-apply")
_EASY = re.compile(r">\s*Easy Apply\s*<", re.IGNORECASE)
_LINK = re.compile(r"https?://[^\s\"'<>)\]]+")
_APPLY_HINT = re.compile(r"career|/jobs?\b|/job/|apply|recruit|vacanc|position|opening|myworkdayjobs|greenhouse\.io|lever\.co|"
                         r"smartrecruiters|icims|taleo|successfactors|eightfold|ashbyhq|workable|jobvite|teamtailor|personio|"
                         r"recruitee|bamboohr|oraclecloud|brassring|avature", re.IGNORECASE)


def _off_linkedin(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and not (host == "linkedin.com" or host.endswith(".linkedin.com") or host == "lnkd.in")


def _apply_url(markup: str, description: str = "") -> str:
    """The employer's own application page: LinkedIn's hidden link where it still exists, else a careers or
    application link in the description itself. Never a LinkedIn page (that's the posting, not the application)."""
    match = _LINKEDIN_APPLY.search(markup)
    if match:
        url = html_lib.unescape(match.group(1))
        target = (parse_qs(urlparse(url).query).get("url") or [url])[0]
        if _off_linkedin(target):
            return target
    for url in _LINK.findall(html_lib.unescape(description or "")):
        url = url.rstrip(".,;:")
        if _off_linkedin(url) and _APPLY_HINT.search(url):
            return url
    return ""


def _first_text(root: _Node, selectors: list[tuple[str, str]]) -> str:
    for attr, value in selectors:
        node = _find(root, attr, value)
        if node is not None:
            text = " ".join(_text(node).split())
            if text:
                return text
    return ""


def parse_job_page(markup: str) -> JobPage:
    root = _parse(markup)
    page = JobPage()

    posting = _job_posting_ld(root)
    if posting:
        page.description = _html_fragment_text(str(posting.get("description") or ""))
        page.title = str(posting.get("title") or "")
        org = posting.get("hiringOrganization")
        page.company = str(org.get("name") or "") if isinstance(org, dict) else str(org or "")
        page.easy_apply = posting.get("directApply") is True
    page.offsite_apply = bool(_OFFSITE.search(markup))
    page.easy_apply = (page.easy_apply or bool(_EASY.search(markup))) and not page.offsite_apply
    page.apply_url = _apply_url(markup, str((posting or {}).get("description") or ""))

    if not page.description:
        for attr, value in _DESCRIPTION_SELECTORS:
            node = _find(root, attr, value)
            if node is not None:
                text = _text(node)
                if text:
                    page.description = text
                    break

    if not page.apply_url and page.description:
        page.apply_url = _apply_url("", page.description)
    page.title = page.title or _first_text(root, _TITLE_SELECTORS)
    page.company = page.company or _first_text(root, _COMPANY_SELECTORS)
    if not page.description:
        page.auth_wall = bool(_AUTH_MARKERS.search(markup))
    return page


def extract_job_description(markup: str) -> str:
    return parse_job_page(markup).description


def page_text(markup: str, limit: int = 4000) -> str:
    """The main readable text of an ordinary web page (its <main>, <article> or <body>)."""
    root = _parse(markup)
    for tag in ("main", "article", "body"):
        nodes = _find_all(root, tag)
        if nodes:
            return _text(nodes[-1] if tag != "body" else nodes[0])[:limit]
    return _text(root)[:limit]


def page_links(markup: str) -> list[tuple[str, str]]:
    """(link text, href) for every link on a page."""
    out = []
    for a in _find_all(_parse(markup), "a"):
        href = (a.attrs.get("href") or "").strip()
        if href:
            out.append((" ".join(_text(a).split()), href))
    return out
