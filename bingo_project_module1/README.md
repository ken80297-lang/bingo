# 台彩賓果賓果分析網頁版｜專案模組1

用途：自動抓取賓果賓果每一期開獎資料，存入 SQLite，並用「專案模組1」在網頁顯示三星、五星、超級獎號候選、大小單雙偏向。

> 注意：BINGO BINGO 為隨機開獎，分析只作統計參考，不保證中獎，請控制本金。

## Windows 最簡單使用方式
1. 安裝 Python 3.12。
2. 解壓縮此資料夾。
3. 直接點兩下 `run_web.bat`。
4. 瀏覽器打開：`http://127.0.0.1:5000`

## macOS 使用方式
1. 安裝 Python 3。
2. 解壓縮此資料夾。
3. 點兩下 `run_web.command`，或在終端機執行：

```bash
pip install -r requirements.txt
python app.py
```

4. 瀏覽器打開：`http://127.0.0.1:5000`

## 網頁功能
- 啟動後先自動抓一次。
- 每 5 分鐘自動抓取更新。
- 可按「立即更新」。
- 顯示最新期別、上一期號碼、三星、五星、超級獎號候選、大小單雙。
- 資料庫位置：`data/bingo.db`

## 模組1分析規則
1. 熱號：最近 20 / 50 期出現頻率。
2. 重號：上一期開出號碼加權。
3. 補號：上一期號碼附近 ±1、±2。
4. 斜線：1–80 排成 8x10 盤面，看左上、右上、左下、右下。
5. 雙生號：11、22、33、44、55、66、77。
6. 大小：1–40 小，41–80 大；13 顆以上判定偏向。
7. 單雙：奇偶數；13 顆以上判定偏向。
8. 超級獎號：熱度、重號、雙生號加權挑候選。

## 手動單次分析
```bash
python src/main.py
```


## 直接變成公開網站（Render 免費方案）
1. 把整個資料夾上傳到 GitHub。
2. 到 Render 建立 New Web Service，連接這個 GitHub 專案。
3. Render 會讀取 `render.yaml`，自動安裝與啟動。
4. 完成後會給你一個 `https://xxx.onrender.com` 網址，手機可直接開。

Render 設定值：
- Build Command：`pip install -r requirements.txt`
- Start Command：`gunicorn app:app --bind 0.0.0.0:$PORT`
