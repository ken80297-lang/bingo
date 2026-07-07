# Bingo AI Pro

完整可部署的 Bingo AI Pro 網站，包含 React/Vite/Tailwind 前端、FastAPI 後端、SQLite 資料庫、APScheduler 排程，並支援 Render 部署與 GitHub Actions 自動化。

## 專案結構

- `backend/`: FastAPI 應用與資料庫、分析模組
- `frontend/`: React + Vite + Tailwind PWA
- `render.yaml`: Render 自動部署設定
- `.github/workflows/ci.yml`: GitHub Actions 每日同步與建置

## 本地執行

### 1. 前端

```bash
cd bingo-ai-pro/frontend
npm install
npm run dev
```

### 2. 後端

```bash
cd bingo-ai-pro/backend
python -m pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

### 3. 同時啟動前後端

前端預設會向 `/api` 端點請求，建議部署時使用反向代理或 Render 直接在後端內建靜態資源。

## Render 部署

1. 將整個 `bingo-ai-pro` 資料夾推上 GitHub。
2. 建立 Render Web Service，選擇 `Python`。
3. 部署設定：
   - Build Command: `pip install -r backend/requirements.txt`
   - Start Command: `uvicorn backend.app:app --host 0.0.0.0 --port $PORT`
4. Render 會自動讀取 `render.yaml`。

## GitHub Actions

- `CI` workflow 會在 `main` branch push 或每天 UTC 02:00 觸發。
- 會安裝 Python/Node.js、建置前端、執行後端測試。

## PWA 支援

- `frontend/public/manifest.webmanifest` 已配置
- 建置後可加入主畫面

## API

- `GET /api/latest`
- `GET /api/history`
- `GET /api/analyze`
- `GET /api/recommend`
- `POST /api/update`

## 資料庫

SQLite 檔案會建立於 `backend/data/bingo.db`。

## 開發補充

- 分析模組位於 `backend/analysis`
- 資料存取與歷史查詢位於 `backend/db.py`
- 排程每 5 分鐘自動更新

## Cloud Deployment

### Render

1. Create a new Render Web Service from this repository.
2. Use Python as the runtime.
3. Set the build command:

```bash
pip install -r backend/requirements.txt
```

4. Set the start command:

```bash
uvicorn backend.app:app --host 0.0.0.0 --port $PORT
```

5. Add the required environment variables in Render.

### Supabase Environment Variables

```env
DATABASE_URL=
SUPABASE_URL=
SUPABASE_KEY=
PROVIDER=kuaishou
```

`DATABASE_URL` should be the Supabase Postgres connection string. The collector storage layer writes to Supabase first and falls back to local SQLite if the cloud database is unavailable.

### API Checks

After deployment, verify these endpoints:

- `GET /api/health`
- `GET /api/system/status`
- `GET /api/collector/status`
- `GET /api/kuaishou/latest`
- `GET /api/kuaishou/history?limit=50`
- `GET /api/draws/latest`
- `GET /api/draws/history?limit=50`
- `GET /api/latest`
- `GET /api/history`
- `GET /api/recommend`

### VPS

Install dependencies and run the same start command:

```bash
pip install -r backend/requirements.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

For production VPS usage, run the command under a process manager such as systemd, Supervisor, or pm2.
