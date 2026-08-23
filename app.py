import asyncio
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
    set_setting
)
from scheduler import background_scheduler_loop, run_scan_cycle, get_next_scan_time_str
from notifier import send_lead_notification
from scraper import ThreadsSearchError, fetch_threads_posts, fetch_apify_posts
from ai_engine import call_gemini_api

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bg_task = asyncio.create_task(background_scheduler_loop())
    yield
    bg_task.cancel()

app = FastAPI(title="私有需求雷達 (Lead Radar)", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

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
