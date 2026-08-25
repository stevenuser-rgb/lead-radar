import asyncio
import datetime
from typing import Any, Dict

from database import (
    create_facebook_job,
    get_facebook_jobs,
    get_active_facebook_jobs,
    get_facebook_sources,
    get_setting,
    clean_facebook_posts,
    set_setting,
    update_facebook_job,
)
from facebook_runner import FacebookRunnerError, submit_facebook_job
from facebook_cookie_store import runner_cookie_path


def _minutes_since(value: str) -> float:
    try:
        created = datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return (datetime.datetime.now() - created).total_seconds() / 60
    except (TypeError, ValueError):
        return float("inf")


def _source_due(source: Dict[str, Any], jobs: list[Dict[str, Any]], interval_minutes: int, scan_mode: str = "normal") -> bool:
    source_jobs = [
        job for job in jobs
        if job.get("source_id") == source["id"] and (job.get("scan_mode") or "normal") == scan_mode
    ]
    all_source_jobs = [job for job in jobs if job.get("source_id") == source["id"]]
    if any(job.get("status") in {"queued", "running"} for job in all_source_jobs):
        return False
    latest = source_jobs[0] if source_jobs else None
    return latest is None or _minutes_since(latest.get("created_at")) >= interval_minutes


def _start_due_jobs(interval_minutes: int):
    if get_setting("facebook_scan_enabled", "1") != "1":
        return
    if get_setting("facebook_auto_scan_enabled", "0") != "1":
        return
    if get_active_facebook_jobs("deep"):
        return

    jobs = get_facebook_jobs(limit=100)
    for source in get_facebook_sources(active_only=True):
        if source.get("monitor_enabled"):
            continue
        if not _source_due(source, jobs, interval_minutes):
            continue
        job_id = create_facebook_job(source)
        try:
            remote = submit_facebook_job(
                source["group_url"],
                min(500, int(source.get("max_posts") or 100)),
                bool(source.get("no_proxy")),
                "",
            )
            update_facebook_job(
                job_id,
                runner_job_id=remote.get("id"),
                status=remote.get("status", "running"),
                started_at=remote.get("startedAt"),
            )
        except (FacebookRunnerError, ValueError) as exc:
            update_facebook_job(
                job_id,
                status="failed",
                error=str(exc),
                finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
            )


def _start_due_monitor_jobs(interval_minutes: int):
    if get_setting("facebook_scan_enabled", "1") != "1":
        return
    if get_setting("facebook_monitor_enabled", "0") != "1":
        return
    if get_active_facebook_jobs("deep"):
        return

    jobs = get_facebook_jobs(limit=100)
    for source in get_facebook_sources(active_only=True):
        if not source.get("monitor_enabled"):
            continue
        if not _source_due(source, jobs, interval_minutes, scan_mode="monitor"):
            continue
        monitor_source = dict(source)
        monitor_source["max_posts"] = min(50, int(source.get("max_posts") or 50))
        job_id = create_facebook_job(monitor_source, scan_mode="monitor")
        try:
            remote = submit_facebook_job(
                source["group_url"],
                monitor_source["max_posts"],
                bool(source.get("no_proxy")),
                runner_cookie_path(source.get("cookies_file", "")),
                no_new_post_cycles=3,
                monitor=True,
                source_key=f"source-{source['id']}",
            )
            update_facebook_job(
                job_id,
                runner_job_id=remote.get("id"),
                status=remote.get("status", "running"),
                started_at=remote.get("startedAt"),
            )
        except (FacebookRunnerError, ValueError) as exc:
            update_facebook_job(
                job_id,
                status="failed",
                error=str(exc),
                finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
            )


def _sync_running_jobs():
    # Import lazily to avoid a module cycle during FastAPI startup.
    from app import _queue_completed_facebook_analysis, _sync_facebook_job

    for job in get_facebook_jobs(limit=100):
        if job.get("status") in {"queued", "running"} and job.get("runner_job_id"):
            try:
                _sync_facebook_job(job)
            except Exception as exc:
                update_facebook_job(job["id"], status="error", error=str(exc))
    _queue_completed_facebook_analysis()


def _maybe_cleanup_facebook_records():
    """Run retention cleanup once per local day, without touching qualified leads."""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if get_setting("facebook_last_retention_cleanup", "") == today:
        return
    try:
        retention_days = min(365, max(30, int(get_setting("facebook_retention_days", "90"))))
    except (TypeError, ValueError):
        retention_days = 90
    try:
        clean_facebook_posts(retention_days)
        set_setting("facebook_last_retention_cleanup", today)
    except Exception as exc:
        print(f"[Facebook retention] cleanup failed: {exc}")


async def background_facebook_scheduler_loop():
    while True:
        try:
            interval_minutes = max(30, int(get_setting("facebook_scan_interval_minutes", "60")))
        except (TypeError, ValueError):
            interval_minutes = 60
        _sync_running_jobs()
        _maybe_cleanup_facebook_records()
        _start_due_jobs(interval_minutes)
        try:
            monitor_interval_minutes = max(30, int(get_setting("facebook_monitor_interval_minutes", "60")))
        except (TypeError, ValueError):
            monitor_interval_minutes = 60
        _start_due_monitor_jobs(monitor_interval_minutes)
        await asyncio.sleep(60)
