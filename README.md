# 🌐 全球政經戰略每日看板 (mynews)

每天早上自動執行的情報管線:先用 **RSS 爬蟲從具公信力的新聞來源抓真實外電**,
再交給 **Gemini 讀取做四維度地緣政治 × 全球宏觀戰略分析 + 白話文字典**,輸出結構化
JSON,並由 Streamlit 前端呈現(含歷史報告瀏覽)。

## 架構

```
GitHub Actions (每日定時)
        │
        ▼
update_data.py
   ├─ news_fetcher.py (RSS 爬蟲):從可信來源抓真實外電(標題/來源/連結/摘要)
   │      Google News RSS（繁中/台灣,聚合中央社/聯合報/自由/中時/BBC中文/DW…)+ 中文官方 feed
   │
   ├─ [A] 戰略報告
   │    └─ Gemini (gemini-2.5-flash):讀取抓到的新聞 → 四維度分析 + 白話文字典
   │       (raw_news 直接採用爬蟲抓到的真實新聞,絕不虛構)
   ├─ [B] 趨勢雷達 (可關)
   │    Gemini:讀取產業新聞 → 最熱門產業,依資金/徵才/政策/技術排名打分
   └─ [C] 台股觀察 (可關)
        Gemini:讀取台灣財經新聞 → 被提最多次的台股標的(利多/利空/觀望)+ 趨勢/夕陽產業
        │
        ▼
latest_report.json / latest_trends.json / latest_stocks.json   ──►  app.py (Streamlit) 呈現
data/reports|trends|stocks/<date>.json   歷史存檔(側邊欄可瀏覽)
(可選) LINE 推播報告 + 熱門產業 Top3 摘要
```

## 檔案

| 檔案 | 用途 |
|------|------|
| `news_fetcher.py` | RSS 新聞爬蟲(純標準函式庫),從可信來源抓真實外電 |
| `update_data.py` | 核心:餵新聞給 Gemini 做分析 + 白話文 + 趨勢雷達,輸出 JSON |
| `app.py` | Streamlit 前端,含歷史報告選擇器 |
| `.github/workflows/daily_update.yml` | 每日定時執行 + 自動 commit/push |
| `requirements.txt` | 相依套件 |
| `CLAUDE.md` | 開發規範(供 AI 與人類協作參考) |
| `STATE.md` | 專案當前狀態、待辦與已知問題 |

## 設定步驟

GitHub repo → Settings → Secrets and variables → Actions

**Secrets(必/選):**
- `GEMINI_API_KEY`(必填)— Google Gemini API 金鑰
- `LINE_CHANNEL_ACCESS_TOKEN` + `LINE_TO`(選填)— 兩者皆設定才會推播報告摘要到 LINE

> **LINE 推播**:LINE Notify 已於 2025 停用,本專案改用 **LINE Messaging API**。
> 需到 [LINE Developers](https://developers.line.biz/) 建立 Messaging API channel,
> 取得 **Channel access token**(填 `LINE_CHANNEL_ACCESS_TOKEN`),並取得要推播的
> 對象 ID(自己的 userId、或加入機器人的群組 groupId,填 `LINE_TO`)。

**Variables(皆選填):**
- `GEMINI_MODEL` — 覆寫 Gemini 模型(預設 `gemini-2.5-flash`)
- `REPORT_TOPIC` — 自訂戰略報告分析主題
- `NEWS_QUERIES` / `TREND_QUERIES` / `STOCK_QUERIES` — 自訂聚焦關鍵字(以 `;` 分隔)
- `ENABLE_TREND_RADAR` / `ENABLE_STOCK_PICKER` — 設為 `0` 可分別關閉趨勢雷達 / 台股觀察
- `NEWS_TOPICS` / `TREND_TOPICS` — Google News 動態分類頭條(以 `,` 分隔,預設 `WORLD,BUSINESS` / `BUSINESS,TECHNOLOGY`;可選 `WORLD`/`BUSINESS`/`TECHNOLOGY`/`NATION`/`SCIENCE`)
- `NEWS_LANG` / `NEWS_REGION` — Google News 語系/地區(預設 `zh` / `TW`,即繁中台灣;想抓英文外電可設 `en` / `US`)
- `NEWS_MAX` / `NEWS_SINCE_HOURS` — 抓新聞則數上限 / 回溯時數(預設 `12` / `48`)

設定完成後可在 Actions 分頁手動 **Run workflow** 測試。

## 本地執行

```bash
pip install -r requirements.txt
export GEMINI_API_KEY="..."
python update_data.py            # 爬新聞 → Gemini 分析 → 產生 latest_report.json
streamlit run app.py             # 啟動看板
```

## 設計重點

- **真實優先**:`raw_news` 直接來自 RSS 爬蟲抓到的真實報導,結構上不可能虛構;
  Gemini 被要求只能根據提供的新聞做分析。
- **合法抓取**:只用新聞網站主動開放的 RSS/feed,不硬爬付費牆網站全文,
  避免違反服務條款與著作權;只保留摘要並連回原文。
- **單模型(Gemini 全包)**:讀新聞、四維度分析、白話文、趨勢雷達都由 Gemini 完成,
  使用官方 `google-genai` SDK。
- **穩健 JSON**:解析會去除 markdown 圍欄、擷取大括號/中括號範圍、`json.loads` 後做結構驗證;
  失敗一律以非零碼結束讓 CI 標紅。

> ⚠️ 本專案產出內容由 AI 自動生成,僅供參考,非投資建議。
