# STATE.md — 專案戰情室

> 全球政經戰略每日看板:RSS 爬蟲抓真實外電 → Gemini 分析 → Streamlit 呈現。
> GitHub Actions 每日/每月排程產出 JSON;Streamlit Community Cloud 部署。

## 技術棧
- Python 3.11;`streamlit` / `google-genai`(官方 SDK)/ `pandas` / `requests` + stdlib(RSS)。見 `requirements.txt`。
- 部署:Streamlit Cloud(看板)+ GitHub Actions(排程)。

## 看板章節(`app.py`)
戰略報告 / 趨勢雷達 / 台股觀察 / 美股觀察 / 國際盤預警 / 全球人物追蹤 / 房市觀察 / 個股健診 / ETF持股反查 / ETF圖鑑。
前五章節:雙語抓新聞(zh/TW + en/US)、回溯約 6 個月、標的標示 首見/最近/提及次數。
國際盤預警:抓美股指數/KOSPI/美股期貨【真實漲跌幅】(Yahoo Finance,非 AI 估算),跌幅≤門檻(INTL_DROP_THRESHOLD 預設 -1.5%)標大跌;Gemini 只依新聞解讀利空原因+台股影響。時間差:美股=隔夜領先、KOSPI=同步連動、期貨=盤前即時。**美股/期貨大跌時自動 LINE 推播盤前預警**(ENABLE_INTL_ALERT_LINE,KOSPI 同步盤不觸發);前端亦有手動推送鈕。
個股健診:互動式即時查詢(不存檔),研究員報告風格(相關性/籌碼/題材/護城河含產業上中下游/估值/風險);依使用者授權放寬硬規則1,AI 補的數字標〔AI估算〕並附非即時免責。

## 關鍵檔案
- `update_data.py`:Gemini 全包(四維戰略分析+白話文、趨勢雷達、台/美股、人物追蹤、房市判讀);每日排程入口。
- `news_fetcher.py`:RSS/Atom 爬蟲(去重/時間排序)。
- `housing_fetcher.py`:房市新聞 + 內政部實價登錄各縣市每坪房價(走 NAS 代理)。
- `etf_fetcher.py` / `etf_profile_fetcher.py` / `etf_holdings.py` / `price_fetcher.py`:ETF 成分股/圖鑑/反查/台股收盤價(走 NAS 代理 MoneyDJ + 證交所)。
- `index_fetcher.py`:國際盤預警 — 美股指數/KOSPI/美股期貨真實漲跌幅(Yahoo Finance,代理優先直連降級)。
- `proxy_helper.py` / `github_store.py`:NAS 代理設定 + 看板一鍵存 GitHub。
- JSON 產物:`latest_{report,reports,trends,stocks,us_stocks,intl_alert,focus,housing}.json` + `data/<類>/<date>.json`;`house_prices*.json`、`etf_*.json`、`stock_prices.json`、`taiwan_counties.geo.json`。
- 排程:`.github/workflows/`(daily_update / update_etf / ci / proxy_check)。

## 環境變數(Secrets / Variables)
`GEMINI_API_KEY`(必,支援複數 key 容錯)、`PROXY_URL`(NAS 代理,含帳密)、`GITHUB_TOKEN`(選)、`LINE_CHANNEL_ACCESS_TOKEN`/`LINE_TO`(選);其餘開關/關鍵字見各 `fetch_*`。

## 硬規則(不可違反)
1. **真實優先**:`raw_news` 一律來自真實 RSS;餵 Gemini 時明確要求「只能依提供的新聞分析」,嚴禁虛構。
2. **合法抓取**:只用網站開放的 RSS/feed,嚴禁硬爬付費牆全文。
3. **房價只取實價登錄**:房價一律取內政部實價登錄官方批次資料,**嚴禁用 AI 猜**;Gemini 只判讀冷熱/政策。ETF 成分股同理,嚴禁 AI 猜。
4. **金鑰只走環境變數/Secrets**:嚴禁硬編碼或進版控;`PROXY_URL`、`.streamlit/secrets.toml` 不得進 git。
5. **Gemini 用官方 `google-genai`**;結構化輸出關 thinking、設 `max_output_tokens`,JSON 經清理+驗證,失敗以非零碼結束;趨勢/LINE/各副章節失敗不可拖垮主報告。
6. 所有產出為 AI/工具自動生成,僅供參考,非投資建議。

## 待辦 ⏳
- [ ] 全市場化:看板匯入全台 ETF 清單後重抓成分股 + 圖鑑並自動存檔。
- [ ] (選)repo Secrets 設 `PROXY_URL` 讓每月排程自動抓 ETF/股價/房價(NAS 需放行 Actions IP)。
