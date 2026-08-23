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
mkdir -p data/facebook-output data/facebook-cookies
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

Facebook Cookie 檔案若要使用，請以主機檔案放在：

```text
/home/davicool/lead-radar/data/facebook-cookies/cookies.json
```

後台欄位填入容器路徑 `/app/cookies/cookies.json`。Cookie 不應提交到 Git，也不要貼到聊天或日誌。

## 注意

- 目前只部署單一 worker，避免背景排程重複掃描。
- 尚未公開掛載網域，也未接入 SSAOS。
- 正式使用前仍需補登入驗證與 Threads 掃描穩定性測試。
- `docker compose` 的 runner build context 是相鄰的 `facebook-public-group-scraper-standalone`；兩個獨立 GitHub 專案都必須存在於 `/home/davicool` 下。
