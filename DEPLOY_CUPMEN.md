# 杯麵主機部署指引

## 目的

先以內網測試方式部署，不修改 `www.shansui.tw`、Home Assistant 或 SSAOS。

## 杯麵主機執行

```bash
mkdir -p /home/davicool/lead-radar
cd /home/davicool/lead-radar

# 將本專案檔案放入此目錄後執行
mkdir -p data
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 lead-radar
```

## 測試網址

同一台主機：

```text
http://127.0.0.1:8010
```

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

## 注意

- 目前只部署單一 worker，避免背景排程重複掃描。
- 尚未公開掛載網域，也未接入 SSAOS。
- 正式使用前仍需補登入驗證與 Threads 掃描穩定性測試。
