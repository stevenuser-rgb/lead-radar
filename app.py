import asyncio
import os
import datetime
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, BackgroundTasks, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from database import (
    init_db,
    get_all_keywords,
    add_keyword,
    delete_keyword,
    toggle_keyword,
    get_leads,
    update_lead_status,
    get_skipped_log_page,
    get_summary_stats,
    get_setting,
    set_setting,
    get_facebook_sources,
    create_facebook_source,
    toggle_facebook_source,
    toggle_facebook_source_monitor,
    get_facebook_source,
    create_facebook_job,
    get_facebook_job,
    get_facebook_jobs,
    get_active_facebook_jobs,
    update_facebook_job,
    save_facebook_posts,
    get_facebook_post_page,
    get_facebook_post_summary,
    get_facebook_post,
    get_facebook_posts_for_job,
    update_facebook_post_analysis,
    update_keyword,
    is_post_duplicate,
    save_lead,
    save_skipped,
)
from scheduler import (
    background_scheduler_loop,
    get_next_scan_time_str,
    get_scan_runtime_status,
    is_within_active_hours,
    request_scan_start,
    run_scan_cycle,
)
from facebook_scheduler import background_facebook_scheduler_loop
from notifier import send_lead_notification
from scraper import ThreadsSearchError, fetch_threads_posts, fetch_apify_posts
from ai_engine import call_gemini_api
from ai_engine import analyze_post_intent
from facebook_runner import (
    FacebookRunnerError,
    cancel_runner_job,
    get_runner_job,
    get_runner_logs,
    get_runner_posts,
    runner_health,
    submit_facebook_job,
)
from facebook_cookie_store import (
    CONTAINER_COOKIE_PATH,
    cookie_file_available,
    save_cookie_upload,
    runner_cookie_path,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bg_task = asyncio.create_task(background_scheduler_loop())
    facebook_bg_task = asyncio.create_task(background_facebook_scheduler_loop())
    yield
    bg_task.cancel()
    facebook_bg_task.cancel()

app = FastAPI(title="私有需求雷達 (Lead Radar)", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
FACEBOOK_ANALYSIS_JOBS = set()
RUNNER_HEALTH_CACHE = {"available": False, "checked_at": 0.0}
RUNNER_HEALTH_LOCK = asyncio.Lock()
FACEBOOK_MAX_POSTS = 500
FACEBOOK_DEEP_NO_NEW_POST_CYCLES = 10
FACEBOOK_MIN_JOB_INTERVAL_MINUTES = max(1, int(os.getenv("FACEBOOK_MIN_JOB_INTERVAL_MINUTES", "30")))
FACEBOOK_KEYWORD_TERMS = (
    "特定目的事業用地", "特定工廠登記", "農地工廠合法化", "工廠合法化", "丁種建築用地",
    "工業地", "廠房", "倉庫", "農地", "工廠", "合法化", "用地", "變更", "登記", "申請", "違章",
    "出租", "出售", "分租", "租", "買", "找", "推薦", "仲介", "桃園", "八德", "大溪", "龍潭", "平鎮", "鶯歌",
)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    keywords = get_all_keywords()
    leads = get_leads(limit=30)
    try:
        skipped_page_number = max(1, int(request.query_params.get("skipped_page", "1")))
    except ValueError:
        skipped_page_number = 1
    try:
        skipped_page_size = int(request.query_params.get("skipped_page_size", "10"))
    except ValueError:
        skipped_page_size = 10
    if skipped_page_size not in {10, 25, 50}:
        skipped_page_size = 10
    skipped_page = get_skipped_log_page(skipped_page_number, skipped_page_size)
    stats = get_summary_stats()
    settings = {
        "gemini_api_key": get_setting("gemini_api_key"),
        "threads_access_token": get_setting("threads_access_token"),
        "threads_source": get_setting("threads_source", "official"),
        "apify_api_token": get_setting("apify_api_token"),
        "apify_actor_id": get_setting("apify_actor_id", "logiover~threads-scraper"),
        "line_bot_token": get_setting("line_bot_token"),
        "line_user_id": get_setting("line_user_id"),
        "confidence_threshold": get_setting("confidence_threshold", "0.75"),
        "collection_days": get_setting("collection_days", "7"),
        "scan_enabled": get_setting("scan_enabled", "1"),
        "facebook_scan_enabled": get_setting("facebook_scan_enabled", "1"),
        "facebook_auto_scan_enabled": get_setting("facebook_auto_scan_enabled", "0"),
        "facebook_scan_interval_minutes": get_setting("facebook_scan_interval_minutes", "60"),
        "facebook_monitor_enabled": get_setting("facebook_monitor_enabled", "0"),
        "facebook_monitor_interval_minutes": get_setting("facebook_monitor_interval_minutes", "60"),
        "facebook_retention_days": get_setting("facebook_retention_days", "90"),
        "scan_interval_minutes": get_setting("scan_interval_minutes", "10"),
        "enable_hours_limit": get_setting("enable_hours_limit", "0"),
        "active_start_hour": get_setting("active_start_hour", "8"),
        "active_end_hour": get_setting("active_end_hour", "22"),
        "daily_summary_time": get_setting("daily_summary_time", "03:00"),
        "last_global_scan_time": get_setting("last_global_scan_time", "尚未執行"),
        "next_scan_time": get_next_scan_time_str()
    }
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "keywords": keywords,
            "leads": leads,
            "skipped": skipped_page["items"],
            "skipped_pagination": skipped_page,
            "stats": stats,
            "settings": settings,
        },
    )


