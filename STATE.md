# STATE.md — 專案戰情室

> 最後更新:2026-05-29

## 當前環境

- 語言/執行:Python 3.11
- 相依:`google-genai`、`streamlit`、`pandas`(見 `requirements.txt`);爬蟲用標準函式庫
- 自動化:GitHub Actions(`.github/workflows/daily_update.yml`,每日 UTC 00:00 / 台灣 08:00)
- 金鑰:`GEMINI_API_KEY`(必);GitHub Secrets(排程)或 Streamlit Secrets(看板)注入
- 看板:已部署 Streamlit Community Cloud(`*.streamlit.app`)

## 架構摘要

RSS 爬蟲抓真實新聞 → Gemini 全包(四維度分析 + 白話文 + 趨勢雷達)。
`raw_news` 直接採用爬蟲結果,結構上保證真實、不虛構。
抓取策略:**動態分類頭條為主(世界/財經)+ 聚焦關鍵字為輔**(聯準會/利率通膨/
股匯債/地緣軍事),聚焦會影響股市與基金的訊息,並排除娛樂/體育等離題內容。

## 已完成 ✅

- [x] `news_fetcher.py` — 純標準函式庫 RSS/Atom 爬蟲;去 HTML、去重、依時間排序、可設回溯時數
- [x] 新聞來源:繁中/台灣(`zh`/`TW`),Google News 搜尋 + 分類頭條 + 中文官方 feed
      (中央社國際/兩岸/財經、BBC 中文、DW);可用 `NEWS_LANG`/`NEWS_REGION` 切回英文
- [x] 動態分類頭條(`google_news_topic_url`,WORLD/BUSINESS/TECHNOLOGY…),可用 `NEWS_TOPICS`/`TREND_TOPICS` 調
- [x] 每則新聞記錄 `origin`(來源管道:分類頭條/官方feed/關鍵字),前端顯示
- [x] `update_data.py` — 餵新聞給 Gemini → 四維度分析 + 白話文(`dictionary_source=gemini`),
      分析強調對股市/債市/基金/資產配置的影響
- [x] 多把金鑰容錯:`get_gemini_keys()`(`GEMINI_API_KEY` 逗號分隔 / `GEMINI_API_KEYS` /
      `GEMINI_API_KEY_1.._n`),`call_gemini_for_json` 逐把嘗試、失敗自動換下一把
- [x] 趨勢雷達:Gemini 讀「財經+科技」頭條 → 依資金/徵才/政策/技術排名打分
      (`latest_trends.json` + `data/trends/<date>.json`,可用 `ENABLE_TREND_RADAR=0` 關閉)
- [x] `app.py` — Streamlit 看板:歷史側邊欄、趨勢歷史折線圖
- [x] **即時按鈕(兩步、皆手動)**:戰略報告與趨勢雷達兩頁皆有
      ① 立即抓新聞(只跑爬蟲) → ② 用 Gemini 分析/排名(看過後才觸發);結果可下載 JSON
- [x] 金鑰診斷:Streamlit 讀不到金鑰時,畫面列出目前 Secrets 名稱與正確 TOML 寫法
- [x] GitHub Actions 每日排程 + 自動 commit/push(含 trends 檔)
- [x] LINE 推播報告摘要(Messaging API,最佳努力、失敗不影響報告)
- [x] `CLAUDE.md` / `STATE.md` / `README.md` 專案文件

## 待辦 / 可優化 ⏳

- [ ] **設定 `GEMINI_API_KEY`**:Streamlit Secrets(看板即時分析)+ GitHub Secret(每日排程)
- [ ] 開放 GitHub Actions 寫入權限後,手動 Run workflow 跑一次,確認產出 `latest_report.json`
- [ ] 視抓取結果微調 `NEWS_QUERIES`/`TREND_QUERIES`/`NEWS_TOPICS`
- [ ] 可考慮多主題(一次產生數份不同主題報告)

## 已知問題 / 注意事項 ⚠️

- 「金鑰讀不到」多為設定問題:Secrets 名稱需完全為 `GEMINI_API_KEY`;複數 key 用逗號或陣列寫法,
  避免重複鍵導致整份 TOML 解析失敗。看新聞不需金鑰,Gemini 分析才需要。
- 沙箱環境(如本機受限 runner)若無對外網路,爬蟲會抓到 0 則新聞;GitHub Actions / Streamlit Cloud 網路正常。
- Gemini 模型名稱(`gemini-2.5-flash`)若不可用,請以 `GEMINI_MODEL` 變數覆寫。
- 趨勢雷達靠爬到的新聞 + Gemini 知識推估(無即時搜尋),evidence 以提供的新聞為準。
- GitHub Actions 排程常有數分鐘~數十分鐘延遲,屬正常現象。
- 產出內容為 AI 生成,僅供參考,非投資建議。
