"""Accounts the assistant helped create on job sites (Workday and others).

Passwords go to your operating system's own password store (Windows Credential Manager, macOS
Keychain, or the Secret Service on Linux) through the `keyring` package. They are never written to
the app's database and never sent to the LLM. Without a password store, a new password is shown to
you once so you can save it yourself.
"""
import csv
import io
import secrets
import string
from urllib.parse import urlparse

from cv_maker.jobs.urls import site_of

SERVICE = "CV Tailor"
# Application systems many employers share, each employer with its own accounts (acme.wd3.myworkdayjobs.com and
# globex.wd5.myworkdayjobs.com are different logins): on these an account belongs to the full address.
MULTI_TENANT = {"myworkdayjobs.com", "myworkday.com", "icims.com", "taleo.net", "successfactors.com", "successfactors.eu",
                "brassring.com", "oraclecloud.com", "avature.net", "eightfold.ai", "phenompeople.com", "sapsf.com", "sapsf.eu"}


def account_key(address: str) -> str:
    """The site an account is for, from a page's address or what you typed: its site (acme.com), or on a shared
    application system its own address (acme.wd3.myworkdayjobs.com)."""
    address = (address or "").strip()
    url = address if "://" in address else "https://" + address
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    site = site_of(url) if host else ""
    return host if site in MULTI_TENANT else site


def generate_password(length: int = 18) -> str:
    """Strong and accepted by typical sign-up rules: upper, lower, digit and symbol."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^*-_=+"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in password) and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password) and any(c in "!@#$%^*-_=+" for c in password)):
            return password


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    return None if isinstance(keyring.get_keyring(), fail.Keyring) else keyring


def keychain_name() -> str:
    import sys

    return {"win32": "Windows Credential Manager", "darwin": "macOS Keychain"}.get(sys.platform, "your system keyring")


def can_store() -> bool:
    return _keyring() is not None


def store_password(site: str, email: str, password: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    kr.set_password(SERVICE, f"{site} {email}", password)
    return True


def get_password(site: str, email: str) -> str | None:
    kr = _keyring()
    return kr.get_password(SERVICE, f"{site} {email}") if kr else None


def forget_password(site: str, email: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, f"{site} {email}")
        except Exception:
            pass  # already gone


def list_accounts(store) -> list[dict]:
    return store.get_preference("apply_accounts") or []


def remember_account(store, site: str, email: str, in_keychain: bool) -> None:
    from datetime import datetime, timezone

    accounts = [a for a in list_accounts(store) if not (a["site"] == site and a["email"] == email)]
    accounts.append({"site": site, "email": email, "in_keychain": in_keychain,
                     "created_at": datetime.now(timezone.utc).isoformat()})
    store.set_preference("apply_accounts", accounts)


def forget_account(store, site: str, email: str) -> None:
    forget_password(site, email)
    store.set_preference("apply_accounts", [a for a in list_accounts(store) if not (a["site"] == site and a["email"] == email)])


# The CSV most password managers import (Bitwarden, 1Password, Chrome, Edge, Firefox, Proton Pass, KeePassXC).
EXPORT_COLUMNS = ["name", "url", "username", "password", "note"]


def export_rows(store) -> tuple[list[dict], int]:
    """(accounts with their saved password, how many have no password saved). Read from the password store now."""
    rows, missing = [], 0
    for a in list_accounts(store):
        try:
            password = get_password(a["site"], a["email"]) if a.get("in_keychain") else None
        except Exception:
            password = None  # the password store refused: counted as missing, never guessed
        if not password:
            missing += 1
            continue
        rows.append({"name": a["site"], "url": f"https://{a['site']}/", "username": a["email"], "password": password,
                     "note": f"Job-site account created with CV Tailor on {str(a.get('created_at', ''))[:10]}"})
    return rows, missing


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
