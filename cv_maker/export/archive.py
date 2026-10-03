"""Export my data: one zip with everything the app holds about the user, minus the API key."""
import io
import json
import re
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from cv_maker.store import Store

_README = """CV Tailor export ({when})

profile.json   your saved profile (everything the app uses as facts about you)
jobs.json      every job: link, description, requirements, questions, your answers, status
privacy.json   the words you chose never to send to the LLM
applications.json  your application details, job-site accounts (no passwords) and every application with what was entered
documents/     every generated CV and cover letter version (.docx)
source-cvs/    the CV files you uploaded
Your settings and API key are not included.
"""


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")[:60] or "job"


def build_export(store: Store) -> io.BytesIO:
    buf = io.BytesIO()
    now = datetime.now(timezone.utc)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", _README.format(when=now.strftime("%Y-%m-%d %H:%M UTC")))
        profile = store.get_profile()
        z.writestr("profile.json", json.dumps(asdict(profile) if profile else {}, indent=2, ensure_ascii=False))
        runs = store.list_runs()
        z.writestr("jobs.json", json.dumps([asdict(r) for r in runs], indent=2, ensure_ascii=False))
        z.writestr("privacy.json", json.dumps({"never_send": store.get_never_send()}, indent=2, ensure_ascii=False))
        z.writestr("applications.json", json.dumps({
            "details": store.get_preference("apply_details") or {},
            "accounts": store.get_preference("apply_accounts") or [],  # passwords are in your system's password store, not here
            "applications": [asdict(a) for a in store.list_applications()],
        }, indent=2, ensure_ascii=False))
        # The short id keeps names unique when two jobs share a title/company or two uploads share a file name.
        folders = {r.id: f"{_slug(' '.join(x for x in (r.company, r.title) if x))}-{r.id[:8]}" for r in runs}
        for doc in store.list_documents():
            if Path(doc.path).exists():
                kind = "cv" if doc.kind == "cv" else "cover-letter"
                z.write(doc.path, f"documents/{folders.get(doc.run_id, doc.run_id[:8])}/v{doc.version}-{kind}.docx")
        for upload in store.list_uploads():
            if Path(upload.path).exists():
                z.write(upload.path, f"source-cvs/{upload.created_at[:10]}-{upload.id[:8]}-{Path(upload.filename).name}")
    buf.seek(0)
    return buf
