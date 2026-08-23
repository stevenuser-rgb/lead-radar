import os
import sqlite3
import datetime
import hashlib
from typing import List, Dict, Any, Optional

DB_PATH = os.getenv("DB_PATH", "/tmp/radar.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def compute_content_hash(text: str) -> str:
    """計算貼文純文字內容的 SHA256 哈希值，用於跨帳號/轉發內容去重"""
    cleaned = "".join(text.split()).lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. 關鍵字設定表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS keywords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT UNIQUE NOT NULL,
        business_description TEXT,
        is_active INTEGER DEFAULT 1,
        last_scan_time TEXT,
        last_scanned_count INTEGER DEFAULT 0,
        last_hit_count INTEGER DEFAULT 0,
        last_skipped_count INTEGER DEFAULT 0,
        last_scan_status TEXT DEFAULT 'pending',
        last_scan_error TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    """)
    cursor.execute("PRAGMA table_info(keywords)")
    keyword_columns = [row[1] for row in cursor.fetchall()]
    if "last_scan_status" not in keyword_columns:
        cursor.execute("ALTER TABLE keywords ADD COLUMN last_scan_status TEXT DEFAULT 'pending'")
    if "last_scan_error" not in keyword_columns:
        cursor.execute("ALTER TABLE keywords ADD COLUMN last_scan_error TEXT DEFAULT ''")
    
    # 2. 命中商機資料表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id TEXT UNIQUE NOT NULL,
        content_hash TEXT,
        keyword TEXT NOT NULL,
        author TEXT,
        content TEXT NOT NULL,
        post_url TEXT NOT NULL,
        publish_time TEXT,
        ai_reason TEXT,
        confidence_score REAL DEFAULT 1.0,
        suggested_reply TEXT,
        intent_type TEXT DEFAULT '',
        location TEXT DEFAULT '',
        urgency TEXT DEFAULT '',
        is_competitor INTEGER DEFAULT 0,
        contactability TEXT DEFAULT '',
        status TEXT DEFAULT 'pending', -- pending, replied, ignored
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    """)
    
    # 檢查並自動補充舊版欄位 (Migration)
    cursor.execute("PRAGMA table_info(leads)")
    columns = [row[1] for row in cursor.fetchall()]
    if "content_hash" not in columns:
        cursor.execute("ALTER TABLE leads ADD COLUMN content_hash TEXT")
    for column, definition in (
        ("intent_type", "TEXT DEFAULT ''"),
        ("location", "TEXT DEFAULT ''"),
        ("urgency", "TEXT DEFAULT ''"),
        ("is_competitor", "INTEGER DEFAULT 0"),
        ("contactability", "TEXT DEFAULT ''"),
    ):
        if column not in columns:
            cursor.execute(f"ALTER TABLE leads ADD COLUMN {column} {definition}")
        
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_hash ON leads(content_hash)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at)")
    
    # 3. 略過貼文與理由日誌表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS skipped_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id TEXT UNIQUE NOT NULL,
        content_hash TEXT,
        keyword TEXT NOT NULL,
        author TEXT,
        content TEXT NOT NULL,
        post_url TEXT NOT NULL,
        ai_reason TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    """)
    
    cursor.execute("PRAGMA table_info(skipped_logs)")
    s_columns = [row[1] for row in cursor.fetchall()]
    if "content_hash" not in s_columns:
        cursor.execute("ALTER TABLE skipped_logs ADD COLUMN content_hash TEXT")
        
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_skipped_hash ON skipped_logs(content_hash)")
    
    # 4. 系統與通知設定表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    # 5. Facebook 社團來源、抓取任務與原始貼文
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS facebook_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        group_url TEXT UNIQUE NOT NULL,
        is_active INTEGER DEFAULT 1,
        max_posts INTEGER DEFAULT 100,
        no_proxy INTEGER DEFAULT 1,
        cookies_file TEXT DEFAULT '',
        last_job_id INTEGER,
        last_job_status TEXT DEFAULT 'pending',
        last_job_error TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS facebook_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        runner_job_id TEXT,
        status TEXT DEFAULT 'queued',
        group_url TEXT NOT NULL,
        max_posts INTEGER DEFAULT 100,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        started_at TEXT,
        finished_at TEXT,
        exit_code INTEGER,
        error TEXT DEFAULT '',
        output_dir TEXT DEFAULT '',
        FOREIGN KEY(source_id) REFERENCES facebook_sources(id)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS facebook_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        job_id INTEGER NOT NULL,
        post_id TEXT NOT NULL,
        author TEXT DEFAULT '',
        content TEXT DEFAULT '',
        post_url TEXT DEFAULT '',
        publish_time TEXT DEFAULT '',
        analysis_status TEXT DEFAULT 'pending',
        matched_keyword TEXT DEFAULT '',
        analysis_error TEXT DEFAULT '',
        analyzed_at TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        UNIQUE(job_id, post_id),
        FOREIGN KEY(source_id) REFERENCES facebook_sources(id),
        FOREIGN KEY(job_id) REFERENCES facebook_jobs(id)
    )
    """)
    cursor.execute("PRAGMA table_info(facebook_posts)")
    facebook_post_columns = [row[1] for row in cursor.fetchall()]
    for column, definition in (
        ("analysis_status", "TEXT DEFAULT 'pending'"),
        ("matched_keyword", "TEXT DEFAULT ''"),
        ("analysis_error", "TEXT DEFAULT ''"),
        ("analyzed_at", "TEXT"),
    ):
        if column not in facebook_post_columns:
            cursor.execute(f"ALTER TABLE facebook_posts ADD COLUMN {column} {definition}")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_facebook_jobs_source ON facebook_jobs(source_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_facebook_posts_source ON facebook_posts(source_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_facebook_posts_analysis ON facebook_posts(analysis_status, id DESC)")
    
    # 一次性加入拆分後的高意圖、地區與問題型關鍵字，不覆寫使用者既有資料。
    recommended_keywords = [
        "找廠房", "租廠房", "買廠房", "找工業地", "工廠合法化",
        "特定工廠登記", "農地工廠合法化", "工廠用地變更", "丁種建築用地",
        "八德廠房", "大溪廠房", "龍潭廠房", "平鎮廠房", "桃園工業地",
        "八德倉庫", "桃園廠房出租", "廠房可以登記嗎", "農地工廠怎麼辦",
        "工廠登記申請", "違章工廠合法化", "想找廠房", "有推薦廠房仲介嗎",
    ]
    cursor.execute("SELECT value FROM system_settings WHERE key = 'recommended_keywords_v2_added'")
    if cursor.fetchone() is None:
        description = "善水工商地產：專營桃園、新北廠房、工業地、工廠合法化與用地變更服務。"
        for keyword in recommended_keywords:
            cursor.execute(
                "INSERT OR IGNORE INTO keywords (keyword, business_description) VALUES (?, ?)",
                (keyword, description),
            )
        cursor.execute("INSERT INTO system_settings (key, value) VALUES ('recommended_keywords_v2_added', '1')")
        # 舊版將多個搜尋詞串成一條，官方 API 會視為完整片語；保留資料但停止無效輪詢。
        cursor.execute(
            "UPDATE keywords SET is_active = 0 WHERE keyword = ?",
            ("八德 租廠房 租鐵皮廠房 租倉庫",),
        )
            
    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_facebook_sources(active_only: bool = False) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    query = "SELECT * FROM facebook_sources"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY id DESC"
    cursor.execute(query)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def create_facebook_source(name: str, group_url: str, max_posts: int = 100, no_proxy: int = 1, cookies_file: str = "") -> Optional[int]:
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO facebook_sources (name, group_url, max_posts, no_proxy, cookies_file)
               VALUES (?, ?, ?, ?, ?)""",
            (name.strip() or group_url, group_url, max_posts, no_proxy, cookies_file.strip()),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()

def toggle_facebook_source(source_id: int, is_active: int):
    conn = get_db()
    conn.execute(
        "UPDATE facebook_sources SET is_active = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        (1 if is_active else 0, source_id),
    )
    conn.commit()
    conn.close()

def get_facebook_source(source_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM facebook_sources WHERE id = ?", (source_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def create_facebook_job(source: Dict[str, Any]) -> int:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO facebook_jobs (source_id, group_url, max_posts, status)
           VALUES (?, ?, ?, 'queued')""",
        (source["id"], source["group_url"], source["max_posts"]),
    )
    job_id = cursor.lastrowid
    cursor.execute(
        """UPDATE facebook_sources
           SET last_job_id = ?, last_job_status = 'queued', last_job_error = '', updated_at = datetime('now', 'localtime')
           WHERE id = ?""",
        (job_id, source["id"]),
    )
    conn.commit()
    conn.close()
    return job_id

def get_facebook_job(job_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM facebook_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_facebook_jobs(limit: int = 30) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        """SELECT j.*, s.name AS source_name
           FROM facebook_jobs j JOIN facebook_sources s ON s.id = j.source_id
           ORDER BY j.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def update_facebook_job(job_id: int, **fields):
    allowed = {"runner_job_id", "status", "started_at", "finished_at", "exit_code", "error", "output_dir"}
    values = [(key, value) for key, value in fields.items() if key in allowed]
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key, _ in values)
    params = [value for _, value in values] + [job_id]
    conn = get_db()
    conn.execute(f"UPDATE facebook_jobs SET {assignments} WHERE id = ?", params)
    if "status" in fields:
        conn.execute(
            """UPDATE facebook_sources SET last_job_status = ?, last_job_error = ?, updated_at = datetime('now', 'localtime')
               WHERE id = (SELECT source_id FROM facebook_jobs WHERE id = ?)""",
            (fields["status"], fields.get("error", ""), job_id),
        )
    conn.commit()
    conn.close()

def save_facebook_posts(job_id: int, source_id: int, posts: List[Dict[str, Any]]):
    conn = get_db()
    conn.executemany(
        """INSERT OR IGNORE INTO facebook_posts
           (source_id, job_id, post_id, author, content, post_url, publish_time)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (source_id, job_id, str(post.get("postId") or post.get("post_id") or post.get("id") or post.get("url") or post.get("post_url") or ""),
             post.get("author", "") or post.get("authorName", "") or post.get("author_name", ""),
             post.get("content", "") or post.get("text", ""),
             post.get("postUrl", "") or post.get("post_url", "") or post.get("url", ""),
             post.get("publishTime", "") or post.get("publish_time", "") or post.get("createdAt", "") or post.get("created_at", ""))
            for post in posts
            if post.get("postId") or post.get("post_id") or post.get("id") or post.get("url") or post.get("post_url")
        ],
    )
    conn.commit()
    conn.close()

