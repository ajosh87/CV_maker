"""Where each user's personal data lives.

Data never belongs in the project folder: a fresh clone starts empty and every user's CVs, profile,
generated documents and API key stay in their own OS account. Resolution order:

1. an explicit path (tests, embedding)
2. the DATA_DIR environment variable (or .env)
3. ./data, only if an older install already keeps its database there
4. the per-user application-data folder for this OS
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("cv_maker")

LEGACY_DIR = Path("data")


def user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "CVTailor"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CVTailor"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "cv-tailor"


def resolve_data_dir(explicit: Path | str | None = None) -> tuple[Path, str]:
    """Returns (path, source) where source is one of: argument, DATA_DIR, legacy, default. The path is absolute,
    so every file path saved under it is too: a relative one would be read from wherever the reader happens to
    look (Flask reads relative paths from its package folder, not the folder the app started in)."""
    if explicit:
        return Path(explicit).absolute(), "argument"
    if os.environ.get("DATA_DIR"):
        return Path(os.environ["DATA_DIR"]).expanduser().absolute(), "DATA_DIR"
    if (LEGACY_DIR / "cv_maker.sqlite").exists():
        return LEGACY_DIR.absolute(), "legacy"
    return user_data_dir(), "default"


def make_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)  # Windows: the per-user AppData ACLs already restrict access


def make_private_file(path: Path) -> None:
    if os.name == "posix" and path.exists():
        os.chmod(path, 0o600)


def warn_if_committable(data_dir: Path) -> bool:
    """Log a warning if the data folder sits inside a git work tree without being ignored."""
    try:
        resolved = data_dir.resolve()
        top = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if top.returncode != 0:
            return False  # not inside a repository
        ignored = subprocess.run(
            ["git", "-C", top.stdout.strip(), "check-ignore", "-q", str(resolved / "cv_maker.sqlite")],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if ignored.returncode != 0:
        logger.warning(
            "Your data folder %s is inside a git repository and is NOT ignored. Personal data could be "
            "committed. Add it to .gitignore or set DATA_DIR to a folder outside the repository.", resolved,
        )
        return True
    return False
