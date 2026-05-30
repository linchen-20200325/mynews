# STATE.md — 專案戰情室

> 最後更新:2026-05-30

## 當前環境

- 語言/執行:Python 3.11
- 相依:`google-genai`、`streamlit`、`pandas`、`requests`(見 `requirements.txt`);RSS 爬蟲用標準函式庫
- 自動化:
  - `daily_update.yml`:每日 UTC 00:00(台灣 08:00)產戰略報告/趨勢雷達/台股觀察
  - `update_etf.yml`:每月 1 號透過代理抓 MoneyDJ 更新 ETF 成分股
- 金鑰/設定(GitHub Secrets 或 Streamlit Secrets):
  - `GEMINI_API_KEY`(必,支援複數 key 容錯)
  - `PROXY_URL`(NAS 代理,供 ETF 爬蟲走 MoneyDJ;格式 http://帳:密@host:3128)
  - `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO`(選)
- 看板:Streamlit Community Cloud(`*.streamlit.app`),共 4 頁

## 架構摘要

RSS 爬蟲抓真實新聞 → Gemini 全包分析;另有 ETF 成分股反查(透過 NAS 代理抓 MoneyDJ)。
抓新聞:動態分類頭條(世界/財經)為主 + 聚焦關鍵字(聯準會/股匯債/地緣軍事)為輔。

## 已完成 ✅

- [x] `news_fetcher.py` — 標準函式庫 RSS/Atom 爬蟲;繁中/台灣來源;動態分類頭條 + 聚焦關鍵字;
      去 HTML/去重/時間排序;每則標 `origin`(來源管道)
- [x] `update_data.py` — Gemini 全包:四維度戰略分析 + 白話文、趨勢雷達、台股觀察;
      多把金鑰容錯 `get_gemini_keys()`;穩健 JSON 清理 + 驗證;失敗隔離
- [x] 看板 4 頁(`app.py`):
      - 戰略報告:① 抓新聞 → ② Gemini 分析+白話文(兩步手動)
      - 趨勢雷達:① 抓產業新聞 → ② Gemini 排名打分
      - 台股觀察:① 抓財經新聞 → ② Gemini 整理(總表/利多/利空/觀望 + 趨勢/夕陽),
        並併入「ETF 持有檔數」交叉參照
      - ETF 持股反查:輸入個股代號/名稱 → 反查被哪些 ETF 持有;🛰️ 代理按鈕即時建庫
- [x] `etf_holdings.py` / `etf_holdings.json` — 個股→ETF 反查(純資料)
- [x] `etf_fetcher.py` / `etf_sources.json` — 透過 `PROXY_URL` 代理抓 MoneyDJ 成分股建庫
      (requests + proxies、HTML 表格解析、單檔失敗不影響其他、抓不到保留既有)
- [x] 金鑰診斷:Streamlit 讀不到金鑰時列出 Secrets 名稱與正確 TOML 寫法
- [x] 每日/每月 GitHub Actions 排程 + 自動 commit;LINE 推播(最佳努力)
- [x] 文件:`CLAUDE.md` / `STATE.md` / `README.md`

## 待辦 / 可優化 ⏳

- [ ] 在 Streamlit 上按「🛰️ 透過代理更新成分股」實測 MoneyDJ;若解析對不上,依抓取明細修
- [ ] 設定 `GEMINI_API_KEY`(看板即時分析 + 每日排程)
- [ ] (選)在 repo Secrets 設 `PROXY_URL` 讓每月排程自動抓 ETF(NAS 防火牆需放行 Actions IP)
- [ ] 抓到完整 ETF 庫後 commit `etf_holdings.json` 永久保存
- [ ] 可考慮多主題報告

## 已知問題 / 注意事項 ⚠️

- 沙箱/本機無外網時,RSS 與 MoneyDJ 都抓不到;Streamlit Cloud / Actions 網路正常。
- MoneyDJ 實際 HTML 結構/`etfid` 需用線上回應驗證,解析器可能需微調一次。
- ETF 成分股屬事實資料;請維持合理抓取頻率並留意來源網站服務條款。
- `PROXY_URL` 含帳密,只走環境變數/Secrets;`.streamlit/secrets.toml` 已 gitignore,不可進版控。
- 「金鑰讀不到」多為命名/格式問題:名稱需為 `GEMINI_API_KEY`,複數 key 用逗號或陣列。
- Gemini 模型(`gemini-2.5-flash`)若不可用,以 `GEMINI_MODEL` 覆寫。
- 所有產出為 AI/工具自動生成,僅供參考,非投資建議。