def get_facebook_posts(limit: int = 100) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        """SELECT p.*, s.name AS source_name
           FROM facebook_posts p JOIN facebook_sources s ON s.id = p.source_id
           ORDER BY p.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_facebook_posts_for_job(job_id: int, pending_only: bool = False) -> List[Dict[str, Any]]:
    conn = get_db()
    query = "SELECT * FROM facebook_posts WHERE job_id = ?"
    params: List[Any] = [job_id]
    if pending_only:
        query += " AND analysis_status = 'pending'"
    query += " ORDER BY id ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]

def update_facebook_post_analysis(
    post_row_id: int,
    status: str,
    matched_keyword: str = "",
    error: str = "",
):
    conn = get_db()
    conn.execute(
        """UPDATE facebook_posts
           SET analysis_status = ?, matched_keyword = ?, analysis_error = ?,
               analyzed_at = datetime('now', 'localtime')
           WHERE id = ?""",
        (status, matched_keyword, error[:500], post_row_id),
    )
    conn.commit()
    conn.close()

def update_keyword(kw_id: int, keyword: str, business_description: str) -> bool:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE keywords SET keyword = ?, business_description = ? WHERE id = ?",
            (keyword.strip(), business_description.strip(), kw_id),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def get_all_keywords(active_only: bool = False) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    if active_only:
        cursor.execute("SELECT * FROM keywords WHERE is_active = 1 ORDER BY id ASC")
    else:
        cursor.execute("SELECT * FROM keywords ORDER BY id ASC")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def add_keyword(keyword: str, business_description: str) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO keywords (keyword, business_description) VALUES (?, ?)", 
                       (keyword.strip(), business_description.strip()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def delete_keyword(kw_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM keywords WHERE id = ?", (kw_id,))
    conn.commit()
    conn.close()

def toggle_keyword(kw_id: int, is_active: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE keywords SET is_active = ? WHERE id = ?", (is_active, kw_id))
    conn.commit()
    conn.close()

def update_keyword_metrics(kw_id: int, scanned: int, hit: int, skipped: int, status: str = "ok", error: str = ""):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE keywords 
    SET last_scan_time = ?, last_scanned_count = ?, last_hit_count = ?, last_skipped_count = ?,
        last_scan_status = ?, last_scan_error = ?
    WHERE id = ?
    """, (now, scanned, hit, skipped, status, error[:300], kw_id))
    conn.commit()
    conn.close()

