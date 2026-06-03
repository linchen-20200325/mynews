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
`us_stocks` / `tw_stocks`(各 `{name,ticker}`,趨勢雷達同時含美股與台股代表股)、
`evidence_news`、`summary`,另含真實新聞統計 `news_count`/`first_seen`/`last_seen`。
詳見 `validate_trends()`。

> **雙語抓取**:前五個章節(戰略報告/趨勢雷達/台股/美股/全球人物追蹤)一律
> **同時抓中文(zh/TW)+ 英文(en/US)新聞並合併去重**(`fetch_bilingual_news()`),
> 確保不論報導是中文台媒或英文國際原文都不漏;呈現語言由各章節 prompt 決定(多為繁中)。
> 房市觀察為台灣在地題材(實價登錄 + 國內房市),維持中文抓取。

前五個章節(戰略報告/趨勢雷達/台股/美股/全球人物追蹤)新聞回溯約 6 個月
(`SIX_MONTHS_HOURS`,實際可回溯範圍受 Google News RSS 限制),並由真實新聞統計每個
標的的 `news_count`(說過幾次)/`first_seen`/`last_seen`(首見/最近見報);戰略報告與
人物追蹤另在頂層帶 `news_span`(或同名欄位)標示整批新聞跨度。時間/次數一律由
`mention_window()` / `news_span()` 從真實新聞算出,**不交給 Gemini 臆測**。

房市觀察 `latest_housing.json` 與 `data/housing/<date>.json` 含 `report_date`、
`overall_sentiment`(熱絡/持平/冷清)、`presale_market` / `resale_market`
(各含 `sentiment`、`note`)、`policy` 陣列(`title`/`impact`)、`regions` 陣列
(`county`/`sentiment`/`heat_score`/`note`,county 限 22 縣市官方名)、`evidence_news`、
`raw_news`,另含 `ai_summary`(買方視角綜合總結物件:`future_trend`/`policy_shift`/
`buyer_impact`(偏好|中性|偏壞)/`buyer_advice`/`regulations`(法規陣列)/`overview`;
舊資料可能為單句字串,看板需向後相容)。詳見 `validate_housing()`。
房市 AI 總結會一併讀入當期房價 + 歷年趨勢(`house_price_history.json`)綜合判讀。
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

## 環境變數 / 設定對照表

> 機密(金鑰、含帳密的 URL)放 **Secrets**;一般開關/參數放 **Variables**(GitHub repo
> → Settings → Secrets and variables → Actions)。Streamlit Cloud 一律放 App → Settings →
> Secrets(TOML)。`PROXY_URL` 含帳密,**只走 Secrets,嚴禁進版控**。

