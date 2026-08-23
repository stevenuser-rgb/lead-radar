import os
import json
import httpx
from database import get_setting

def analyze_post_intent(post_content: str, keyword: str, business_description: str) -> dict:
    """
    呼叫 Google Gemini API (或相容端點) 分析貼文是否為潛在客戶的真實需求。
    若未設定 API Key，則使用本機語意啟發式規則作為備用。
    """
    gemini_api_key = get_setting("gemini_api_key", os.getenv("GEMINI_API_KEY", ""))
    
    if gemini_api_key:
        try:
            return call_gemini_api(gemini_api_key, post_content, keyword, business_description)
        except Exception as e:
            print(f"[AI Engine Error] 呼叫 Gemini 失敗: {e}，切換為規則判定。")
            
    return fallback_heuristic_analyzer(post_content, keyword, business_description)


def call_gemini_api(api_key: str, post_content: str, keyword: str, business_description: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key}"
    
    system_prompt = f"""
你是一個精準的商務需求雷達（Lead Radar）AI 意圖判斷引擎。
我們的業務說明為：【{business_description}】
追蹤的關鍵字為：【{keyword}】

請仔細閱讀以下社群公開貼文，並嚴格以 JSON 格式回應以下欄位：
1. "is_lead" (bool): 發文者是否為「真正有需求、正在尋找服務、詢問推薦、或遇到問題尋求專業協助」的潛在客戶？
   - 若為客戶求助/發問/找廠商/找代書/找顧問，請回傳 true。
   - 若為同行廣告宣傳、新聞法規轉發、純粹抒發抱怨、招募求職或無關閒聊，請回傳 false。
2. "confidence_score" (float): 判定信心度 (0.0 ~ 1.0)。
3. "ai_reason" (string): 繁體中文簡短說明判定原因（25字以內）。
4. "suggested_reply" (string): 若判定為 true，請提供一段簡短專業的切入回覆建議；若為 false 則為空字串。
5. "intent_type" (string): 租賃、購買、合法化、用地變更、諮詢或其他。
6. "location" (string): 文中提及的地區，無則為空字串。
7. "urgency" (string): high、medium 或 low。
8. "is_competitor" (bool): 是否為同業、仲介或服務商宣傳。
9. "contactability" (string): high、medium 或 low，表示是否適合公開回覆接觸。

【待分析貼文內容】
{post_content}
"""

    payload = {
        "contents": [{"parts": [{"text": system_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(raw_text)
        return {
            "is_lead": bool(result.get("is_lead", False)),
            "confidence_score": float(result.get("confidence_score", 0.9)),
            "ai_reason": str(result.get("ai_reason", "AI 判定符合業務意圖")),
            "suggested_reply": str(result.get("suggested_reply", "")),
            "intent_type": str(result.get("intent_type", "其他")),
            "location": str(result.get("location", "")),
            "urgency": str(result.get("urgency", "low")),
            "is_competitor": bool(result.get("is_competitor", False)),
            "contactability": str(result.get("contactability", "low")),
        }


def fallback_heuristic_analyzer(content: str, keyword: str, business_description: str) -> dict:
    """
    備用規則過濾器：當使用者尚未填寫 Gemini API Key 時提供基本判斷
    """
    inquiry_indicators = ["推薦", "請問", "想找", "有沒有", "費用", "流程", "代辦", "諮詢", "評估", "請教", "怎麼辦", "求助", "哪裡有"]
    ad_indicators = ["歡迎洽詢", "私訊我", "本公司專營", "專線", "點擊連結", "限時特惠", "歡迎委託", "立即預約", "點我加賴", "專業代辦歡迎"]
    
    has_inquiry = any(word in content for word in inquiry_indicators)
    has_ad = any(word in content for word in ad_indicators)
    
    if has_inquiry and not has_ad:
        return {
            "is_lead": True,
            "confidence_score": 0.85,
            "ai_reason": "偵測到明確詢問與尋求推薦之關鍵字，符合潛在客戶特徵。",
            "suggested_reply": "主動說明專業顧問背景，並提供初步法規/實務建議以建立信任。",
            "intent_type": "諮詢", "location": "", "urgency": "medium",
            "is_competitor": False, "contactability": "medium",
        }
    elif has_ad:
        return {
            "is_lead": False,
            "confidence_score": 0.90,
            "ai_reason": "貼文包含宣傳與推廣用語，判定為同行或行銷貼文。",
            "suggested_reply": "", "intent_type": "其他", "location": "",
            "urgency": "low", "is_competitor": True, "contactability": "low",
        }
    else:
        return {
            "is_lead": False,
            "confidence_score": 0.75,
            "ai_reason": "純時事提及或閒聊，未發現具體發問或委託意圖。",
            "suggested_reply": "", "intent_type": "其他", "location": "",
            "urgency": "low", "is_competitor": False, "contactability": "low",
        }