def _activity_job(job: dict) -> dict:
    return {
        "id": job.get("id"),
        "source_name": job.get("source_name") or f"來源 #{job.get('source_id', '')}",
        "mode": job.get("scan_mode") or "normal",
        "status": job.get("status") or "queued",
        "created_at": job.get("created_at") or "",
        "started_at": job.get("started_at") or "",
        "finished_at": job.get("finished_at") or "",
    }


async def _cached_runner_available() -> bool:
    now = time.monotonic()
    if now - RUNNER_HEALTH_CACHE["checked_at"] < 15:
        return bool(RUNNER_HEALTH_CACHE["available"])
    async with RUNNER_HEALTH_LOCK:
        now = time.monotonic()
        if now - RUNNER_HEALTH_CACHE["checked_at"] >= 15:
            RUNNER_HEALTH_CACHE["available"] = await asyncio.to_thread(lambda: runner_health() is not None)
            RUNNER_HEALTH_CACHE["checked_at"] = now
    return bool(RUNNER_HEALTH_CACHE["available"])


@app.get("/api/activity")
async def api_activity():
    threads = get_scan_runtime_status()
    threads_enabled = get_setting("scan_enabled", "1") == "1"
    try:
        within_active_hours = is_within_active_hours()
    except (TypeError, ValueError):
        within_active_hours = True

    if not threads_enabled:
        threads.update(status="disabled", phase="disabled", running=False, message="Threads 掃描系統已關閉")
    elif not threads.get("running") and not within_active_hours:
        threads.update(status="paused", phase="paused", message="目前不在設定的掃描時段")
    elif threads.get("status") == "starting":
        threads.update(status="waiting", message="等待下一次排程")
    threads.update(
        enabled=threads_enabled,
        source=get_setting("threads_source", "official").strip().lower(),
        last_scan_time=get_setting("last_global_scan_time", "尚未執行"),
        next_scan_time=get_next_scan_time_str(),
        within_active_hours=within_active_hours,
    )

    jobs = get_facebook_jobs(limit=8)
    sources = get_facebook_sources()
    source_names = {source["id"]: source.get("name") or f"來源 #{source['id']}" for source in sources}
    active_jobs = []
    for active_job in get_active_facebook_jobs():
        active_job = dict(active_job)
        active_job["source_name"] = source_names.get(active_job.get("source_id"), f"來源 #{active_job.get('source_id', '')}")
        active_jobs.append(active_job)
    active_payload = [_activity_job(job) for job in active_jobs]
    facebook_enabled = get_setting("facebook_scan_enabled", "1") == "1"
    facebook_auto_enabled = get_setting("facebook_auto_scan_enabled", "0") == "1"
    monitor_enabled = get_setting("facebook_monitor_enabled", "0") == "1"
    monitored_sources = sum(
        1 for source in sources if source.get("is_active") and source.get("monitor_enabled")
    )
    analysis_jobs = len(FACEBOOK_ANALYSIS_JOBS)
    post_summary = get_facebook_post_summary(days=0)

    running_jobs = [job for job in active_jobs if job.get("status") == "running"]
    queued_jobs = [job for job in active_jobs if job.get("status") == "queued"]
    if not facebook_enabled:
        facebook_status = "disabled"
        facebook_message = "Facebook 掃描系統已關閉"
    elif running_jobs:
        current = running_jobs[0]
        mode_labels = {"normal": "一般掃描", "deep": "深度搜尋", "monitor": "登入監控"}
        facebook_status = "running"
        facebook_message = f"#{current['id']} {mode_labels.get(current.get('scan_mode'), '抓取')}執行中"
    elif queued_jobs:
        facebook_status = "waiting"
        facebook_message = f"#{queued_jobs[0]['id']} 等待 Runner 接手"
    elif analysis_jobs:
        facebook_status = "running"
        facebook_message = f"正在分析 {analysis_jobs} 個已完成任務"
    elif facebook_auto_enabled:
        facebook_status = "waiting"
        facebook_message = "等待下一次自動排程"
    else:
        facebook_status = "idle"
        facebook_message = "手動模式，沒有執行中的任務"

    deep_scan_active = any(job.get("scan_mode") == "deep" for job in active_jobs)
    running_monitor_jobs = [job for job in running_jobs if job.get("scan_mode") == "monitor"]
    queued_monitor_jobs = [job for job in queued_jobs if job.get("scan_mode") == "monitor"]
    if not facebook_enabled or not monitor_enabled:
        monitor_status = "disabled"
        monitor_message = "登入監控目前關閉"
    elif deep_scan_active:
        monitor_status = "paused"
        monitor_message = "深度搜尋中，登入監控暫停"
    elif running_monitor_jobs:
        monitor_status = "running"
        monitor_message = f"{len(running_monitor_jobs)} 個監控任務執行中"
    elif queued_monitor_jobs:
        monitor_status = "waiting"
        monitor_message = f"{len(queued_monitor_jobs)} 個監控任務等待中"
    elif monitored_sources:
        monitor_status = "waiting"
        monitor_message = f"監控 {monitored_sources} 個來源，等待排程"
    else:
        monitor_status = "paused"
        monitor_message = "尚未對任何來源開啟監控"

    runner_available = await _cached_runner_available()
    return {
        "status": "ok",
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "runner": {"available": runner_available},
        "threads": threads,
        "facebook": {
            "enabled": facebook_enabled,
            "auto_enabled": facebook_auto_enabled,
            "status": facebook_status,
            "message": facebook_message,
            "interval_minutes": get_setting("facebook_scan_interval_minutes", "60"),
            "active_jobs": active_payload,
            "latest_job": _activity_job(jobs[0]) if jobs else None,
        },
        "monitor": {
            "enabled": monitor_enabled,
            "status": monitor_status,
            "message": monitor_message,
            "interval_minutes": get_setting("facebook_monitor_interval_minutes", "60"),
            "source_count": monitored_sources,
        },
        "analysis": {
            "running_jobs": analysis_jobs,
            "pending_posts": post_summary["pending"],
            "error_posts": post_summary["errors"],
        },
        "recent_jobs": [_activity_job(job) for job in jobs[:5]],
    }


