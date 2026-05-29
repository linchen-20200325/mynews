# STATE.md — 專案戰情室

> 最後更新:2026-05-29

## 當前環境

- 語言/執行:Python 3.11
- 相依:`anthropic`、`google-genai`、`streamlit`(見 `requirements.txt`)
- 自動化:GitHub Actions(`.github/workflows/daily_update.yml`,每日 UTC 00:00 / 台灣 08:00)
- 金鑰:`ANTHROPIC_API_KEY`(必)、`GEMINI_API_KEY`(選);皆透過 GitHub Secrets 注入

## 已完成 ✅

- [x] `update_data.py` — Claude(web_search)四維度分析 → JSON
- [x] 雙模型:Gemini 產生最終版白話文字典(失敗自動回退 Claude)
- [x] 穩健 JSON 清理 + 結構驗證 + `pause_turn` 續跑
- [x] `app.py` — Streamlit 看板 + 歷史報告側邊欄選擇器
- [x] 歷史存檔 `data/reports/<date>.json`
- [x] 趨勢雷達:Claude + web_search 找最熱門產業,依資金/徵才/政策/技術排名打分
      (`latest_trends.json` + `data/trends/<date>.json`,可用 ENABLE_TREND_RADAR=0 關閉)
- [x] Streamlit 側邊欄切換「戰略報告 / 趨勢雷達」
- [x] GitHub Actions 每日排程 + 自動 commit/push
- [x] LINE 推播報告摘要(Messaging API,最佳努力、失敗不影響報告)
- [x] `CLAUDE.md` / `STATE.md` 專案文件

## 待辦 / 可優化 ⏳

- [ ] 在 GitHub repo 設定 `ANTHROPIC_API_KEY`(以及選用的 `GEMINI_API_KEY`)Secret
- [ ] 手動 Run workflow 跑一次,確認能產出 `latest_report.json`
- [ ] 部署 Streamlit(如 Streamlit Community Cloud)對外提供看板
- [ ] 可考慮多主題(一次產生數份不同主題報告)
- [ ] 可考慮加上 email / LINE / Slack 推播

## 已知問題 / 注意事項 ⚠️

- Gemini 模型名稱(`gemini-2.5-flash`)若不可用,請以 `GEMINI_MODEL` 變數覆寫。
- GitHub Actions 排程常有數分鐘~數十分鐘延遲,屬正常現象。
- 產出內容為 AI 生成,僅供參考,非投資建議。