# ==================== 多維度精準去重檢查函式 ====================

def is_post_duplicate(post_id: str, post_url: str, content: str) -> bool:
    """
    三重去重檢驗：
    1. 貼文 ID (post_id) 是否已存在於 leads 或 skipped_logs
    2. 貼文網址 (post_url) 是否已存在於 leads 或 skipped_logs
    3. 貼文內容 Hash (content_hash) 是否在近期 (30天內) 已被記錄過（避免不同帳號洗版相同文案）
    """
    content_hash = compute_content_hash(content) if content else ""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 1 FROM leads WHERE post_id = ? OR post_url = ? OR (content_hash = ? AND content_hash != '')
        UNION
        SELECT 1 FROM skipped_logs WHERE post_id = ? OR post_url = ? OR (content_hash = ? AND content_hash != '')
    """, (post_id, post_url, content_hash, post_id, post_url, content_hash))
    
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def get_all_processed_ids_set() -> set:
    """載入所有已處理過的 ID、URL 與 Hash 集合至記憶體，加速第一層 O(1) 快取快篩"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT post_id FROM leads UNION SELECT post_id FROM skipped_logs")
    ids = {row[0] for row in cursor.fetchall() if row[0]}
    cursor.execute("SELECT post_url FROM leads UNION SELECT post_url FROM skipped_logs")
    urls = {row[0] for row in cursor.fetchall() if row[0]}
    cursor.execute("SELECT content_hash FROM leads WHERE content_hash IS NOT NULL UNION SELECT content_hash FROM skipped_logs WHERE content_hash IS NOT NULL")
    hashes = {row[0] for row in cursor.fetchall() if row[0]}
    conn.close()
    return ids.union(urls).union(hashes)