def _normalize_facebook_group_url(group_url: str) -> str:
    parsed = urlparse(group_url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        raise ValueError("請填入 facebook.com/groups/... 的社團網址")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "groups":
        raise ValueError("請填入 facebook.com/groups/... 的社團網址")
    return f"https://www.facebook.com/groups/{parts[1]}/"


def _facebook_keyword_matches(keyword: str, content: str) -> bool:
    normalized_keyword = re.sub(r"\s+", "", keyword).casefold()
    normalized_content = re.sub(r"\s+", "", content).casefold()
    if not normalized_keyword or normalized_keyword in normalized_content:
        return bool(normalized_keyword)

    terms = [term.casefold() for term in FACEBOOK_KEYWORD_TERMS if term in normalized_keyword]
    # Flexible matching handles natural variants such as "廠房分租" vs "租廠房".
    return len(terms) >= 2 and all(term in normalized_content for term in terms)


def _analyze_facebook_job(job_id: int):
    """Match imported Facebook posts to active keywords and run the shared AI classifier."""
    try:
        posts = get_facebook_posts_for_job(job_id, pending_only=True)
        keywords = [item for item in get_all_keywords(active_only=True) if item.get("keyword", "").strip()]
        if not keywords:
            return

        try:
            confidence_threshold = float(get_setting("confidence_threshold", "0.75"))
        except ValueError:
            confidence_threshold = 0.75

        for post in posts:
            content = str(post.get("content") or "")
            matches = [item for item in keywords if _facebook_keyword_matches(item["keyword"], content)]
            if not matches:
                update_facebook_post_analysis(post["id"], "unmatched")
                continue

            # Prefer the most specific (longest) phrase when several keywords match.
            keyword_item = max(matches, key=lambda item: len(item["keyword"].strip()))
            keyword = keyword_item["keyword"].strip()
            post_key = f"facebook:{post['source_id']}:{post['post_id']}"
            if is_post_duplicate(post_key, post.get("post_url", ""), content):
                update_facebook_post_analysis(post["id"], "duplicate", keyword)
                continue

            try:
                ai_result = analyze_post_intent(content, keyword, keyword_item.get("business_description", ""))
                is_qualified_lead = (
                    ai_result.get("is_lead", False)
                    and not ai_result.get("is_competitor", False)
                    and float(ai_result.get("confidence_score", 0)) >= confidence_threshold
                )
                if is_qualified_lead:
                    lead = {
                        "post_id": post_key,
                        "keyword": keyword,
                        "author": post.get("author", ""),
                        "content": content,
                        "post_url": post.get("post_url", ""),
                        "publish_time": post.get("publish_time", ""),
                        "ai_reason": ai_result.get("ai_reason", ""),
                        "confidence_score": ai_result.get("confidence_score", 1.0),
                        "suggested_reply": ai_result.get("suggested_reply", ""),
                        "intent_type": ai_result.get("intent_type", "其他"),
                        "location": ai_result.get("location", ""),
                        "urgency": ai_result.get("urgency", "low"),
                        "is_competitor": ai_result.get("is_competitor", False),
                        "contactability": ai_result.get("contactability", "low"),
                    }
                    if save_lead(lead):
                        send_lead_notification(lead)
                        update_facebook_post_analysis(post["id"], "lead", keyword)
                    else:
                        update_facebook_post_analysis(post["id"], "duplicate", keyword)
                else:
                    skipped = {
                        "post_id": post_key,
                        "keyword": keyword,
                        "author": post.get("author", ""),
                        "content": content,
                        "post_url": post.get("post_url", ""),
                        "ai_reason": ai_result.get("ai_reason", "AI 判定非實質需求"),
                    }
                    save_skipped(skipped)
                    update_facebook_post_analysis(post["id"], "skipped", keyword)
            except Exception as exc:
                update_facebook_post_analysis(post["id"], "error", keyword, str(exc))
    finally:
        FACEBOOK_ANALYSIS_JOBS.discard(job_id)


def _queue_facebook_analysis(job_id: int):
    if job_id in FACEBOOK_ANALYSIS_JOBS:
        return
    FACEBOOK_ANALYSIS_JOBS.add(job_id)
    asyncio.create_task(asyncio.to_thread(_analyze_facebook_job, job_id))


def _queue_completed_facebook_analysis():
    for job in get_facebook_jobs(limit=100):
        if job.get("status") == "completed":
            _queue_facebook_analysis(job["id"])


def _sync_facebook_job(job: dict) -> dict:
    """Refresh local job state and persist completed runner output."""
    if not job.get("runner_job_id"):
        return job
    try:
        remote = get_runner_job(job["runner_job_id"])
    except FacebookRunnerError as exc:
        job["runner_error"] = str(exc)
        return job

    status = remote.get("status", job.get("status", "queued"))
    update_facebook_job(
        job["id"],
        status=status,
        started_at=remote.get("startedAt"),
        finished_at=remote.get("finishedAt"),
        exit_code=remote.get("exitCode"),
        error=remote.get("error") or "",
        output_dir=remote.get("outputDir") or "",
    )
    if status == "completed":
        try:
            payload = get_runner_posts(job["runner_job_id"])
            save_facebook_posts(job["id"], job["source_id"], payload.get("posts", []))
            _queue_facebook_analysis(job["id"])
        except FacebookRunnerError as exc:
            job["runner_error"] = str(exc)
    job.update(
        status=status,
        started_at=remote.get("startedAt"),
        finished_at=remote.get("finishedAt"),
        exit_code=remote.get("exitCode"),
        error=remote.get("error") or "",
        output_dir=remote.get("outputDir") or "",
    )
    return job


@app.get("/facebook", response_class=HTMLResponse)
async def facebook_page(request: Request):
    jobs = [_sync_facebook_job(job) for job in get_facebook_jobs(limit=30)]
    _queue_completed_facebook_analysis()
    query_params = request.query_params
    try:
        page = max(1, int(query_params.get("page", "1")))
    except ValueError:
        page = 1
    try:
        page_size = min(100, max(25, int(query_params.get("page_size", "50"))))
    except ValueError:
        page_size = 50
    try:
        source_id = int(query_params["source_id"]) if query_params.get("source_id") else None
    except ValueError:
        source_id = None
    try:
        days = min(365, max(0, int(query_params.get("days", "7"))))
    except ValueError:
        days = 7
    status = query_params.get("status", "all")
    if status not in {"all", "lead", "pending", "skipped", "unmatched", "duplicate", "error"}:
        status = "all"
    search = query_params.get("q", "")[:100]
    post_page = get_facebook_post_page(page, page_size, source_id, status, search, days)
    return templates.TemplateResponse(
        request=request,
        name="facebook.html",
        context={
            "sources": get_facebook_sources(),
            "jobs": jobs,
            "posts": post_page["items"],
            "post_pagination": post_page,
            "post_summary": get_facebook_post_summary(source_id, search, days),
            "post_filters": {"source_id": source_id, "status": status, "q": search, "days": days, "page_size": page_size},
            "facebook_scan_enabled": get_setting("facebook_scan_enabled", "1") == "1",
            "facebook_auto_scan_enabled": get_setting("facebook_auto_scan_enabled", "0") == "1",
            "facebook_scan_interval_minutes": get_setting("facebook_scan_interval_minutes", "60"),
            "facebook_monitor_enabled": get_setting("facebook_monitor_enabled", "0") == "1",
            "facebook_monitor_interval_minutes": get_setting("facebook_monitor_interval_minutes", "60"),
            "deep_scan_active": bool(get_active_facebook_jobs("deep")),
            "runner_url": os.getenv("FACEBOOK_RUNNER_URL", "http://facebook-runner:9090"),
            "runner_available": runner_health() is not None,
            "facebook_cookie_available": cookie_file_available(),
            "facebook_cookie_path": CONTAINER_COOKIE_PATH,
        },
    )

@app.get("/api/facebook/posts/{post_id}")
async def api_get_facebook_post(post_id: int):
    post = get_facebook_post(post_id)
    if not post:
        return JSONResponse(status_code=404, content={"status": "error", "message": "貼文不存在"})
    return {"status": "ok", "post": post}


@app.post("/api/facebook/cookies")
async def api_upload_facebook_cookies(file: UploadFile = File(...)):
    filename = Path(file.filename or "").name.lower()
    if filename != "cookies.json":
        await file.close()
        return JSONResponse(status_code=400, content={"status": "error", "message": "請選擇檔名為 cookies.json 的檔案"})
    try:
        raw = await file.read(5 * 1024 * 1024 + 1)
        cookie_count = save_cookie_upload(raw)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})
    except OSError:
        return JSONResponse(status_code=500, content={"status": "error", "message": "Cookie 檔案無法寫入指定目錄"})
    finally:
        await file.close()
    return {
        "status": "ok",
        "message": "Cookie 已匯入",
        "path": CONTAINER_COOKIE_PATH,
        "cookie_count": cookie_count,
    }


