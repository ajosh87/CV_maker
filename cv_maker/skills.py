"""How well your profile covers each thing a job asks for, with evidence you can check.

A requirement is first split into single skills ("Python, Java and React" is three). Each is then looked for in
your profile, under its other names too ("RAG" is "retrieval-augmented generation"), and scored from where it was
found: your skills list, the bullets of each role (recent roles count more), your titles, certifications and
projects, and the level you gave when asked. Every point comes with the line that earned it, so a score can always be
traced; nothing here is an LLM's opinion.
"""
import re
from datetime import date

# ---- names ------------------------------------------------------------------------------------------------------

# Different words for the same skill: any of them counts as the skill itself. Lower case; spaces and hyphens are
# interchangeable when matching.
_ALIAS_GROUPS = [
    {"rag", "retrieval augmented generation", "retrieval-augmented generation"},
    {"llm", "llms", "large language model", "large language models"},
    {"genai", "gen ai", "generative ai"},
    {"ml", "machine learning"},
    {"dl", "deep learning"},
    {"nlp", "natural language processing"},
    {"mlops", "ml ops"},
    {"llmops", "llm ops"},
    {"ai agents", "agentic ai", "ai agent", "llm agents", "autonomous agents"},
    {"multimodal", "multi-modal"},
    {"fine-tuning", "fine tuning", "finetuning"},
    {"prompt engineering", "prompt design"},
    {"k8s", "kubernetes"},
    {"js", "javascript", "ecmascript"},
    {"ts", "typescript"},
    {"node.js", "nodejs", "node"},
    {"react", "react.js", "reactjs"},
    {"vue", "vue.js", "vuejs"},
    {"next.js", "nextjs"},
    {"postgres", "postgresql"},
    {"mongo", "mongodb"},
    {"gcp", "google cloud", "google cloud platform"},
    {"aws", "amazon web services"},
    {"azure", "microsoft azure"},
    {"azure openai", "azure open ai", "azure openai service"},
    {"ci/cd", "cicd", "ci cd", "continuous integration and delivery", "continuous integration/continuous delivery"},
    {"iac", "infrastructure as code"},
    {"oop", "object-oriented programming", "object oriented programming"},
    {"rest", "restful", "rest api", "rest apis", "restful apis"},
    {"sklearn", "scikit-learn", "scikit learn"},
    {"pytorch", "torch"},
    {"c#", "c sharp", "csharp"},
    {".net", "dotnet", "dot net"},
    {"golang", "go language"},
    {"tdd", "test-driven development", "test driven development"},
    {"ux", "user experience"},
    {"ui", "user interface"},
    {"bi", "business intelligence"},
    {"etl", "extract transform load"},
    {"sre", "site reliability engineering"},
    {"gpt", "openai gpt"},
]
# Umbrella names: when the posting asks for the umbrella, any member is the skill itself; when it asks for one
# member, another member is only related.
_FAMILIES = {
    "azure ai": {"azure openai", "azure ai search", "azure cognitive search", "azure cognitive services", "azure ai services",
                 "azure machine learning", "azure ml", "azure ai foundry", "azure ai studio", "document intelligence",
                 "azure document intelligence", "form recognizer", "azure speech", "azure ai vision", "copilot studio"},
    "cloud": {"aws", "azure", "gcp", "oracle cloud", "ibm cloud"},
    "vector database": {"pinecone", "weaviate", "qdrant", "milvus", "chroma", "chromadb", "faiss", "pgvector",
                        "azure ai search", "elasticsearch", "opensearch"},
    "llm framework": {"langchain", "langgraph", "llamaindex", "llama index", "semantic kernel", "autogen", "crewai", "haystack"},
    "deep learning framework": {"pytorch", "tensorflow", "keras", "jax"},
    "containers": {"docker", "kubernetes", "podman", "containerd"},
    "relational database": {"postgresql", "mysql", "sql server", "oracle", "sqlite", "mariadb"},
    "nosql": {"mongodb", "cassandra", "dynamodb", "cosmos db", "redis", "couchbase"},
    "frontend framework": {"react", "angular", "vue", "svelte", "next.js"},
}
_FAMILY_HEADS = {
    "azure ai": {"azure ai", "azure ai services", "azure ai platform", "microsoft ai", "azure cognitive services"},
    "cloud": {"cloud", "cloud platform", "cloud platforms", "public cloud", "cloud computing", "cloud services"},
    "vector database": {"vector database", "vector databases", "vector db", "vector store", "vector stores", "vector search"},
    "llm framework": {"llm framework", "llm frameworks", "llm orchestration", "agent frameworks", "agentic frameworks"},
    "deep learning framework": {"deep learning framework", "deep learning frameworks", "ml frameworks", "ml framework"},
    "containers": {"containers", "containerization", "containerisation", "container orchestration"},
    "relational database": {"relational database", "relational databases", "rdbms", "sql databases"},
    "nosql": {"nosql", "nosql databases", "non-relational databases"},
    "frontend framework": {"frontend framework", "frontend frameworks", "front-end frameworks", "javascript frameworks"},
}
# A skill that can't be used without another: knowing the first is evidence of the second.
_IMPLIES = {"sql": {"postgresql", "postgres", "mysql", "sql server", "t-sql", "pl/sql", "sqlite", "oracle", "bigquery",
                    "snowflake", "redshift", "mariadb", "spark sql"},
            "python": {"django", "flask", "fastapi", "pandas", "numpy", "pytorch", "tensorflow", "scikit-learn", "sklearn",
                       "langchain", "langgraph", "llamaindex", "pyspark"},
            "javascript": {"react", "node.js", "nodejs", "vue", "angular", "next.js", "typescript", "express"},
            "azure": {"azure openai", "azure ai search", "azure functions", "azure devops", "azure ml", "azure machine learning",
                      "azure ai foundry", "cosmos db", "azure kubernetes service", "aks"},
            "aws": {"lambda", "ec2", "s3", "sagemaker", "bedrock", "dynamodb", "ecs", "eks"},
            "gcp": {"bigquery", "vertex ai", "cloud run", "gke"},
            "docker": {"kubernetes", "docker compose"},
            "machine learning": {"deep learning", "scikit-learn", "pytorch", "tensorflow", "xgboost"},
            "llm": {"rag", "retrieval augmented generation", "prompt engineering", "langchain", "langgraph", "fine tuning",
                    "gpt", "azure openai", "llamaindex"},
            "generative ai": {"llm", "llms", "rag", "retrieval augmented generation", "azure openai", "gpt", "diffusion models"},
            "nlp": {"llm", "llms", "transformers", "hugging face", "spacy"}}

