import httpx
from database import get_setting

def send_lead_notification(lead_data: dict):
    """
    依據使用者設定，自動觸發 LINE / Email 即時推播
    """
    keyword = lead_data.get("keyword", "")
    author = lead_data.get("author", "匿名用戶")
    content = lead_data.get("content", "")
    url = lead_data.get("post_url", "")
    reason = lead_data.get("ai_reason", "")
    suggested_reply = lead_data.get("suggested_reply", "")
    
    # LINE Messaging API (Bot Push)
    line_bot_token = get_setting("line_bot_token")
    line_user_id = get_setting("line_user_id")
    if line_bot_token and line_user_id:
        try:
            headers = {
                "Authorization": f"Bearer {line_bot_token}",
                "Content-Type": "application/json"
            }
            payload = {
                "to": line_user_id,
                "messages": [
                    {
                        "type": "text",
                        "text": f"🎯【需求雷達 命中需求】\n關鍵字：#{keyword}\n發文者：{author}\n\n內容：\n{content}\n\n💡 AI 理由：{reason}\n\n👉 直達貼文：{url}"
                    }
                ]
            }
            response = httpx.post("https://api.line.me/v2/bot/message/push", headers=headers, json=payload, timeout=10.0)
            response.raise_for_status()
            print(f"[Notifier] LINE Bot 發送成功: {url}")
            return True
        except Exception as e:
            print(f"[Notifier Error] LINE Bot 發送失敗: {e}")
            return False
    print("[Notifier] 尚未設定 LINE Bot Token 或 LINE User ID")
    return False
