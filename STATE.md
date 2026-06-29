# STATE.md — 專案戰情室

> 全球政經戰略每日看板:RSS 爬蟲抓真實外電 → Gemini 分析 → Streamlit 呈現。
> GitHub Actions 每日/每月排程產出 JSON;Streamlit Community Cloud 部署。

## 技術棧
- Python 3.11;`streamlit` / `google-genai`(官方 SDK)/ `pandas` / `requests` + stdlib(RSS)。見 `requirements.txt`。
- 部署:Streamlit Cloud(看板)+ GitHub Actions(排程)。

## 個股盯盤(第二個 LINE bot)
自選台股/ETF 每早推「消息面 AI 總結 + 新月營收」給指定對象。清單 `watchlist.json` 由 `scripts/nas_line_bot.py`(NAS 常駐 webhook,接「加/刪/清單」指令經 GitHub API 寫回 repo)維護;排程端 `update_data.py` 的 `run_watch_section()` 讀清單→逐檔抓真實新聞(`news_fetcher`)+ Gemini 一次總結 + `earnings_fetcher` 抓 TWSE OpenAPI 月營收(真實財報訊號,`watch_revenue_pushed.json` dedup 只推新公告)→ 第二個 bot(`LINE_WATCH_TOKEN`/`LINE_WATCH_TO`)push。未設第二 bot → `watch_enabled()` 為偽,整段靜默略過。

## 看板導覽(`app.py`)— 4 大整合頁(2026-06 改版)
側邊欄收斂為 **📊 台股 / 🇺🇸 美股 / 🌍 全球 / 🏠 台灣房市**;點一個領域,該領域所有面板「一次展開」在同一頁,最上方有 **AI 今日總結**(`render_market_digest` → `update_data.get_market_digest`:把該領域當日各面板數據融成統一研判,按鈕觸發、依日期 session 快取)。
- **台股**:國際盤預警(含共振)+ 法人籌碼 + 台股觀察 + 互動工具 expander(個股健診 / 新聞策略 / ETF工作台)。
- **美股**:美股觀察 + 個股健診(美股也能查)。
- **全球**:戰略報告 + 趨勢雷達 + 全球人物追蹤。
- **台灣房市**:各縣市房價面板 + 房市觀察。
- 唯讀面板直接攤開(`sec_*`),即時重抓收進 expander;互動工具為 `tool_*`。底層 render_* 函數全沿用,僅重組導覽。
前述各區:雙語抓新聞(zh/TW + en/US)、回溯約 6 個月、標的標示 首見/最近/提及次數。
國際盤預警:抓美股指數/KOSPI/美股期貨【真實漲跌幅】(Yahoo Finance,非 AI 估算),跌幅≤門檻(INTL_DROP_THRESHOLD 預設 -1.5%)標大跌;Gemini 只依新聞解讀利空原因+台股影響+美股看法(us_view)。時間差:美股=隔夜領先、KOSPI=同步連動、期貨=盤前即時。**每天都推一則 LINE**(平靜→🌅快報、大跌→🚨預警,標題自動切換),含美股/台股看法;前端亦有手動推送鈕。
個股健診:互動式即時查詢(不存檔),研究員報告風格(相關性/籌碼/題材/護城河含產業上中下游/估值/風險);依使用者授權放寬硬規則1,AI 補的數字標〔AI估算〕並附非即時免責。
新聞策略:互動式(貼新聞文本、不存檔),首席策略師風格四階段(因果鏈→台股供應鏈三大陣營→台股 ETF 進攻/防守佈局→持有週期與出場訊號)。prompt+函數 `NEWS_ETF_STRATEGY_SYSTEM_PROMPT`/`get_news_etf_strategy` 在 `update_data.py`,UI 在 `app.py`;嚴禁亂編 ETF 代號(不確定留空)、附非即時與非投資建議免責。

## 架構約定(SSOT — 同類事實只定義一次)
- `paths.py`:所有資料檔/封存目錄、ETF 三檔路徑的**唯一**定義(各檔 import,勿再貼字面值)。
- `tz_utils.py`:台灣 UTC+8 時間(`taiwan_now/today`);凡「台灣今日」一律走它。例外:`scripts/nas_trigger.py`刻意零相依、自帶。
- `etf_data.py`:ETF 成分股/反查/圖鑑的快取(`@st.cache_data`)單一入口;app.py 一律向它要資料。
- `numutil.py`:漲跌幅 `pct_change()`、`parse_number()`、`OKU` 億元係數的**唯一**來源;嚴禁在其他模組重複定義。
- `freshness.py`:資料新鮮度(staleness)判定的**唯一**入口。`stale_note()` 給 UI 警語、`ensure_fresh()` 給排程守門(過期 raise)。門檻屬領域決策,以具名常數帶入:籌碼 `CHIP_STALE_DAYS=5`、房價 `HOUSE_STALE_DAYS=40`、股價/報告 `PRICE_STALE_DAYS`/`STALE_REPORT_DAYS=5/2`(皆可環境變數覆寫)。
- `config.py`:環境變數解析(`env_bool/int/float`)與 11 個功能開關的**唯一**入口;嚴禁在 `update_data.py` 散落 `os.environ.get`。
- `gemini_client.py`:Gemini API 呼叫、JSON 清洗、字典正規化的**唯一**入口;嚴禁直接 `import google.generativeai`。
- `line_notify.py`:LINE Messaging API 推播(`broadcast/multicast/push` 自動路由)的**唯一**入口;嚴禁直接 urllib/requests 推 LINE。
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