@app.post("/api/facebook/sources")
async def api_create_facebook_source(
    name: str = Form(""),
    group_url: str = Form(...),
    max_posts: str = Form("100"),
    no_proxy: str = Form("1"),
    cookies_file: str = Form(""),
):
    try:
        normalized_url = _normalize_facebook_group_url(group_url)
        parsed_max_posts = min(FACEBOOK_MAX_POSTS, max(1, int(max_posts)))
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"status": "error", "message": "社團網址或貼文上限格式不正確"})
    source_id = create_facebook_source(name, normalized_url, parsed_max_posts, 1 if no_proxy == "1" else 0, cookies_file)
    if source_id is None:
        return JSONResponse(status_code=409, content={"status": "error", "message": "這個社團網址已經加入來源"})
    return {"status": "ok", "source_id": source_id}


@app.post("/api/facebook/sources/{source_id}/toggle")
async def api_toggle_facebook_source(source_id: int, is_active: int = Form(...)):
    if not get_facebook_source(source_id):
        return JSONResponse(status_code=404, content={"status": "error", "message": "來源不存在"})
    toggle_facebook_source(source_id, is_active)
    return {"status": "ok"}


@app.post("/api/facebook/sources/{source_id}/monitor")
async def api_toggle_facebook_source_monitor(source_id: int, enabled: int = Form(...)):
    if not get_facebook_source(source_id):
        return JSONResponse(status_code=404, content={"status": "error", "message": "來源不存在"})
    if get_setting("facebook_monitor_enabled", "0") != "1":
        return JSONResponse(status_code=403, content={"status": "error", "message": "請先在系統設定啟用 Facebook 模式 3「登入監控」"})
    toggle_facebook_source_monitor(source_id, enabled)
    return {"status": "ok"}