| 變數 | 必填 | 類別 | 預設 | 用途 |
|------|------|------|------|------|
| `GEMINI_API_KEY` | ✅ | Secret | — | Gemini 金鑰;支援逗號/分號/換行分隔多把容錯 |
| `GEMINI_API_KEYS` | — | Secret | — | 多把金鑰的另一寫法(同上分隔) |
| `GEMINI_MODEL` | — | Variable | `gemini-2.5-flash` | 覆寫 Gemini 模型 |
| `GEMINI_MAX_TOKENS` | — | Variable | `8192` | 單次輸出 token 上限(已關 thinking) |
| `PROXY_URL` | — | Secret | — | NAS 代理(ETF/股價/房價走 MoneyDJ、實價登錄);格式 `http://帳:密@host:3128` |
| `GITHUB_TOKEN` | — | Secret | — | 看板「💾 直接存到 GitHub」;fine-grained PAT 限本 repo、Contents 讀寫 |
| `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO` | — | Secret | — | 兩者皆設才推播(Messaging API push) |
| `REPORT_TOPIC` | — | Variable | 內建預設主題 | 戰略報告單一主題 |
| `REPORT_TOPICS` | — | Variable | — | 多主題戰略報告,以 `;` 分隔(第一個為主報告) |
| `ENABLE_TREND_RADAR` | — | Variable | `1` | 設 `0/false/no` 關閉趨勢雷達 |
| `ENABLE_STOCK_PICKER` | — | Variable | `1` | 設 `0/false/no` 關閉台股觀察 |
| `ENABLE_US_STOCK_PICKER` | — | Variable | `1` | 設 `0/false/no` 關閉美股觀察 |
| `ENABLE_FOCUS` | — | Variable | `1` | 設 `0/false/no` 關閉全球人物追蹤每日排程 |
| `ENABLE_HOUSING` | — | Variable | `1` | 設 `0/false/no` 關閉房市觀察 |
| `NEWS_QUERIES` / `TREND_QUERIES` / `STOCK_QUERIES` / `US_STOCK_QUERIES` | — | Variable | 內建 | 各頁抓新聞關鍵字,以 `;` 分隔 |
| `US_TREND_QUERIES` | — | Variable | 內建(英文) | 趨勢雷達美股面向的英文關鍵字,以 `;` 分隔 |
| `NEWS_QUERIES_EN` / `STOCK_QUERIES_EN` | — | Variable | 內建(英文) | 戰略報告/台股的英文側關鍵字(雙語抓取),以 `;` 分隔 |
| `US_STOCK_QUERIES_ZH` | — | Variable | 內建(中文) | 美股的中文側關鍵字(雙語抓取),以 `;` 分隔 |
| `NEWS_TOPICS` / `TREND_TOPICS` | — | Variable | `WORLD,BUSINESS` 等 | Google News 動態分類頭條,以 `,` 分隔 |
| `NEWS_LANG` / `NEWS_REGION` | — | Variable | `zh` / `TW` | Google News 語系/地區 |
| `US_NEWS_LANG` / `US_NEWS_REGION` | — | Variable | `en` / `US` | 美股觀察抓英文原文新聞的語系/地區(輸出仍由 Gemini 翻成中文) |
| `FOCUS_TOPICS` | — | Variable | `川普;黃仁勳` | 全球人物追蹤每日排程追蹤對象(中文),以 `;` 分隔 |
| `FOCUS_MAX` / `FOCUS_SINCE_HOURS` | — | Variable | `30` / `4392` | 全球人物追蹤抓新聞則數 / 回溯時數(~6 個月) |
| `NEWS_MAX` / `NEWS_SINCE_HOURS` | — | Variable | `25` / `4392` | 戰略報告+趨勢抓新聞則數上限 / 回溯時數(~6 個月) |
| `STOCK_MAX` / `STOCK_SINCE_HOURS` | — | Variable | `40` / `4392` | 台股觀察抓新聞則數 / 回溯時數(~6 個月) |
| `US_STOCK_MAX` / `US_STOCK_SINCE_HOURS` | — | Variable | `40` / `4392` | 美股觀察抓新聞則數 / 回溯時數(~6 個月) |
| `HOUSING_MAX` / `HOUSING_SINCE_HOURS` | — | Variable | `18` / `72` | 房市抓新聞則數 / 回溯時數 |

## 分支與提交

- 功能開發在指定的 feature 分支,提交訊息清楚描述變更。
- 自動化(GitHub Actions)以 `github-actions[bot]` 身分 commit 更新後的 JSON。

### §4 PR 規範與「跳 PR 直推」例外

原則:**所有變更一律走 PR**(保留 CI gate 與變更紀錄)。唯下列「不影響功能行為」
的純維護性改動,可直接推 `main`(可用 `scripts/quick_merge.sh`):

1. `STATE.md` / `CLAUDE.md` / 程式註解 / typo 修正。
2. 版本字串 bump(僅版本號字串,**不含**任何程式邏輯變動)。
3. 不影響功能行為的純文件改動(README、文件、設定說明等)。

> 其他任何牽涉程式邏輯、資料契約、爬蟲/分析行為的變更,一律走 PR,不得直推。
> 判斷準則:**若改動可能改變執行結果或行為,就走 PR**;有疑慮一律走 PR。