## Phase-2 SSOT 整合（2026-06-28 結案，PR #73 已併入 main）
- ✅ `validate_*()` 泛化：`_validate_structure()` helper，8 個 wrapper 各縮為 1 行（−37 行）
- ✅ `update_data.main()` 拆分：291→48 行，10 個 `_run_*` helper
- ✅ `app.py::render_stock_query()` 拆分：164→7 行，6 個 `_render_stock_query_*` helper
- ✅ `ARCHITECTURE.md` 更新至 v2.0：SSOT 表 11 條，技術債 8 項全結案

## Hotfix（2026-06-28，PR #75 已併入 main）
- ✅ `app.py::ensure_gemini_key()` AttributeError 修復：補 `import gemini_client`，兩處錯誤呼叫 `update_data.get_gemini_keys()` → `gemini_client.get_gemini_keys()`（SSOT 正名）。

## 國際盤快報 UX 大改版（2026-06-29，PR #77 已併入 main）
- ✅ `root_cause` 欄位：Gemini Prompt 強制萃取今日最大觸發事件（15字內，地緣政治優先），LINE 訊息第二行顯示 `🔥 主因:XXX`
- ✅ 地緣政治新聞源：`DEFAULT_INTL_ALERT_QUERIES` 加入戰爭/伊朗/關稅/台海關鍵字；zh_feeds 補「中央社 國際」「BBC 中文」；en_feeds 加 WORLD 版塊
- ✅ LINE 訊息精簡：reason 壓 100 字、focus ≤2 條、sectors ≤3 個、interpretation ≤2 條

## 總統任期週期季節性圖表（2026-06-29，PR #80）
- ✅ `season_chart.py`（SSOT）新建：內嵌 S&P 500 1949-2024 月底累積報酬率；四條分析線（全年/第六年/共和黨第六年/期中選舉年）；`fetch_sp500_2026()` 透過 `proxy_helper.fetch_json()` 抓 Yahoo Finance v8 取得 2026 實際走勢；`build_cycle_figure()` 回傳 matplotlib Figure
- ✅ `app.py` 整合：`import season_chart`、`_fetch_2026_cached(ttl=3600)`、`_tool_cycle_chart()`；`page_tw()` 新增第三個 expander「📅 總統任期週期 — 2026 走勢預測參考」

## 總統任期週期診斷資料 Tab（2026-06-29，PR #81 #82 已併入 main）
- ✅ PR #81：`requirements.txt` 補 `matplotlib>=3.7`（修復 Streamlit Cloud ModuleNotFoundError）
- ✅ PR #82：`season_chart.py` 新增 `get_cycle_data()` 公開函數（SSOT，月均報酬率原始數值）；`app.py` `_tool_cycle_chart()` 改為雙 tab：📊 圖表 / 📋 診斷資料（資料來自 SSOT，不重複計算）

## 待辦 ⏳
- [x] 全市場化 ETF **程式已完成**:看板「🌐 一鍵匯入全市場 ETF」(`etf_fetcher.import_all_etfs`)→ 重抓成分股/圖鑑(`etf_fetcher.crawl` / `etf_profile_fetcher.crawl`)→ 自動存 GitHub 全接妥(`app.py` 443-455 / 404 / 546)。**待帶真實 `PROXY_URL` 在看板按一次**即生效(沙箱無代理,無法代跑)。
- [x] repo Secrets `PROXY_URL` 早已設妥，排程(ETF/股價/房價)持續正常運作。
- [x] 個股盯盤(第二個 LINE bot)**已上線驗收通過(2026-06-28)**:傳「加 2330」bot 正確回「已加入 2330」並顯示 watchlist 4 檔(6770/6239/3231/2330);NAS `nas_line_bot.py` webhook 對外可達,Secrets 全設妥,watchlist.json 寫回 GitHub 正常。
- [x] **上櫃月營收已實作**(`earnings_fetcher._fetch_otc_bulk`):MOPS `ajax_t05st10_q` POST 一次全抓,`fetch_monthly_revenue()` 透明合併上市(TWSE) + 上櫃(MOPS),呼叫端零改動;需 proxy 過境 MOPS。
- [x] **季報 EPS 已實作(2026-06-28)**:`fetch_quarterly_eps()` 向 MOPS `ajax_t163sb04` 逐檔 POST,sii/otc 自動辨識;`_push_watch_for` 加 EPS dedup 區塊;LINE 訊息新增「📊 新季報(EPS)」段落。需 proxy + 實機驗收(MOPS 境外限速)。
- 註:§5 向量化已實查結案 — 全庫零 `numpy`/`.iterrows()`,既有 pandas(melt/dropna/line_chart)皆已向量化,其餘為小型巢狀 dict 迴圈(縣市×市場×年),改 pandas 反增風險無效益,**刻意保留**。
