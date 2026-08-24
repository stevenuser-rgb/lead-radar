# 杯麵主機部署指引

## 目的

先以內網測試方式部署，不修改 `www.shansui.tw`、Home Assistant 或 SSAOS。

## 杯麵主機執行

```bash
mkdir -p /home/davicool/lead-radar
cd /home/davicool/lead-radar

# Lead Radar 會以相鄰目錄建置獨立 Facebook runner
git clone https://github.com/stevenuser-rgb/lead-radar.git . 2>/dev/null || git pull --ff-only origin main
cd ..
git clone https://github.com/stevenuser-rgb/facebook-public-group-scraper-standalone.git 2>/dev/null || git -C facebook-public-group-scraper-standalone pull --ff-only origin main
cd lead-radar
mkdir -p data/facebook-output data/facebook-cookies data/facebook-monitor-output data/facebook-state
# Playwright runner runs as pwuser (UID 1001); bind-mounted monitor paths must be writable by it.
sudo chown -R 1001:1001 data/facebook-monitor-output data/facebook-state
sudo chmod -R u+rwX,go-rwx data/facebook-monitor-output data/facebook-state
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 lead-radar
```

## 測試網址

同一台主機：

```text
http://127.0.0.1:8010
```

開啟後從頂部導覽進入「Facebook 社團」，再依序填寫社團來源、貼文上限與（需要時）容器內 Cookie 路徑。抓取任務由 `facebook-runner` 執行，結果會寫入 `data/facebook-output`，來源、任務與貼文索引會寫入 `data/radar.db`。

帳號安全限制：Facebook 抓取預設手動啟動；若開啟自動排程，單次最多 500 篇，同一社團完成後預設至少等待 30 分鐘；不自動登入、不解 CAPTCHA、不執行發文或互動，也不以 Proxy 繞過封鎖。若 Facebook 顯示 checkpoint、驗證或存取警告，應立即停止任務並檢查授權與平台規範。

若要啟用 Facebook 自動排程，請在「系統設定」分開開啟 Facebook 掃描系統、Facebook 自動排程，並選擇 30 分鐘以上的頻率。Threads 的掃描開關與頻率不會影響 Facebook。

區域網路測試時，將 `docker-compose.yml` 的：

```yaml
127.0.0.1:8010:8000
```

改為：

```yaml
8010:8000
```

再重新啟動容器。

## 停止與更新

```bash
docker compose down
docker compose up -d --build
```

資料會保留在：

```text
/home/davicool/lead-radar/data/radar.db
```

Facebook 結果頁預設只查詢最近 7 天、每頁 50 筆，可依社團、分析狀態、文字與日期範圍篩選；系統設定中的「原始貼文保留天數」預設為 90 天。每日排程會清理超過期限的未命中、略過、重複與錯誤原始貼文，命中需求不會清除。同一社團的同一 Facebook 貼文跨任務會以 `source_id + post_id` 去重。

Facebook Cookie 檔案若要使用，請以主機檔案放在：

```text
/home/davicool/lead-radar/data/facebook-cookies/cookies.json
```

後台欄位填入容器路徑 `/app/cookies/cookies.json`。Cookie 不應提交到 Git，也不要貼到聊天或日誌。

也可以直接在 Facebook 後台的「Facebook 登入 Cookie」區塊上傳檔名為 `cookies.json` 的檔案。系統會驗證 JSON 格式後，以權限 `600` 寫入 `data/facebook-cookies/cookies.json`；未另外指定 Cookie 路徑的來源會自動使用 `/app/cookies/cookies.json`。Cookie 過期或 Facebook 導向登入頁時，請重新匯出並上傳，不要在系統中自動重試登入。

持續監控使用 `data/facebook-monitor-output` 與 `data/facebook-state`。若出現 `EACCES: permission denied, mkdir /app/monitor-output`，重新執行上方的 `chown`／`chmod` 後再重啟 `facebook-runner`。

## 注意

- 目前只部署單一 worker，避免背景排程重複掃描。
- 尚未公開掛載網域，也未接入 SSAOS。
- 正式使用前仍需補登入驗證與 Threads 掃描穩定性測試。
- `docker compose` 的 runner build context 是相鄰的 `facebook-public-group-scraper-standalone`；兩個獨立 GitHub 專案都必須存在於 `/home/davicool` 下。
