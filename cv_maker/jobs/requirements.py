from cv_maker.llm.chat import ChatModel, complete_json
from cv_maker.skills import norm, split_requirement


class MustHave:
    def __init__(self, term: str, original: str) -> None:
        self.term = term
        self.original = original


class NiceToHave:
    def __init__(self, term: str, original: str) -> None:
        self.term = term
        self.original = original


class JobRequirements:
    def __init__(
        self,
        must_have: list[MustHave],
        nice_to_have: list[NiceToHave],
        tools: list[str],
        seniority: str,
        domain: str,
        education: str,
        language: str,
        work_auth: str,
        job_title: str = "",
        company: str = "",
        notes: list[str] | None = None,
        responsibilities: list[str] | None = None,
    ) -> None:
        self.must_have = must_have
        self.nice_to_have = nice_to_have
        self.tools = tools
        self.seniority = seniority
        self.domain = domain
        self.education = education
        self.language = language
        self.work_auth = work_auth
        self.job_title = job_title
        self.company = company
        self.notes = notes or []  # things to know before applying, read in the same request (no extra tokens)
        self.responsibilities = responsibilities or []  # what the job is mostly about: steers emphasis and questions

    def to_dict(self) -> dict:
        return {
            "must_have": [{"term": m.term, "original": m.original} for m in self.must_have],
            "nice_to_have": [{"term": n.term, "original": n.original} for n in self.nice_to_have],
            "tools": self.tools,
            "seniority": self.seniority,
            "domain": self.domain,
            "education": self.education,
            "language": self.language,
            "work_auth": self.work_auth,
            "job_title": self.job_title,
            "company": self.company,
            "notes": self.notes,
            "responsibilities": self.responsibilities,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobRequirements":
        return cls(
            must_have=[MustHave(t, o) for t, o in _terms(data.get("must_have"), 20)],
            nice_to_have=[NiceToHave(t, o) for t, o in _terms(data.get("nice_to_have"), 12)],
            tools=[str(t) for t in data.get("tools") or [] if t],
            seniority=_text(data.get("seniority")),
            domain=_text(data.get("domain")),
            education=_text(data.get("education")),
            language=_text(data.get("language")),
            work_auth=_text(data.get("work_auth")),
            job_title=_text(data.get("job_title")),
            company=_text(data.get("company")),
            notes=[_text(n)[:200] for n in (data.get("notes") or []) if _text(n)][:8],
            responsibilities=[_text(r)[:200] for r in (data.get("responsibilities") or []) if _text(r)][:6],
        )


def _text(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    return str(value or "").strip()


def _terms(items, limit: int = 20) -> list[tuple[str, str]]:
    """Accept [{"term","original"}] or plain strings. A term that lists several skills ("Python, Java and React")
    becomes one item per skill, so each is matched, asked about and counted on its own. Empties and duplicates go."""
    out, seen = [], set()
    for item in items or []:
        if isinstance(item, dict):
            term = _text(item.get("term") or item.get("name") or item.get("original"))
            original = _text(item.get("original")) or term
        else:
            term = original = _text(item)
        for part in split_requirement(term):
            if part and norm(part) not in seen:
                seen.add(norm(part))
                out.append((part, original))
    return out[:limit]


_PROMPT = """Read this job description and extract its requirements. Work through it in order before answering:
1. Find what the job is mostly about: the 3-6 responsibilities the posting spends most words on, in plain words.
2. Separate requirements from everything else: skip benefits, company boilerplate, equal-opportunity text and
   generic traits ("team player", "passionate") unless the posting makes one a stated requirement.
3. Sort each requirement: must_have = stated as required ("must", "required", "you have", years of X); nice_to_have =
   preferred, bonus, "plus" or "nice to have". When unclear, a skill named in the responsibilities is a must_have.
4. Split lists into single skills: "Python, Java and React" becomes three items that share the same "original".

Return one JSON object with these keys:
{{
  "job_title": "the role's title as written",
  "company": "hiring company name, or empty string if not stated",
  "responsibilities": ["what the role mostly does, most important first, each under 20 words"],
  "must_have": [{{"term": "ONE skill, tool or qualification, 1-3 words, written as the posting writes it, e.g. Kubernetes", "original": "the exact phrase from the posting"}}],
  "nice_to_have": [same shape as must_have],
  "tools": ["named tools/technologies"],
  "seniority": "", "domain": "the industry or product area, e.g. fintech collections, healthcare AI", "education": "", "language": "", "work_auth": "",
  "notes": ["what a candidate should know before applying, only if the posting says it"]
}}
Keep each term short and in the posting's own wording, since applicant tracking systems match those exact words.
At most 16 must_have and 10 nice_to_have, most important first. Use empty strings or [] when absent.
notes: at most 6 short points such as a closing date, visa sponsorship, on-site/hybrid/relocation, travel,
background or security checks, assessments or tests, documents to provide, a stated salary range.

Job description:
<<<
{jd}
>>>"""


def extract_requirements(jd: str, model: ChatModel) -> JobRequirements:
    return JobRequirements.from_dict(complete_json(model, _PROMPT.format(jd=jd[:20000])))
