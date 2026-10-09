from cv_maker.jobs.fetch import FetchResult
from cv_maker.models import JobRun


def apply_fetch_result(run: JobRun, result: FetchResult) -> None:
    """Copy a fetch outcome onto a run. A failed fetch never discards a description already on the run."""
    run.fetch_ok = result.ok
    run.fetch_reason = result.reason
    run.title = run.title or result.title
    run.company = run.company or result.company
    run.apply_url = getattr(result, "apply_url", "") or run.apply_url
    run.easy_apply = getattr(result, "easy_apply", False) or run.easy_apply
    run.offsite_apply = getattr(result, "offsite_apply", False) or run.offsite_apply
    if result.ok and result.text.strip():
        run.jd_text = result.text
        run.status = "needs_answers"
    elif run.jd_text.strip():
        run.status = "needs_answers"
    else:
        run.status = "needs_paste"
