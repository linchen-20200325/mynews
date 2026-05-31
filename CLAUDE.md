# CLAUDE.md — 開發規範 (Core Protocol)

本檔是本專案最高級別的協作規範,供 AI 助手與人類開發者共同遵循。

## 專案目標

每日自動產生一份「全球政經戰略報告」並以 Streamlit 呈現。核心價值是
**基於真實外電的深度分析**,而非泛泛而談或虛構資料。

## 架構與分工(RSS 爬蟲 + Gemini 全包)

| 階段 | 元件 | SDK/函式庫 | 職責 |
|------|------|-----------|------|
| 抓新聞 | `news_fetcher.py` | 標準函式庫(urllib + xml.etree) | 從可信來源(Google News RSS + 官方 feed)抓真實外電,只取標題/來源/連結/摘要 |
| 分析 | Gemini `gemini-2.5-flash` | `google-genai` | 讀取抓到的新聞 → 四維度戰略分析 + `laymans_dictionary` → JSON |
| 趨勢雷達 | Gemini `gemini-2.5-flash` | `google-genai` | 讀取產業新聞 → 最熱門產業排名打分 |
| 房市觀察 | `housing_fetcher.py` + Gemini | stdlib + `requests` + `google-genai` | 抓房市新聞 → Gemini 判讀預售/成屋冷熱 + 打房政策 + 縣市標記;另透過 NAS 代理抓內政部實價登錄各縣市每坪房價(真實) |

> Gemini 用官方 `google-genai` SDK,**不要**用 OpenAI 相容層代換。
> 爬蟲只用新聞網站主動開放的 RSS/feed,**嚴禁**硬爬付費牆網站全文(違反服務條款/著作權)。
> 房價一律取內政部實價登錄官方批次資料,**嚴禁**用 AI 猜測房價;Gemini 只負責判讀冷熱/政策。

## 資料契約 (JSON Schema)

`latest_report.json` 與 `data/reports/<date>.json` 必須含以下頂層欄位:
`report_date`、`topic`、`raw_news`、`strategic_analysis`、`laymans_dictionary`,
另含 `dictionary_source`(目前固定為 `gemini`)。
`strategic_analysis` 必含 `geo_military` / `supply_chain` / `macro_economy` /
`blind_spots_and_kpi` 四欄。詳見 `update_data.py` 的 `validate_report()`。

趨勢雷達 `latest_trends.json` 與 `data/trends/<date>.json` 含 `report_date` 與
`trends` 陣列;每個 trend 含 `rank`、`industry`、`heat_score`(0~100)、
`signals`(funding/hiring/policy/technology)、`leading_indicators`、
`evidence_news`、`summary`。詳見 `validate_trends()`。

房市觀察 `latest_housing.json` 與 `data/housing/<date>.json` 含 `report_date`、
`overall_sentiment`(熱絡/持平/冷清)、`presale_market` / `resale_market`
(各含 `sentiment`、`note`)、`policy` 陣列(`title`/`impact`)、`regions` 陣列
(`county`/`sentiment`/`heat_score`/`note`,county 限 22 縣市官方名)、`evidence_news`、
`raw_news`。詳見 `validate_housing()`。
房價 `house_prices.json` 含 `as_of`、`season`、`unit`(萬元/坪)、`counties`,
每縣市含 `resale` / `presale`(各 `avg_ping_wan`/`median_ping_wan`/`count`/`samples`)。
歷年房價 `house_price_history.json` 含 `as_of`、`unit`、`years` 陣列、`counties`,
每縣市含 `resale` / `presale`(各為 `{西元年: 每坪均價}`),供單一縣市歷年折線圖;
另含 `seasons_included`(已納入季別)與內部 `_acc`(各年總和/筆數,供增量累加,
每月排程 `housing_fetcher.py history` 只補最新季)。
縣市地圖底圖為 `taiwan_counties.geo.json`(內建、已正名臺/桃園市,`properties.name` 對應縣市)。
交通分類由 `housing_fetcher.transport_tag()` 判定(高鐵站 `HSR_COUNTIES`、自強號
`TRA_TZECHIANG_COUNTIES`),供地圖★標記與長條圖額外標出交通便利縣市。房價只取實價登錄,**嚴禁 AI 猜**。

## 開發守則

1. **真實優先**:`raw_news` 一律來自 `news_fetcher` 抓到的真實 RSS 報導,嚴禁虛構;
   餵給 Gemini 時要明確要求「只能根據提供的新聞分析」。
2. **合法抓取**:只用新聞網站主動開放的 RSS/feed,嚴禁硬爬付費牆網站全文。
3. **JSON 穩健**:模型輸出都要經過 `clean_json_text()` 清理 + `json.loads()` +
   結構驗證;解析失敗一律以非零碼結束讓 CI 標紅。
4. **失敗隔離**:趨勢雷達或 LINE 推播失敗時,不可讓整份戰略報告失敗。
5. **金鑰只走環境變數**:`GEMINI_API_KEY` /
   `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO`,嚴禁硬編碼或進版控。
   (LINE 推播用 Messaging API push;LINE Notify 已停用,勿再採用。)
6. **快取友善**:大型 system prompt 維持穩定;每次變動的內容(日期、主題、新聞)放 user 訊息。
7. **改動需驗證**:Python 改動後至少 `python -m py_compile` 通過再提交。

## 常用指令

```bash
pip install -r requirements.txt
python -m py_compile update_data.py app.py news_fetcher.py housing_fetcher.py   # 語法檢查
python update_data.py                         # 產生報告(需金鑰)
streamlit run app.py                          # 啟動看板
```

## 分支與提交

- 功能開發在指定的 feature 分支,提交訊息清楚描述變更。
- 自動化(GitHub Actions)以 `github-actions[bot]` 身分 commit 更新後的 JSON。
