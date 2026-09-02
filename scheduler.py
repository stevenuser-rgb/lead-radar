import asyncio
import time
import datetime
from database import (
    get_all_keywords,
    is_post_duplicate,
    get_all_processed_ids_set,
    compute_content_hash,
    save_lead,
    save_skipped,
    update_keyword_metrics,
    clean_expired_records,
    get_setting,
    set_setting
)
from scraper import ThreadsSearchError, fetch_threads_posts, fetch_apify_posts
from ai_engine import analyze_post_intent
from notifier import send_lead_notification

IS_SCANNING = False
SCAN_REQUESTED = False
NEXT_SCAN_TIMESTAMP = 0
SCAN_RUNTIME = {
    "status": "starting",
    "phase": "starting",
    "running": False,
    "trigger": "scheduled",
    "started_at": "",
    "completed_at": "",
    "current_keyword": "",
    "current_index": 0,
    "total_keywords": 0,
    "scanned_posts": 0,
    "hit_count": 0,
    "skipped_count": 0,
    "error_count": 0,
    "message": "排程啟動中",
    "updated_at": "",
}


def _runtime_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _update_scan_runtime(**changes):
    changes["updated_at"] = _runtime_timestamp()
    SCAN_RUNTIME.update(changes)


def get_scan_runtime_status() -> dict:
    return dict(SCAN_RUNTIME)


def request_scan_start() -> bool:
    """Reserve one manual scan before FastAPI starts its background task."""
    global SCAN_REQUESTED
    if IS_SCANNING or SCAN_REQUESTED:
        return False
    SCAN_REQUESTED = True
    _update_scan_runtime(
        status="running",
        phase="queued",
        running=True,
        trigger="manual",
        message="手動掃描已排入，正在準備執行",
    )
    return True

def get_next_scan_time_str() -> str:
    """取得下次預計掃描時間字串"""
    global NEXT_SCAN_TIMESTAMP
    if NEXT_SCAN_TIMESTAMP <= 0:
        return "計算中..."
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NEXT_SCAN_TIMESTAMP))

def is_within_active_hours() -> bool:
    """檢查目前時間是否在使用者設定的允許掃描時段內"""
    enable_hours_limit = get_setting("enable_hours_limit", "0")
    if enable_hours_limit != "1":
        return True # 全天 24 小時運作
        
    start_hour = int(get_setting("active_start_hour", "8"))
    end_hour = int(get_setting("active_end_hour", "22"))
    
    current_hour = datetime.datetime.now().hour
    if start_hour <= end_hour:
        return start_hour <= current_hour < end_hour
    else:
        # 跨午夜時段 (例如 20:00 到 06:00)
        return current_hour >= start_hour or current_hour < end_hour