@app.post("/api/facebook/jobs")
async def api_create_facebook_job(source_id: int = Form(...), scan_mode: str = Form("normal")):
    if get_setting("facebook_scan_enabled", "1") != "1":
        return JSONResponse(status_code=403, content={"status": "error", "message": "Facebook 掃描系統目前已關閉，請先到系統設定開啟"})
    source = get_facebook_source(source_id)
    if not source:
        return JSONResponse(status_code=404, content={"status": "error", "message": "來源不存在"})
    if not source["is_active"]:
        return JSONResponse(status_code=400, content={"status": "error", "message": "請先啟用這個社團來源"})
    if scan_mode not in {"normal", "deep", "monitor"}:
        return JSONResponse(status_code=400, content={"status": "error", "message": "抓取模式不正確"})
    active_jobs = get_active_facebook_jobs()
    if scan_mode == "deep" and active_jobs:
        return JSONResponse(status_code=409, content={"status": "error", "message": "目前已有 Facebook 任務執行中，深度搜尋要等其他任務完成後才能啟動"})
    if scan_mode != "deep" and any(job.get("scan_mode") == "deep" for job in active_jobs):
        return JSONResponse(status_code=409, content={"status": "error", "message": "深度搜尋執行中，一般掃描與登入監控已暫停，請等待深度搜尋完成"})
    if scan_mode == "monitor":
        if get_setting("facebook_monitor_enabled", "0") != "1":
            return JSONResponse(status_code=403, content={"status": "error", "message": "請先在系統設定啟用 Facebook 模式 3「登入監控」"})
        if not source.get("monitor_enabled"):
            return JSONResponse(status_code=400, content={"status": "error", "message": "請先對這個社團開啟登入監控"})
    recent_jobs = [job for job in get_facebook_jobs(limit=100) if job.get("source_id") == source_id]
    if any(job.get("status") in {"queued", "running"} for job in recent_jobs):
        return JSONResponse(status_code=409, content={"status": "error", "message": "這個社團已有抓取任務執行中，請等待完成"})
    latest_completed = next((job for job in recent_jobs if job.get("status") == "completed"), None)
    if latest_completed:
        try:
            created_at = datetime.datetime.strptime(latest_completed["created_at"], "%Y-%m-%d %H:%M:%S")
            elapsed_minutes = (datetime.datetime.now() - created_at).total_seconds() / 60
            if elapsed_minutes < FACEBOOK_MIN_JOB_INTERVAL_MINUTES:
                wait_minutes = max(1, int(FACEBOOK_MIN_JOB_INTERVAL_MINUTES - elapsed_minutes))
                return JSONResponse(status_code=429, content={"status": "error", "message": f"為降低帳號風險，請約 {wait_minutes} 分鐘後再抓取"})
        except (KeyError, TypeError, ValueError):
            pass
    source = dict(source)
    source["max_posts"] = (
        FACEBOOK_MAX_POSTS if scan_mode == "deep"
        else min(50, int(source.get("max_posts") or 100)) if scan_mode == "monitor"
        else min(FACEBOOK_MAX_POSTS, int(source.get("max_posts") or 100))
    )
    job_id = create_facebook_job(source, scan_mode=scan_mode)
    try:
        # Anonymous mode is intentional: only the independent monitor runner may use login cookies.
        cookies_file = runner_cookie_path(source.get("cookies_file", "")) if scan_mode == "monitor" else ""
        remote = submit_facebook_job(
            source["group_url"],
            int(source["max_posts"]),
            bool(source["no_proxy"]),
            cookies_file,
            FACEBOOK_DEEP_NO_NEW_POST_CYCLES if scan_mode == "deep" else 3 if scan_mode == "monitor" else 4,
            monitor=scan_mode == "monitor",
            source_key=f"source-{source_id}" if scan_mode == "monitor" else "",
        )
        update_facebook_job(job_id, runner_job_id=remote.get("id"), status=remote.get("status", "running"), started_at=remote.get("startedAt"))
        return {"status": "ok", "job_id": job_id, "runner_job_id": remote.get("id")}
    except FacebookRunnerError as exc:
        update_facebook_job(job_id, status="failed", error=str(exc), finished_at=datetime.datetime.now().isoformat(timespec="seconds"))
        return JSONResponse(status_code=503, content={"status": "error", "message": str(exc), "job_id": job_id})