# Acronyms worth writing out once on a CV, so either form a recruiter searches for is found.
ACRONYMS = {"rag": "Retrieval-Augmented Generation", "llm": "Large Language Models", "nlp": "Natural Language Processing",
            "ml": "Machine Learning", "dl": "Deep Learning", "genai": "Generative AI", "iac": "Infrastructure as Code",
            "ci/cd": "Continuous Integration/Continuous Delivery", "tdd": "Test-Driven Development",
            "etl": "Extract, Transform, Load", "sre": "Site Reliability Engineering", "oop": "Object-Oriented Programming"}
# Words that make a requirement a list of skills when they join known names ("Python and Java").
_KNOWN = set().union(*_ALIAS_GROUPS, *_FAMILIES.values(), *_FAMILY_HEADS.values()) | {
    "python", "java", "c", "c++", "go", "rust", "scala", "kotlin", "swift", "ruby", "php", "r", "sql", "bash", "matlab",
    "spark", "hadoop", "kafka", "airflow", "dbt", "snowflake", "databricks", "tableau", "power bi", "excel", "django",
    "flask", "fastapi", "spring", "angular", "svelte", "terraform", "ansible", "jenkins", "github actions", "gitlab",
    "git", "linux", "docker", "graphql", "grpc", "redis", "elasticsearch", "tensorflow", "keras", "pandas", "numpy",
    "hugging face", "transformers", "openai", "anthropic", "gemini", "llama", "computer vision", "opencv", "figma",
    "jira", "agile", "scrum", "devops", "microservices", "kubernetes", "helm", "prometheus", "grafana", "selenium",
    "playwright", "cypress", "jest", "pytest", "html", "css", "sass", "tailwind", "webpack", "vite", "android", "ios",
}
_GENERIC_HEADS = {"programming languages", "languages", "frameworks", "tools", "technologies", "databases", "platforms",
                  "libraries", "cloud platforms", "cloud providers", "such as", "e.g", "eg", "including", "like"}
_FILLER = {"etc", "etc.", "similar", "or similar", "and similar", "others", "related technologies", "equivalent",
           "or equivalent", "and/or", "related tools", "similar tools", "the like"}
