import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from cv_maker.paths import make_private_file

logger = logging.getLogger("cv_maker")

_DEFAULTS: dict[str, Any] = {
    "LLM_PROVIDER": "openai",
    "LLM_MODEL": "gpt-4o",
    "LLM_API_KEY": "",
    "AZURE_OPENAI_ENDPOINT": "",
    "AWS_DEFAULT_REGION": "us-east-1",
    "OLLAMA_HOST": "http://localhost:11434",
}

SETTING_KEYS = tuple(_DEFAULTS)

# How much the app may do on its own, and how fast. Not secret; saved in the same file.
_AUTOMATION_DEFAULTS: dict[str, str] = {
    "LLM_RPM": "",  # LLM requests per minute; empty: the suggestion for your provider and model
    "LLM_CONCURRENCY": "",  # LLM requests at the same time; empty: suggested
    "LLM_DAILY_CAP": "",  # LLM requests per day; empty: suggested (only free models have one)
    "SCAN_APPLICATIONS": "1",  # check what each application needs, read-only, as soon as a job is analysed
    "APPLY_ALONGSIDE": "0",  # start filling in the application while the CV is being written
    "PREP_AUTO": "1",  # write interview prep notes once the CV is written
    "RESEARCH_DEPTH": "simple",  # company research: off | simple | thorough (see research.py)
    "RESEARCH_SOURCES": "wikipedia,wikidata,website,hackernews,glassdoor,indeed,ambitionbox,reddit",
    "RESEARCH_AUTO": "1",  # research the company as soon as a job is analysed (otherwise only when asked)
    "POLITENESS": "standard",  # gentle | standard | brisk (see jobs/polite.py)
}
AUTOMATION_KEYS = tuple(_AUTOMATION_DEFAULTS)
# Your own keys for other services (bring your own key). Saved like the LLM key: never shown again, never exported.
_SERVICE_KEYS = ("TAVILY_API_KEY",)
_ALL_KEYS = set(_DEFAULTS) | set(_AUTOMATION_DEFAULTS) | set(_SERVICE_KEYS)


def suggested_limits(provider: str, model: str) -> dict:
    """Limits that keep well inside what the provider allows, so requests are never refused as abuse."""
    provider, model = (provider or "").lower(), (model or "").lower()
    if provider == "openrouter" and model.endswith(":free"):
        return {"rpm": 15, "concurrency": 1, "daily": 45,
                "why": "OpenRouter's free models allow 20 requests a minute and 50 a day (1,000 a day once you've bought credits)."}
    if provider == "groq":
        return {"rpm": 25, "concurrency": 1, "daily": 0, "why": "Groq's free tier allows about 30 requests a minute."}
    if provider == "ollama":
        return {"rpm": 0, "concurrency": 1, "daily": 0, "why": "Runs on this computer: one request at a time keeps it responsive."}
    return {"rpm": 50, "concurrency": 2, "daily": 0, "why": "Comfortably inside paid-tier limits."}


def automation_settings(data_dir: Path) -> dict[str, str]:
    saved = load_saved_settings(data_dir)
    return {key: str(saved.get(key, default)) for key, default in _AUTOMATION_DEFAULTS.items()}


def llm_limits(data_dir: Path) -> dict:
    """The limits in force: yours where you set them, otherwise the suggestion for your provider and model."""
    values, _ = effective_settings(data_dir)
    chosen = automation_settings(data_dir)
    suggested = suggested_limits(values.get("LLM_PROVIDER", ""), values.get("LLM_MODEL", ""))

    def pick(key: str, name: str) -> int:
        try:
            return max(0, int(chosen[key])) if str(chosen[key]).strip() else suggested[name]
        except ValueError:
            return suggested[name]

    return {"rpm": pick("LLM_RPM", "rpm"), "concurrency": max(1, pick("LLM_CONCURRENCY", "concurrency")),
            "daily": pick("LLM_DAILY_CAP", "daily"), "suggested": suggested}


def save_automation(data_dir: Path, values: dict[str, Any]) -> dict[str, Any]:
    current = load_saved_settings(data_dir)
    for key in AUTOMATION_KEYS:
        if key in values:
            current[key] = str(values[key] or "").strip()
    _write(data_dir, current)
    return current


def tavily_key(data_dir: Path) -> tuple[str, str]:
    """(your Tavily key, where it came from: "settings", "env" or "")."""
    saved = str(load_saved_settings(data_dir).get("TAVILY_API_KEY") or "")
    if saved:
        return saved, "settings"
    env = os.environ.get("TAVILY_API_KEY", "").strip()
    return (env, "env") if env else ("", "")


def save_tavily_key(data_dir: Path, key: str = "", *, clear: bool = False) -> None:
    """Save a new key (a blank one keeps the saved key), or remove the saved one."""
    current = load_saved_settings(data_dir)
    if clear:
        current.pop("TAVILY_API_KEY", None)
    elif key.strip():
        current["TAVILY_API_KEY"] = key.strip()
    else:
        return
    _write(data_dir, current)


def _write(data_dir: Path, data: dict) -> None:
    """Replace the settings file in one step, so a crash or a second save at the same moment can't leave it half
    written (a broken file would quietly lose your keys)."""
    path = _settings_path(data_dir)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    make_private_file(temp)  # holds API keys
    os.replace(temp, path)


def _settings_path(data_dir: Path) -> Path:
    return data_dir / "settings.json"


def load_saved_settings(data_dir: Path) -> dict[str, Any]:
    """Only the values the user saved on the Settings page (no defaults, no env)."""
    path = _settings_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in _ALL_KEYS}


def load_settings(data_dir: Path) -> dict[str, Any]:
    return {**_DEFAULTS, **load_saved_settings(data_dir)}


def effective_settings(data_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve config as defaults <- environment/.env <- Settings page.

    Returns (values, sources) where sources maps each key to "settings", "env" or "default".
    """
    saved = load_saved_settings(data_dir)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, default in _DEFAULTS.items():
        if saved.get(key):
            values[key], sources[key] = saved[key], "settings"
        elif os.environ.get(key):
            values[key], sources[key] = os.environ[key], "env"
        else:
            values[key], sources[key] = default, "default"
    return values, sources


def save_settings(data_dir: Path, values: dict[str, Any], clear_api_key: bool = False) -> dict[str, Any]:
    """Persist submitted values. A blank API key keeps the one already saved unless `clear_api_key`."""
    current = load_saved_settings(data_dir)
    for key, value in values.items():
        if key not in _DEFAULTS:
            continue
        value = (value or "").strip()
        if key == "LLM_API_KEY" and not value:
            continue
        current[key] = value
    if clear_api_key and not (values.get("LLM_API_KEY") or "").strip():
        current.pop("LLM_API_KEY", None)
    _write(data_dir, current)
    return current


def get_secret_key(data_dir: Path) -> str:
    """Per-install random key, created on first run inside the user's data folder.

    A SECRET_KEY from the environment is only used if it is long enough to be a real secret;
    short placeholders such as a copied example value are ignored.
    """
    env_key = os.environ.get("SECRET_KEY", "")
    if len(env_key) >= 32:
        return env_key
    if env_key:
        logger.warning("Ignoring SECRET_KEY from the environment: shorter than 32 characters. Using the per-install key.")
    path = data_dir / "secret_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    path.write_text(key, encoding="utf-8")
    make_private_file(path)
    return key
