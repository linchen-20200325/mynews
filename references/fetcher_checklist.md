# references/fetcher_checklist.md — 資料抓取 (fetcher) 撰寫檢查表

> 新增/改任何 `*_fetcher.py` 時逐項核對。

## 硬規則（不可違反）
- [ ] **真實優先**：`raw_news` 一律真實 RSS/feed；餵 Gemini 明講「只能依提供的新聞分析」，嚴禁虛構。
- [ ] **房價/ETF 成分股嚴禁 AI 猜**：房價走內政部實價登錄、ETF 成分股走官方源；AI 只判讀冷熱/政策/題材。
- [ ] **合法抓取**：只用網站開放的 RSS/feed，不硬爬付費牆全文。

## 架構（SSOT）
- [ ] 過境 NAS 代理走 `proxy_helper`（`get_shared_session` 池化 Session）；全球可達來源（Yahoo）用 `prefer_direct` 直連優先、NAS 降級備援。
- [ ] 時間走 `tz_utils`（台灣 UTC+8），**不用裸 `datetime.now()`**。
- [ ] 路徑走 `paths.py`、環境變數走 `config.py`、漲跌幅/數字走 `numutil.py`。
- [ ] 新鮮度判定走 `freshness.py`（`stale_note` 給 UI 警語、`ensure_fresh` 給排程守門）。

## 容錯（fail-loud，不造假）
- [ ] 抓不到就明示降級/留空，**嚴禁假資料填充**（如 NAV：日期不符標「NAV 延遲」不計折溢價）。
- [ ] 具型別 except（**禁裸 `except:`**）+ stderr 日誌；副章節失敗不可拖垮主報告。
- [ ] 平行抓取（`ThreadPoolExecutor`）需**保序 + 去重**。
- [ ] 內建離線確定性 demo（沙箱無代理也能跑各分支驗邏輯）。
