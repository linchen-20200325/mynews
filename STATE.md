# STATE.md — 專案戰情室

> 最後更新:2026-05-29

## 當前環境

- 語言/執行:Python 3.11
- 相依:`google-genai`、`streamlit`、`pandas`(見 `requirements.txt`);爬蟲用標準函式庫
- 自動化:GitHub Actions(`.github/workflows/daily_update.yml`,每日 UTC 00:00 / 台灣 08:00)
- 金鑰:`GEMINI_API_KEY`(必);透過 GitHub Secrets 注入

## 架構摘要

RSS 爬蟲抓真實外電 → Gemini 全包(四維度分析 + 白話文 + 趨勢雷達)。
`raw_news` 直接採用爬蟲結果,結構上保證真實、不虛構。

## 已完成 ✅

- [x] `news_fetcher.py` — 純標準函式庫 RSS/Atom 爬蟲(Google News RSS + 官方 feed),
      去 HTML、去重、依時間排序、可設回溯時數
- [x] `update_data.py` — 餵新聞給 Gemini → 四維度分析 + 白話文(`dictionary_source=gemini`)
- [x] 穩健 JSON 清理 + 結構驗證
- [x] 趨勢雷達:Gemini 讀產業新聞,依資金/徵才/政策/技術排名打分
      (`latest_trends.json` + `data/trends/<date>.json`,可用 ENABLE_TREND_RADAR=0 關閉)
- [x] `app.py` — Streamlit 看板 + 歷史報告側邊欄選擇器 + 趨勢歷史折線圖
- [x] GitHub Actions 每日排程 + 自動 commit/push(含 trends 檔)
- [x] LINE 推播報告摘要(Messaging API,最佳努力、失敗不影響報告)
- [x] `CLAUDE.md` / `STATE.md` / `README.md` 專案文件

## 待辦 / 可優化 ⏳

- [ ] 在 GitHub repo 設定 `GEMINI_API_KEY` Secret
- [ ] 手動 Run workflow 跑一次,確認能產出 `latest_report.json`
- [ ] 視抓取結果微調 `NEWS_QUERIES` / `TREND_QUERIES` 關鍵字與語系
- [ ] 部署 Streamlit(如 Streamlit Community Cloud)對外提供看板
- [ ] 可考慮多主題(一次產生數份不同主題報告)

## 已知問題 / 注意事項 ⚠️

- 沙箱環境(如本機受限 runner)若無對外網路,爬蟲會抓到 0 則新聞;GitHub Actions 網路正常。
- Gemini 模型名稱(`gemini-2.5-flash`)若不可用,請以 `GEMINI_MODEL` 變數覆寫。
- 趨勢雷達靠爬到的新聞 + Gemini 知識推估(無即時搜尋),evidence 以提供的新聞為準。
- GitHub Actions 排程常有數分鐘~數十分鐘延遲,屬正常現象。
- 產出內容為 AI 生成,僅供參考,非投資建議。