_PROTECTED = {"ci/cd", "a/b", "a/b testing", "i/o", "tcp/ip", "pl/sql", "ui/ux", "r&d", "p&l", "m&a", "b2b", "b2c",
              "24/7", "and/or", "client/server", "input/output", "os/2"}
_LEADS = re.compile(
    r"^(?:(?:strong|solid|good|excellent|deep|proven|hands-on|practical|working|demonstrable|extensive|advanced|"
    r"basic|some|prior|professional|commercial|production)\s+)*"
    r"(?:experience|knowledge|understanding|proficiency|familiarity|expertise|skills?|background|exposure|fluency)"
    r"\s+(?:with|in|of|using|on|across|building)\s+", re.IGNORECASE)
_STOP = {"and", "or", "the", "with", "for", "experience", "knowledge", "skills", "strong", "years", "using", "of", "in",
         "a", "an", "to", "on", "at", "as", "is", "be", "are", "ability", "understanding", "working", "good", "solid"}


def norm(text: str) -> str:
    """Lower case, one space, hyphens as spaces, no trailing punctuation: 'Retrieval-Augmented  Generation.'."""
    text = re.sub(r"[‐-―]", "-", (text or "").lower())
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    return " ".join(text.split()).strip(" .,:;()")


_ALIASES: dict[str, set[str]] = {}
for _group in _ALIAS_GROUPS:
    _members = {norm(m) for m in _group}
    for _m in _members:
        _ALIASES.setdefault(_m, set()).update(_members)


_TRAILING = re.compile(r"\s+(?:experience|knowledge|skills?|expertise|proficiency|background|familiarity|exposure)$", re.I)


def core(term: str) -> str:
    """The skill itself: "Experience with Kubernetes" and "Kubernetes experience" are both "Kubernetes"."""
    t = _TRAILING.sub("", _LEADS.sub("", " ".join((term or "").split())))
    return t or term


def variants(term: str) -> set[str]:
    """The term and its other names, normalised, with a plural or singular form ("APIs" / "API")."""
    base = norm(term)
    out = {base, norm(core(term))}
    for name in list(out):
        out |= _ALIASES.get(name, set())
    for v in list(out):
        if len(v) > 3 and v.endswith("s") and not v.endswith("ss"):
            out.add(v[:-1])
        elif len(v) >= 2 and not v.endswith("s") and re.fullmatch(r"[a-z]+", v.split()[-1] or ""):
            out.add(v + "s")
    return {v for v in out if v}


def _family_of(name: str) -> str:
    n = norm(name)
    return next((f for f, members in _FAMILIES.items() if n in members or n in {norm(m) for m in members}), "")


def _head_of(name: str) -> str:
    n = norm(name)
    return next((f for f, heads in _FAMILY_HEADS.items() if n in heads), "")


def _pattern(variant: str) -> re.Pattern:
    """Whole words, case-insensitive; spaces or hyphens between words; copes with C++, C#, .NET and Node.js."""
    words = [re.escape(w) for w in re.split(r"[\s\-]+", variant) if w]
    body = r"[\s\-]*".join(words)
    return re.compile(rf"(?<![\w+#]){body}(?![\w+#]|\.\w)", re.IGNORECASE)


def spans(term: str, text: str) -> list[tuple[int, int]]:
    """Where `term` appears in `text` under any of its names: (start, end) for each, in order."""
    found = {(m.start(), m.end()) for v in variants(term) for m in _pattern(v).finditer(text or "")}
    return sorted(found)


def found_in(term: str, text: str) -> str:
    """The words in `text` that name `term` (under any of its names), or ""."""
    for v in sorted(variants(term), key=len, reverse=True):
        m = _pattern(v).search(text or "")
        if m:
            return m.group(0)
    return ""


def mentioned(term: str, text: str) -> str:
    """Like `found_in`, and for an umbrella term ("Azure AI services") any of its members counts."""
    hit = found_in(term, text)
    head = _head_of(term)
    if hit or not head:
        return hit
    return next((found for m in sorted(_FAMILIES[head], key=len, reverse=True) if (found := found_in(m, text))), "")


def is_known(term: str) -> bool:
    n = norm(term)
    return n in _KNOWN or n in _ALIASES or bool(_family_of(n)) or bool(_head_of(n))


# ---- one requirement, several skills ----------------------------------------------------------------------------

def _looks_technical(part: str) -> bool:
    return is_known(part) or bool(re.search(r"[A-Z].*[A-Z]|[+#.]\w|\d", part.strip()))