async def run_scan_cycle(force: bool = False):
    """
    執行單次全關鍵字掃描作業（具備嚴密的三層防重複機制）
    """
    global IS_SCANNING, SCAN_REQUESTED
    if get_setting("scan_enabled", "1") != "1":
        if force:
            SCAN_REQUESTED = False
        _update_scan_runtime(status="disabled", phase="disabled", running=False, message="Threads 掃描系統已關閉")
        print("[Scheduler] 掃描系統目前已關閉，本次略過。")
        return
    if IS_SCANNING:
        if force:
            SCAN_REQUESTED = False
        print("[Scheduler] 上一輪掃描仍在執行中，本次略過。")
        return
    if SCAN_REQUESTED and not force:
        print("[Scheduler] 手動掃描已排入，本次自動排程略過。")
        return
        
    if not force and not is_within_active_hours():
        _update_scan_runtime(status="paused", phase="paused", running=False, message="目前不在設定的掃描時段")
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⏸️ 目前非設定之掃描營運時段，暫停自動掃描。")
        return

    SCAN_REQUESTED = False
    IS_SCANNING = True
    _update_scan_runtime(
        status="running",
        phase="preparing",
        running=True,
        trigger="manual" if force else "scheduled",
        started_at=_runtime_timestamp(),
        current_keyword="",
        current_index=0,
        total_keywords=0,
        scanned_posts=0,
        hit_count=0,
        skipped_count=0,
        error_count=0,
        message="正在準備掃描關鍵字",
    )
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 🚀 開始執行需求雷達掃描作業...")

    cycle_error = ""
    try:
        # 1. 載入記憶體層快取（第一層快篩）
        processed_cache = get_all_processed_ids_set()
        current_run_seen_posts = set() # 當次循環跨關鍵字去重集合
        
        active_keywords = get_all_keywords(active_only=True)
        _update_scan_runtime(total_keywords=len(active_keywords))
        for keyword_index, item in enumerate(active_keywords, start=1):
            kw_id = item["id"]
            keyword = item["keyword"]
            desc = item["business_description"] or ""

            _update_scan_runtime(
                phase="searching",
                current_keyword=keyword,
                current_index=keyword_index,
                message=f"正在掃描：{keyword}",
            )
            print(f"--> 正在掃描關鍵字: 【{keyword}】")
            source = get_setting("threads_source", "official").strip().lower()
            try:
                fetcher = fetch_apify_posts if source == "apify" else fetch_threads_posts
                raw_posts = await asyncio.to_thread(fetcher, keyword)
            except Exception as exc:
                error_message = str(exc)
                update_keyword_metrics(kw_id, 0, 0, 0, status="error", error=error_message)
                _update_scan_runtime(
                    phase="searching",
                    error_count=SCAN_RUNTIME["error_count"] + 1,
                    message=f"{keyword} 掃描失敗，繼續下一組",
                )
                print(f"    ❌ [Threads Search] {error_message}")
                if source == "apify" and isinstance(exc, ThreadsSearchError):
                    cycle_error = f"Apify 來源暫時無法使用，已停止本輪掃描：{error_message}"
                    _update_scan_runtime(phase="error", message=cycle_error)
                    break
                continue
            
            scanned_count = len(raw_posts)
            hit_count = 0
            skipped_count = 0
            _update_scan_runtime(
                phase="analyzing",
                message=f"正在比對與分析：{keyword}（取得 {scanned_count} 篇）",
            )
            
            for post in raw_posts:
                post_id = post.get("post_id", "")
                post_url = post.get("post_url", "")
                content = post.get("content", "")
                content_hash = compute_content_hash(content)
                
                # 【去重第一關】：記憶體快取快篩 (O(1))
                if (post_id in processed_cache or 
                    post_url in processed_cache or 
                    content_hash in processed_cache or 
                    post_id in current_run_seen_posts):
                    # 已處理過或在當次循環已遇過，直接略過，不重複消耗 AI 運算
                    continue
                
                # 【去重第二關】：資料庫精確比對
                if is_post_duplicate(post_id, post_url, content):
                    processed_cache.add(post_id)
                    processed_cache.add(post_url)
                    processed_cache.add(content_hash)
                    continue
                
                # 標記當次已見
                current_run_seen_posts.add(post_id)
                current_run_seen_posts.add(post_url)
                current_run_seen_posts.add(content_hash)
                
                # 呼叫 AI 意圖分析
                ai_result = await asyncio.to_thread(analyze_post_intent, content, keyword, desc)
                
                try:
                    confidence_threshold = float(get_setting("confidence_threshold", "0.75"))
                except ValueError:
                    confidence_threshold = 0.75
                is_qualified_lead = (
                    ai_result.get("is_lead", False)
                    and not ai_result.get("is_competitor", False)
                    and float(ai_result.get("confidence_score", 0)) >= confidence_threshold
                )

                if is_qualified_lead:
                    lead_dict = {
                        "post_id": post_id,
                        "keyword": keyword,
                        "author": post.get("author", ""),
                        "content": content,
                        "post_url": post_url,
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
                    
                    # 【去重第三關】：資料庫級 INSERT 唯一性約束
                    saved = save_lead(lead_dict)
                    if saved:
                        # 僅在資料庫真正寫入成功時，才觸發 LINE 即時推播，杜絕重複通知
                        await asyncio.to_thread(send_lead_notification, lead_dict)
                        hit_count += 1
                        print(f"    🎯 [命中新商機] {post.get('author')}: {post_url}")
                    else:
                        print(f"    ⚠️ [重複商機已攔截] 避免重複儲存: {post_id}")
                else:
                    skipped_dict = {
                        "post_id": post_id,
                        "keyword": keyword,
                        "author": post.get("author", ""),
                        "content": content,
                        "post_url": post_url,
                        "ai_reason": ai_result.get("ai_reason", "AI 判定非實質需求")
                    }
                    saved = save_skipped(skipped_dict)
                    if saved:
                        skipped_count += 1
                        print(f"    🚫 [略過雜訊] 原因: {ai_result.get('ai_reason')}")
                
                # 寫入快取集合
                processed_cache.add(post_id)
                processed_cache.add(post_url)
                processed_cache.add(content_hash)
                    
            # 更新資料庫關鍵字指標數據
            update_keyword_metrics(kw_id, scanned_count, hit_count, skipped_count, status="ok")
            _update_scan_runtime(
                phase="searching",
                scanned_posts=SCAN_RUNTIME["scanned_posts"] + scanned_count,
                hit_count=SCAN_RUNTIME["hit_count"] + hit_count,
                skipped_count=SCAN_RUNTIME["skipped_count"] + skipped_count,
                message=f"{keyword} 完成，準備下一組",
            )
            await asyncio.sleep(1)
            
        # 自動清理超過 30 天的舊紀錄
        clean_expired_records(days=30)
            
    except Exception as e:
        cycle_error = str(e)
        print(f"[Scheduler Error] 掃描發生異常: {e}")
    finally:
        IS_SCANNING = False
        completed_at = _runtime_timestamp()
        set_setting("last_global_scan_time", completed_at)
        if cycle_error:
            status = "error"
            message = f"掃描中斷：{cycle_error[:120]}"
        elif SCAN_RUNTIME["error_count"]:
            status = "warning"
            message = f"掃描完成，{SCAN_RUNTIME['error_count']} 組關鍵字失敗"
        else:
            status = "waiting"
            message = (
                f"掃描完成：{SCAN_RUNTIME['scanned_posts']} 篇、"
                f"命中 {SCAN_RUNTIME['hit_count']} 篇"
            )
        _update_scan_runtime(
            status=status,
            phase="completed" if status in {"waiting", "warning"} else "error",
            running=False,
            completed_at=completed_at,
            current_keyword="",
            message=message,
        )
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✅ 需求雷達掃描作業完成。\n")


async def background_scheduler_loop():
    """
    背景定時排程迴圈
    """
    global NEXT_SCAN_TIMESTAMP
    while True:
        try:
            interval_minutes = int(get_setting("scan_interval_minutes", "10"))
        except Exception:
            interval_minutes = 10
            
        interval_seconds = interval_minutes * 60
        NEXT_SCAN_TIMESTAMP = time.time() + interval_seconds
        
        await run_scan_cycle()
        
        # 進入等待倒數
        while time.time() < NEXT_SCAN_TIMESTAMP:
            try:
                current_cfg = int(get_setting("scan_interval_minutes", "10")) * 60
                if current_cfg != interval_seconds:
                    interval_seconds = current_cfg
                    NEXT_SCAN_TIMESTAMP = time.time() + interval_seconds
            except Exception:
                pass
            await asyncio.sleep(2)