@app.get("/api/facebook/jobs/{job_id}")
async def api_get_facebook_job(job_id: int):
    job = get_facebook_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"status": "error", "message": "任務不存在"})
    return {"status": "ok", "job": _sync_facebook_job(job)}


@app.get("/api/facebook/jobs/{job_id}/logs")
async def api_get_facebook_job_logs(job_id: int):
    job = get_facebook_job(job_id)
    if not job or not job.get("runner_job_id"):
        return JSONResponse(status_code=404, content={"status": "error", "message": "任務尚未連接 runner"})
    try:
        return {"status": "ok", **get_runner_logs(job["runner_job_id"])}
    except FacebookRunnerError as exc:
        return JSONResponse(status_code=503, content={"status": "error", "message": str(exc)})


@app.post("/api/facebook/jobs/{job_id}/cancel")
async def api_cancel_facebook_job(job_id: int):
    job = get_facebook_job(job_id)
    if not job or not job.get("runner_job_id"):
        return JSONResponse(status_code=404, content={"status": "error", "message": "任務不存在或尚未啟動"})
    try:
        remote = cancel_runner_job(job["runner_job_id"])
        update_facebook_job(job_id, status="cancelled", finished_at=remote.get("finishedAt"), error=remote.get("error") or "")
        return {"status": "ok"}
    except FacebookRunnerError as exc:
        return JSONResponse(status_code=503, content={"status": "error", "message": str(exc)})