def split_requirement(text: str) -> list[str]:
    """'Experience with Python, Java, JavaScript/TypeScript and React' -> Python, Java, JavaScript, TypeScript, React.
    A phrase that isn't a list of skills ("Research and development", "CI/CD") stays whole."""
    whole = " ".join((text or "").split()).strip(" .;:")
    if not whole:
        return []
    t = _LEADS.sub("", whole)
    parts: list[str] = []
    m = re.fullmatch(r"(.+?)\s*\((.+)\)\s*(.*)", t)
    if m and re.search(r",|/|\bor\b|\band\b|\be\.?g\b|\bsuch as\b|\blike\b", m.group(2), re.IGNORECASE):
        head, inner, tail = m.group(1).strip(), m.group(2), m.group(3).strip()
        inner = re.sub(r"^(?:e\.?g\.?|such as|like|including|incl\.?)\s*,?\s*", "", inner, flags=re.IGNORECASE)
        if norm(head) not in _GENERIC_HEADS and not head.lower().endswith(tuple(_GENERIC_HEADS)):
            parts.append(head)
        t = inner + (", " + tail if tail else "")
    elif m and len(m.group(2).split()) == 1 and not m.group(3):
        return [m.group(1).strip()]  # "Retrieval-Augmented Generation (RAG)": one skill, written two ways
    has_list = "," in t or ";" in t
    pieces = [p for p in re.split(r"\s*[,;]\s*", t) if p.strip()]
    for piece in pieces:
        piece = re.sub(r"^(?:and|or|plus)\s+", "", piece.strip(), flags=re.IGNORECASE)
        joined = [s for s in re.split(r"\s+(?:and|or|plus|&)\s+", piece, flags=re.IGNORECASE) if s.strip()]
        if len(joined) > 1 and (has_list or all(_looks_technical(s) for s in joined)) and all(len(s.split()) <= 4 for s in joined):
            candidates = joined
        else:
            candidates = [piece]
        for c in candidates:
            c = c.strip(" .")
            if "/" in c and norm(c) not in _PROTECTED and " " not in c.replace(" / ", "/"):
                halves = [h.strip() for h in c.split("/") if h.strip()]
                if len(halves) > 1 and all(len(h) >= 1 for h in halves):
                    parts.extend(halves)
                    continue
            parts.append(c)
    out, seen = [], set()
    for p in parts:
        p = re.sub(r"^(?:including|incl\.?|such as|e\.?g\.?|like)\s+", "", p.strip(" .,;:"), flags=re.IGNORECASE)
        if not p or norm(p) in _FILLER or norm(p) in seen or len(p) > 80:
            continue
        seen.add(norm(p))
        out.append(p)
    return out or [whole]


# ---- evidence -----------------------------------------------------------------------------------------------------

LEVELS = ("beginner", "intermediate", "expert")
LEVEL_LABELS = {"none": "No experience", "beginner": "Beginner", "intermediate": "Intermediate", "expert": "Expert"}
_LEVEL_POINTS = {"beginner": 45, "intermediate": 70, "expert": 85}
STRONG, PARTIAL = 70, 40


def _year(text: str | None) -> int | None:
    m = re.search(r"(19|20)\d{2}", text or "")
    return int(m.group(0)) if m else None


def _recency(exp) -> tuple[float, str]:
    if exp.current:
        return 1.0, "current role"
    end = _year(exp.end)
    if end is None:
        return 0.7, "end date unknown"
    age = date.today().year - end
    if age <= 2:
        return 0.8, f"ended {end}"
    if age <= 5:
        return 0.6, f"ended {end}"
    return 0.4, f"ended {end}"


def _skill_evidence(term: str, skill: str) -> tuple[int, str, bool]:
    """(points, why, same skill?) for one entry of your skills list."""
    t, s = norm(term), norm(skill)
    tv, sv = variants(term), variants(skill)
    if tv & sv:
        return 55, "listed in your skills" if t == s else f"listed as “{skill}”, another name for it", True
    hit = found_in(term, skill)
    if hit:
        return 50, f"your skill “{skill}” includes it", True
    head, fam = _head_of(t), _family_of(s)
    if head and fam == head:
        return 50, f"“{skill}” is one of these", True
    for implied, by in _IMPLIES.items():
        if (implied in tv or variants(implied) & tv) and (sv & {norm(b) for b in by} or any(found_in(b, skill) for b in by)):
            return 40, f"“{skill}” relies on it", True
    if fam and fam == _family_of(t) and s != t:
        return 20, f"related: you list “{skill}”", False
    if len(s) >= 3 and found_in(skill, term) and s not in _STOP:
        return 15, f"part of it: you list “{skill}”", False
    return 0, "", False