def save_lead(lead_dict: dict) -> bool:
    """儲存命中商機（具有資料庫級唯一性防護，若衝突自動略過）"""
    content = lead_dict.get("content", "")
    content_hash = compute_content_hash(content)
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
        INSERT INTO leads (post_id, content_hash, keyword, author, content, post_url, publish_time, ai_reason, confidence_score, suggested_reply, intent_type, location, urgency, is_competitor, contactability)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            lead_dict.get("post_id"),
            content_hash,
            lead_dict.get("keyword"),
            lead_dict.get("author"),
            content,
            lead_dict.get("post_url"),
            lead_dict.get("publish_time"),
            lead_dict.get("ai_reason"),
            lead_dict.get("confidence_score", 1.0),
            lead_dict.get("suggested_reply", ""),
            lead_dict.get("intent_type", ""),
            lead_dict.get("location", ""),
            lead_dict.get("urgency", ""),
            int(bool(lead_dict.get("is_competitor", False))),
            lead_dict.get("contactability", "")
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def save_skipped(skipped_dict: dict) -> bool:
    """儲存略過日誌（具有唯一性防護）"""
    content = skipped_dict.get("content", "")
    content_hash = compute_content_hash(content)
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
        INSERT INTO skipped_logs (post_id, content_hash, keyword, author, content, post_url, ai_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            skipped_dict.get("post_id"),
            content_hash,
            skipped_dict.get("keyword"),
            skipped_dict.get("author"),
            content,
            skipped_dict.get("post_url"),
            skipped_dict.get("ai_reason")
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def clean_expired_records(days: int = 30):
    """定時自動清理超過指定天數（預設 30 天）的舊日誌"""
    conn = get_db()
    cursor = conn.cursor()
    cutoff_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("DELETE FROM skipped_logs WHERE created_at < ?", (cutoff_date,))
    conn.commit()
    conn.close()

def get_leads(limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    if status and status != "all":
        cursor.execute("SELECT * FROM leads WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
    else:
        cursor.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def update_lead_status(lead_id: int, status: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    conn.commit()
    conn.close()

def get_skipped_logs(limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM skipped_logs ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def get_summary_stats() -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM leads")
    total_leads = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM skipped_logs")
    total_skipped = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM keywords WHERE is_active = 1")
    active_keywords = cursor.fetchone()[0]
    conn.close()
    return {
        "total_leads": total_leads,
        "total_skipped": total_skipped,
        "active_keywords": active_keywords
    }