# API: 觸發手動強制掃描
@app.post("/api/scan")
async def trigger_scan(background_tasks: BackgroundTasks):
    if get_setting("scan_enabled", "1") != "1":
        return JSONResponse(status_code=400, content={"status": "error", "message": "掃描系統目前已關閉，請先在後台重新開啟"})
    if not request_scan_start():
        return JSONResponse(status_code=409, content={"status": "error", "message": "Threads 掃描正在執行，請查看背景作業進度"})
    background_tasks.add_task(run_scan_cycle, force=True)
    return {"status": "ok", "message": "已在背景啟動即時掃描"}

# API: 關鍵字管理
@app.post("/api/keywords/add")
async def api_add_keyword(keyword: str = Form(...), business_description: str = Form(...)):
    success = add_keyword(keyword, business_description)
    if success:
        _queue_completed_facebook_analysis()
        return {"status": "ok"}
    return JSONResponse(status_code=400, content={"status": "error", "message": "關鍵字已存在或無效"})

@app.post("/api/keywords/{kw_id}/toggle")
async def api_toggle_keyword(kw_id: int, is_active: int = Form(...)):
    toggle_keyword(kw_id, is_active)
    return {"status": "ok"}

@app.post("/api/keywords/{kw_id}/update")
async def api_update_keyword(
    kw_id: int,
    keyword: str = Form(...),
    business_description: str = Form(...),
):
    if not keyword.strip():
        return JSONResponse(status_code=400, content={"status": "error", "message": "關鍵字不可為空白"})
    if not update_keyword(kw_id, keyword, business_description):
        return JSONResponse(status_code=409, content={"status": "error", "message": "關鍵字已存在，請換一個名稱"})
    _queue_completed_facebook_analysis()
    return {"status": "ok"}

@app.post("/api/keywords/{kw_id}/delete")
async def api_delete_keyword(kw_id: int):
    delete_keyword(kw_id)
    return {"status": "ok"}

# API: 商機狀態變更
@app.post("/api/leads/{lead_id}/status")
async def api_update_lead_status(lead_id: int, status: str = Form(...)):
    update_lead_status(lead_id, status)
    return {"status": "ok"}

