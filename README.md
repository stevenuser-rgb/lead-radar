# 🛰️ 私有商務需求雷達 (Lead Radar Web App)

> 100% 完整復刻 [需求雷達 (Lead Radar)](https://lead-radar.homo.tw/) 功能架構，專為「善水工商地產」量身打造的私有 AI 潛在客戶自動探測系統。

---

## ✨ 核心特色與功能模組
1. **即時監控看盤 (Transparency View)**：即時掌握各關鍵字之掃描接收數、🎯 命中數與 🚫 略過數。
2. **命中需求庫 (Hit Leads - 30天紀錄)**：AI 語意判斷客戶真實需求，提供一鍵「直達 Threads 回覆」與跟進狀態切換。
3. **略過貼文與 AI 理由日誌 (Skipped Logs)**：透明列出每則被排除貼文之 AI 判定理由，方便微調業務說明與關鍵字。
4. **關鍵字與業務背景自訂**：支援最多 10 組關鍵字與自訂專業業務簡介（AI System Prompt Context）。
5. **多管道即時推播**：支援 LINE Notify / LINE Messaging API (Bot)，命中時第一時間通報。
6. **10 分鐘自動背景排程**：內建非同步排程器，每 10 分鐘自動執行一輪全網檢索與意圖分析。
7. **Facebook 社團模組**：獨立管理社團來源、抓取任務、runner 日誌與貼文結果；結果頁支援日期、來源、狀態、文字篩選與分頁，完整內容以詳細視窗載入；抓取程式以相鄰的獨立 `facebook-public-group-scraper-standalone` 專案建置，不建立 fork 或原作者依賴。

Facebook 社團來源提供兩種手動任務：一般抓取依來源貼文上限執行；「深度」模式只可由後台手動啟動，最多抓取 500 篇並提高無新貼文停止門檻。自動排程固定使用一般模式，避免無意間增加帳號請求量。

### Facebook 持續監控（可選）

系統設定中的「Facebook 持續監控架構」預設關閉，開啟後仍須在每個社團來源按「開啟監控」才會生效。監控以設定的 30 分鐘以上間隔輪詢最近貼文，使用獨立的持久登入狀態目錄與監控輸出目錄；新資料仍回到既有 Facebook 貼文、關鍵字比對、AI 分析、命中需求與略過紀錄流程。監控來源不會同時被既有 Facebook 自動排程重複抓取，手動一般／深度任務仍保留。

深度掃描啟動後會取得全域互斥鎖；一般抓取、自動排程與監控輪詢會暫停，直到深度任務完成或失敗，避免同時增加 Facebook 請求量。

Cupmen 的 `data/facebook-state` 與 `data/facebook-monitor-output` 僅供容器執行期使用，不要提交到 Git；Cookie 仍放在 `data/facebook-cookies`。

---

## 🚀 快速啟動指南

### 1. 本機安裝與執行
```bash
# 1. 進入專案目錄
cd lead_radar_webapp

# 2. 安裝依賴套件
pip install -r requirements.txt

# 3. 啟動 Web 服務
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```
啟動後開啟瀏覽器訪問：`http://localhost:8000` 即可進入儀表板。

---

### 2. Docker 一鍵部署 (支援 Zeabur / Render / Railway / VPS)
```bash
docker build -t lead-radar-app .
docker run -d -p 8000:8000 --name lead-radar lead-radar-app
```

Facebook 社團模組的 Cupmen 部署方式與資料目錄，請參閱 [`DEPLOY_CUPMEN.md`](DEPLOY_CUPMEN.md)。