def evidence(term: str, profile, level: str = "") -> dict:
    """{"term", "score" 0-100, "strength", "same", "level", "found": [{"where", "text", "why", "points"}]}."""
    found: list[dict] = []
    same = False

    best = (0, "", False, "")
    for skill in getattr(profile, "skills", []) or []:
        named = bool(variants(term) & variants(skill.name) or found_in(term, skill.name))
        if not level and getattr(skill, "level", "") and named:
            level = skill.level
        if getattr(skill, "source", "") == "clarification" and named and getattr(skill, "level", ""):
            continue  # added from your answer with a level: that level below is the evidence, not counted twice
        points, why, is_same = _skill_evidence(term, skill.name)
        if points > best[0]:
            best = (points, why, is_same, skill.name)
    if best[0]:
        found.append({"where": "Skills", "text": best[3], "why": best[1], "points": best[0]})
        same = same or best[2]

    experience_points = 0
    for exp in getattr(profile, "experiences", []) or []:
        weight, when = _recency(exp)
        role = f"{exp.title} at {exp.company}".strip(" at")
        if experience_points < 60 and mentioned(term, exp.title or ""):
            pts = min(round(25 * weight), 60 - experience_points)
            found.append({"where": role, "text": exp.title, "why": f"in your job title ({when})", "points": pts})
            experience_points += pts
            same = True
        for bullet in exp.bullets or []:
            if experience_points >= 60:
                break
            if mentioned(term, bullet):
                pts = min(round(20 * weight), 60 - experience_points)
                found.append({"where": role, "text": bullet, "why": f"in a bullet ({when})", "points": pts})
                experience_points += pts
                same = True

    extra_points = 0
    for key, label in (("certifications", "Certification"), ("projects", "Project"), ("clarifications", "Your answer"),
                       ("languages", "Language")):
        for item in (getattr(profile, "extras", {}) or {}).get(key) or []:
            if extra_points >= 30:
                break
            if mentioned(term, str(item)):
                found.append({"where": label, "text": str(item), "why": "mentioned", "points": 15})
                extra_points += 15
                same = True
    for edu in getattr(profile, "education", []) or []:
        text = f"{edu.credential} {edu.field}"
        if mentioned(term, text):
            found.append({"where": "Education", "text": text.strip(), "why": "in your education", "points": 10})
            same = True
            break

    level_points = 0
    if level in _LEVEL_POINTS:
        level_points = _LEVEL_POINTS[level]
        found.append({"where": "Your answer", "text": LEVEL_LABELS[level], "why": "the level you gave", "points": level_points})
        same = True

    score = min(100, sum(f["points"] for f in found))
    if not same:
        score = min(score, PARTIAL - 1)  # related isn't the same skill: never "partial" or better on its own
    if level == "beginner":
        score = min(score, STRONG - 1)  # you have it, but say so modestly: a beginner level is never "strong"
    strength = "strong" if score >= STRONG else "partial" if score >= PARTIAL else "weak" if score > 0 else "none"
    return {"term": term, "score": score, "strength": strength, "same": same, "level": level,
            "found": sorted(found, key=lambda f: -f["points"])}


STATUS = {"strong": "covered", "partial": "partial", "weak": "missing", "none": "missing"}


# ---- writing at the right level -----------------------------------------------------------------------------------

LEVEL_WORDING = {
    "beginner": "familiar with / foundational knowledge of / exposure to",
    "intermediate": "working knowledge of / hands-on experience with",
    "expert": "expert in / deep expertise in / advanced",
}
_OVERCLAIM = {
    "beginner": r"expert|expertise|advanced|deep|extensive|mastery|master|seasoned|strong|proficient|specialist|senior|lead",
    "intermediate": r"expert|expertise|mastery|master|seasoned|specialist|authority|guru|world[- ]class|deep expertise",
}


def overclaims(text: str, term: str, level: str) -> bool:
    """Whether a sentence says more about `term` than the level you gave."""
    words = _OVERCLAIM.get(level)
    if not words or not found_in(term, text):
        return False
    return re.search(rf"\b(?:{words})\b", text, re.IGNORECASE) is not None