# API: 儲存系統與掃描時間設定
@app.post("/api/settings/save")
async def api_save_settings(
    gemini_api_key: str = Form(""),
    threads_access_token: str = Form(""),
    threads_source: str = Form("official"),
    apify_api_token: str = Form(""),
    apify_actor_id: str = Form("logiover~threads-scraper"),
    line_bot_token: str = Form(""),
    line_user_id: str = Form(""),
    confidence_threshold: str = Form("0.75"),
    collection_days: str = Form("7"),
    scan_enabled: str = Form("1"),
    facebook_scan_enabled: str = Form("1"),
    facebook_auto_scan_enabled: str = Form("0"),
    facebook_scan_interval_minutes: str = Form("60"),
    facebook_monitor_enabled: str = Form("0"),
    facebook_monitor_interval_minutes: str = Form("60"),
    facebook_retention_days: str = Form("90"),
    scan_interval_minutes: str = Form("10"),
    enable_hours_limit: str = Form("0"),
    active_start_hour: str = Form("8"),
    active_end_hour: str = Form("22"),
    daily_summary_time: str = Form("03:00")
):
    set_setting("gemini_api_key", gemini_api_key.strip())
    set_setting("threads_access_token", threads_access_token.strip())
    set_setting("threads_source", threads_source.strip().lower() if threads_source.strip().lower() in ("official", "apify") else "official")
    set_setting("apify_api_token", apify_api_token.strip())
    set_setting("apify_actor_id", apify_actor_id.strip() or "logiover~threads-scraper")
    set_setting("line_bot_token", line_bot_token.strip())
    set_setting("line_user_id", line_user_id.strip())
    try:
        threshold = min(1.0, max(0.0, float(confidence_threshold)))
    except ValueError:
        threshold = 0.75
    set_setting("confidence_threshold", str(threshold))
    try:
        days = min(90, max(1, int(collection_days)))
    except ValueError:
        days = 7
    set_setting("collection_days", str(days))
    set_setting("scan_enabled", "1" if scan_enabled == "1" else "0")
    set_setting("facebook_scan_enabled", "1" if facebook_scan_enabled == "1" else "0")
    set_setting("facebook_auto_scan_enabled", "1" if facebook_auto_scan_enabled == "1" else "0")
    set_setting("facebook_monitor_enabled", "1" if facebook_monitor_enabled == "1" else "0")
    try:
        facebook_interval = max(30, int(facebook_scan_interval_minutes))
    except ValueError:
        facebook_interval = 60
    set_setting("facebook_scan_interval_minutes", str(facebook_interval))
    try:
        facebook_monitor_interval = max(30, int(facebook_monitor_interval_minutes))
    except ValueError:
        facebook_monitor_interval = 60
    set_setting("facebook_monitor_interval_minutes", str(facebook_monitor_interval))
    try:
        facebook_retention = min(365, max(30, int(facebook_retention_days)))
    except ValueError:
        facebook_retention = 90
    set_setting("facebook_retention_days", str(facebook_retention))
    set_setting("scan_interval_minutes", scan_interval_minutes.strip())
    set_setting("enable_hours_limit", enable_hours_limit.strip())
    set_setting("active_start_hour", active_start_hour.strip())
    set_setting("active_end_hour", active_end_hour.strip())
    set_setting("daily_summary_time", daily_summary_time.strip())
    return {"status": "ok", "message": "系統與掃描排程設定已成功儲存並即刻生效！"}

# API: 測試 LINE 推播
@app.post("/api/settings/test-notify")
async def api_test_notify():
    mock_lead = {
        "keyword": "特定工廠",
        "author": "@test_client",
        "content": "【測試訊息】請問大溪有推薦做農地違章工廠合法化或特定目的事業用地的專業地產顧問嗎？",
        "post_url": "https://lead-radar.homo.tw/",
        "ai_reason": "這是一則系統測試推播訊息，代表 LINE 串接成功！",
        "suggested_reply": "測試成功：系統已可即時發送通知至您的 LINE。"
    }
    if send_lead_notification(mock_lead):
        return {"status": "ok", "message": "測試推播已發出，請檢查您的 LINE"}
    return JSONResponse(status_code=400, content={"status": "error", "message": "LINE 推播失敗，請檢查 Token、User ID 與容器日誌"})


@app.post("/api/settings/test-threads")
async def api_test_threads():
    source = get_setting("threads_source", "official").strip().lower()
    try:
        if source == "apify":
            posts = await asyncio.to_thread(fetch_apify_posts, "台灣")
            return {"status": "ok", "message": f"Apify Threads 連線成功，測試取得 {len(posts)} 篇公開貼文"}
        posts = await asyncio.to_thread(fetch_threads_posts, "台灣")
        return {"status": "ok", "message": f"Meta Threads API 連線成功，測試取得 {len(posts)} 篇公開貼文"}
    except ThreadsSearchError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})

@app.post("/api/settings/test-apify")
async def api_test_apify():
    try:
        posts = await asyncio.to_thread(fetch_apify_posts, "桃園廠房")
        return {"status": "ok", "message": f"Apify 連線成功，測試取得 {len(posts)} 篇近期貼文"}
    except ThreadsSearchError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})


@app.post("/api/settings/test-gemini")
async def api_test_gemini():
    api_key = get_setting("gemini_api_key")
    if not api_key:
        return JSONResponse(status_code=400, content={"status": "error", "message": "尚未設定 Gemini API Key"})
    try:
        result = call_gemini_api(
            api_key,
            "請問有推薦的桃園工廠合法化顧問嗎？",
            "工廠合法化",
            "桃園、新北工廠合法化與用地變更服務",
        )
        return {"status": "ok", "message": f"Gemini 連線與 JSON 分析成功（信心 {result['confidence_score']:.0%}）"}
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Gemini 測試失敗，請檢查 API Key、模型額度與容器日誌"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
