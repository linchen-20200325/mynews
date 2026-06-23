# STATE.md — 專案戰情室

> 全球政經戰略每日看板:RSS 爬蟲抓真實外電 → Gemini 分析 → Streamlit 呈現。
> GitHub Actions 每日/每月排程產出 JSON;Streamlit Community Cloud 部署。

## 技術棧
- Python 3.11;`streamlit` / `google-genai`(官方 SDK)/ `pandas` / `requests` + stdlib(RSS)。見 `requirements.txt`。
- 部署:Streamlit Cloud(看板)+ GitHub Actions(排程)。

## 個股盯盤(第二個 LINE bot)
自選台股/ETF 每早推「消息面 AI 總結 + 新月營收」給指定對象。清單 `watchlist.json` 由 `scripts/nas_line_bot.py`(NAS 常駐 webhook,接「加/刪/清單」指令經 GitHub API 寫回 repo)維護;排程端 `update_data.py` 的 `run_watch_section()` 讀清單→逐檔抓真實新聞(`news_fetcher`)+ Gemini 一次總結 + `earnings_fetcher` 抓 TWSE OpenAPI 月營收(真實財報訊號,`watch_revenue_pushed.json` dedup 只推新公告)→ 第二個 bot(`LINE_WATCH_TOKEN`/`LINE_WATCH_TO`)push。未設第二 bot → `watch_enabled()` 為偽,整段靜默略過。

## 看板章節(`app.py`)
戰略報告 / 趨勢雷達 / 台股觀察 / 美股觀察 / 國際盤預警 / 全球人物追蹤 / 房市觀察 / 個股健診 / ETF工作台(持股反查 + 圖鑑,`st.tabs` 共用 `etf_data` 快取)。
前五章節:雙語抓新聞(zh/TW + en/US)、回溯約 6 個月、標的標示 首見/最近/提及次數。
國際盤預警:抓美股指數/KOSPI/美股期貨【真實漲跌幅】(Yahoo Finance,非 AI 估算),跌幅≤門檻(INTL_DROP_THRESHOLD 預設 -1.5%)標大跌;Gemini 只依新聞解讀利空原因+台股影響+美股看法(us_view)。時間差:美股=隔夜領先、KOSPI=同步連動、期貨=盤前即時。**每天都推一則 LINE**(平靜→🌅快報、大跌→🚨預警,標題自動切換),含美股/台股看法;前端亦有手動推送鈕。
個股健診:互動式即時查詢(不存檔),研究員報告風格(相關性/籌碼/題材/護城河含產業上中下游/估值/風險);依使用者授權放寬硬規則1,AI 補的數字標〔AI估算〕並附非即時免責。

## 架構約定(SSOT — 同類事實只定義一次)
- `paths.py`:所有資料檔/封存目錄、ETF 三檔路徑的**唯一**定義(各檔 import,勿再貼字面值)。
- `tz_utils.py`:台灣 UTC+8 時間(`taiwan_now/today`);凡「台灣今日」一律走它。例外:`scripts/nas_trigger.py` 刻意零相依、自帶。
- `etf_data.py`:ETF 成分股/反查/圖鑑的快取(`@st.cache_data`)單一入口;app.py 一律向它要資料。
- `numutil.py`:漲跌幅 `pct_change()`(內建 `prev>0` 與方向對帳不變量)的**唯一**公式;凡算 % 一律走它。
- `freshness.py`:資料新鮮度(staleness)判定的**唯一**入口。`stale_note()` 給 UI 警語、`ensure_fresh()` 給排程守門(過期 raise)。門檻屬領域決策,以具名常數帶入:籌碼 `CHIP_STALE_DAYS=5`、房價 `HOUSE_STALE_DAYS=40`、股價/報告 `PRICE_STALE_DAYS`/`STALE_REPORT_DAYS=5/2`(皆可環境變數覆寫)。
- `watchlist.py`:個股盯盤清單(`watchlist.json`)的**唯一**入口。純邏輯(`parse_command/add_stock/remove_stock/format_list/normalize_ticker`)與 I/O(`load/save/dumps`)分離,排程端(本機檔)與 `scripts/nas_line_bot.py`(GitHub API)共用同一套加/刪/解析規則,杜絕兩端漂移。

## 關鍵檔案
- `update_data.py`:Gemini 全包(四維戰略分析+白話文、趨勢雷達、台/美股、人物追蹤、房市判讀);每日排程入口。內建資料齊備守門(台灣 05:30 前的 schedule 略過,擋 GitHub 半夜亂觸發)。
- `scripts/nas_trigger.py`(每日 06:00 主力,`workflow_dispatch` 直發)/ `scripts/force_send.sh`(手動強制補發,`TRIGGER_MODE=always`);GitHub schedule 退為 06:40/07:30 兜底。LINE 群發:`LINE_TO=broadcast` 發全體好友。
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
- [x] 全市場化 ETF **程式已完成**:看板「🌐 一鍵匯入全市場 ETF」(`etf_fetcher.import_all_etfs`)→ 重抓成分股/圖鑑(`etf_fetcher.crawl` / `etf_profile_fetcher.crawl`)→ 自動存 GitHub 全接妥(`app.py` 443-455 / 404 / 546)。**待帶真實 `PROXY_URL` 在看板按一次**即生效(沙箱無代理,無法代跑)。
- [ ] (選,**唯一剩餘阻塞、屬你的帳號操作**)repo Secrets 設 `PROXY_URL`:設妥後上面全市場化與每月排程自動抓 ETF/股價/房價即可自動跑(NAS 需放行 Actions IP)。
- [x] 個股盯盤(第二個 LINE bot)**程式已完成**:`watchlist.py` + `earnings_fetcher.py` + `update_data.run_watch_section()` + `scripts/nas_line_bot.py`(NAS webhook)+ workflow 接線。**待上線:** ① repo Secrets 設 `LINE_WATCH_TOKEN`/`LINE_WATCH_TO`(第二個 Messaging API channel);② NAS 跑 `nas_line_bot.py` 並把 LINE Webhook URL 指到 NAS(需 `LINE_WATCH_SECRET` + 具 contents:write 的 `GITHUB_TOKEN`,對外經 Cloudflare Tunnel/路由器轉發)。沙箱無網路/無第二 bot,無法代跑驗收。
- [ ] (擴充)個股盯盤財報目前只涵蓋**上市月營收**(TWSE OpenAPI `t187ap05_L`);上櫃月營收與季報 EPS/法說屬後續(MOPS 無正式 API,需 proxy + 表單解析)。
- 註:§5 向量化已實查結案 — 全庫零 `numpy`/`.iterrows()`,既有 pandas(melt/dropna/line_chart)皆已向量化,其餘為小型巢狀 dict 迴圈(縣市×市場×年),改 pandas 反增風險無效益,**刻意保留**。
