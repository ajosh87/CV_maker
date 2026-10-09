import re
from urllib.parse import parse_qs, urlparse


def split_urls(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_url_lines(text: str) -> tuple[list[str], list[str]]:
    """Return (valid unique http(s) URLs in order, invalid lines)."""
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for line in split_urls(text):
        parsed = urlparse(line)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or " " in line:
            invalid.append(line)
            continue
        key = normalize_job_url(line)
        if key in seen:
            continue
        seen.add(key)
        valid.append(line)
    return valid, invalid


def linkedin_job_id(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("linkedin.com"):
        return None
    current = parse_qs(parsed.query).get("currentJobId")
    if current and current[0].isdigit():
        return current[0]
    match = re.search(r"/jobs/view/(?:[^/]*?-)?(\d{6,})/?", parsed.path)
    return match.group(1) if match else None


def site_of(url: str) -> str:
    """The registrable part of a host ("jobs.acme.co.uk" -> "acme.co.uk"), to tell one website from another."""
    host = re.sub(r"^https?://", "", url or "").split("/")[0].split(":")[0].casefold()
    if re.fullmatch(r"[\d.]+", host) or host == "localhost":
        return host  # an address, not a name: 127.0.0.1 is one site, never "0.1"
    parts = host.split(".")
    if len(parts) > 2 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "ac", "org", "net", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def normalize_job_url(url: str) -> str:
    """Map LinkedIn search/collection/slug links onto the public job page the guest view serves."""
    job_id = linkedin_job_id(url)
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}/"
    return url.strip()
