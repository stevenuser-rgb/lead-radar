import asyncio
import os
import datetime
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, BackgroundTasks
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
    get_skipped_logs,
    get_summary_stats,
    get_setting,
    set_setting,
    get_facebook_sources,
    create_facebook_source,
    toggle_facebook_source,
    get_facebook_source,
    create_facebook_job,
    get_facebook_job,
    get_facebook_jobs,
    update_facebook_job,
    save_facebook_posts,
    get_facebook_posts,
    get_facebook_posts_for_job,
    update_facebook_post_analysis,
    update_keyword,
    is_post_duplicate,
    save_lead,
    save_skipped,
)
from scheduler import background_scheduler_loop, run_scan_cycle, get_next_scan_time_str
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bg_task = asyncio.create_task(background_scheduler_loop())
    yield
    bg_task.cancel()

app = FastAPI(title="私有需求雷達 (Lead Radar)", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
FACEBOOK_ANALYSIS_JOBS = set()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    keywords = get_all_keywords()
    leads = get_leads(limit=30)
    skipped = get_skipped_logs(limit=30)
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
            "skipped": skipped,
            "stats": stats,
            "settings": settings,
        },
    )


def _normalize_facebook_group_url(group_url: str) -> str:
    parsed = urlparse(group_url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        raise ValueError("請填入 facebook.com/groups/... 的社團網址")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "groups":
        raise ValueError("請填入 facebook.com/groups/... 的社團網址")
    return f"https://www.facebook.com/groups/{parts[1]}/"


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
            content_folded = content.casefold()
            matches = [item for item in keywords if item["keyword"].strip().casefold() in content_folded]
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
    return templates.TemplateResponse(
        request=request,
        name="facebook.html",
        context={
            "sources": get_facebook_sources(),
            "jobs": jobs,
            "posts": get_facebook_posts(limit=100),
            "runner_url": os.getenv("FACEBOOK_RUNNER_URL", "http://facebook-runner:9090"),
            "runner_available": runner_health() is not None,
        },
    )


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
        parsed_max_posts = min(5000, max(1, int(max_posts)))
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


@app.post("/api/facebook/jobs")
async def api_create_facebook_job(source_id: int = Form(...)):
    source = get_facebook_source(source_id)
    if not source:
        return JSONResponse(status_code=404, content={"status": "error", "message": "來源不存在"})
    if not source["is_active"]:
        return JSONResponse(status_code=400, content={"status": "error", "message": "請先啟用這個社團來源"})
    job_id = create_facebook_job(source)
    try:
        remote = submit_facebook_job(
            source["group_url"],
            int(source["max_posts"]),
            bool(source["no_proxy"]),
            source.get("cookies_file", ""),
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
    background_tasks.add_task(run_scan_cycle, force=True)
    return {"status": "ok", "message": "已在背景啟動即時掃描"}

# API: 關鍵字管理
@app.post("/api/keywords/add")
async def api_add_keyword(keyword: str = Form(...), business_description: str = Form(...)):
    success = add_keyword(keyword, business_description)
    if success:
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
    try:
        posts = fetch_threads_posts("台灣")
        return {"status": "ok", "message": f"Threads API 連線成功，測試取得 {len(posts)} 篇公開貼文"}
    except ThreadsSearchError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})

@app.post("/api/settings/test-apify")
async def api_test_apify():
    try:
        posts = fetch_apify_posts("桃園廠房")
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
