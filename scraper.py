import time
import datetime
import httpx
from typing import List, Dict, Any

from database import get_setting

THREADS_API_URL = "https://graph.threads.net/v1.0/keyword_search"
APIFY_RUN_URL = "https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"
# Keep this list to fields documented by Meta's keyword_search response.
# `is_reply` is not a supported search field and can trigger a vague HTTP 500.
THREADS_FIELDS = "id,text,permalink,timestamp,username,has_replies,is_quote_post"


class ThreadsSearchError(RuntimeError):
    """Threads 搜尋失敗，保留可呈現在儀表板的安全錯誤訊息。"""


def _request_page(client: httpx.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    last_error = "未知錯誤"
    for attempt in range(3):
        try:
            response = client.get(THREADS_API_URL, params=params)
            if response.status_code == 200:
                return response.json()
            try:
                detail = response.json().get("error", {}).get("message", response.text[:200])
            except Exception:
                detail = response.text[:200]
            last_error = f"Threads API HTTP {response.status_code}: {detail}"
            if response.status_code not in (429, 500, 502, 503, 504):
                break
        except httpx.HTTPError:
            last_error = "Threads API 連線失敗或逾時"
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise ThreadsSearchError(last_error)

def fetch_threads_posts(keyword: str) -> List[Dict[str, Any]]:
    """
    使用 Meta Threads Keyword Search API 取得最新公開貼文。
    """
    access_token = get_setting("threads_access_token")
    if not access_token:
        raise ThreadsSearchError("尚未設定 Threads Access Token")

    # Meta's keyword search expects the query without a leading hashtag.
    # Our UI stores hashtags for display, so normalize them at the API boundary.
    query = keyword.strip().lstrip("#").strip()
    params: Dict[str, Any] = {
        "q": query,
        "search_type": "RECENT",
        "search_mode": "KEYWORD",
        "fields": THREADS_FIELDS,
        "limit": 25,
        "access_token": access_token,
    }
    try:
        collection_days = min(90, max(1, int(get_setting("collection_days", "7"))))
    except (TypeError, ValueError):
        collection_days = 7
    now_ts = int(time.time())
    params["since"] = now_ts - collection_days * 86400
    params["until"] = now_ts
    posts: List[Dict[str, Any]] = []
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        for _ in range(2):
            payload = _request_page(client, params)
            for item in payload.get("data", []):
                text = (item.get("text") or "").strip()
                post_id = str(item.get("id") or "").strip()
                permalink = (item.get("permalink") or "").strip()
                if not post_id or not text or not permalink:
                    continue
                username = (item.get("username") or "").strip()
                posts.append({
                    "post_id": f"threads_{post_id}",
                    "author": f"@{username}" if username else "匿名用戶",
                    "content": text,
                    "post_url": permalink,
                    "publish_time": item.get("timestamp", ""),
                    "is_reply": bool(item.get("is_reply", False)),
                    "is_quote_post": bool(item.get("is_quote_post", False)),
                })
            after = payload.get("paging", {}).get("cursors", {}).get("after")
            if not after or len(posts) >= 50:
                break
            params["after"] = after
    return posts


def fetch_apify_posts(keyword: str) -> List[Dict[str, Any]]:
    """透過 Apify Threads Scraper 取得公開貼文，轉成雷達統一格式。"""
    token = get_setting("apify_api_token").strip()
    actor_id = get_setting("apify_actor_id", "logiover~threads-scraper").strip()
    if not token:
        raise ThreadsSearchError("尚未設定 Apify API Token")
    if not actor_id:
        raise ThreadsSearchError("尚未設定 Apify Actor ID")

    try:
        days = min(90, max(1, int(get_setting("collection_days", "7"))))
    except (TypeError, ValueError):
        days = 7
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    payload = {
        "searchTerms": [keyword.lstrip("#").strip()],
        "maxResults": 30,
        "includeReplies": False,
        "expandFromSearch": False,
    }
    url = APIFY_RUN_URL.format(actor_id=actor_id)
    try:
        with httpx.Client(timeout=180.0, follow_redirects=True) as client:
            response = client.post(
                url,
                params={"format": "json", "clean": "true"},
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            # Apify may return 200 or 201 when the synchronous dataset is ready.
            if response.status_code not in (200, 201):
                detail = response.text[:300]
                raise ThreadsSearchError(f"Apify API HTTP {response.status_code}: {detail}")
            items = response.json()
    except httpx.HTTPError as exc:
        raise ThreadsSearchError(f"Apify API 連線失敗或逾時: {exc}") from exc

    posts: List[Dict[str, Any]] = []
    for item in items if isinstance(items, list) else items.get("data", []):
        text = (item.get("text") or item.get("content") or "").strip()
        post_id = str(item.get("postId") or item.get("id") or "").strip()
        permalink = (item.get("url") or item.get("permalink") or "").strip()
        publish_time = item.get("createdAt") or item.get("timestamp") or ""
        if not post_id or not text or not permalink:
            continue
        try:
            published = datetime.datetime.fromisoformat(str(publish_time).replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=datetime.timezone.utc)
            if published < cutoff:
                continue
        except (TypeError, ValueError):
            pass
        author = item.get("author") or {}
        username = author.get("username") if isinstance(author, dict) else item.get("username", "")
        posts.append({
            "post_id": f"threads_{post_id}",
            "author": f"@{username}" if username else "匿名用戶",
            "content": text,
            "post_url": permalink,
            "publish_time": publish_time,
            "is_reply": bool(item.get("isReply", False)),
            "is_quote_post": bool(item.get("isQuotePost", False)),
        })
    return posts
