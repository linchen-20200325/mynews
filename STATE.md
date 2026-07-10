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

## Phase 2 中央決策大腦（2026-06-29，PR #83 已併入 main）
- ✅ `feature_aligner.py`（新 SSOT）：四路特徵對齊合流（macro/chip/news/tech），各路獨立容錯
- ✅ `paths.py`：新增 `LATEST_DECISION` / `ARCHIVE_DECISION`
- ✅ `update_data.py`：`MASTER_DECISION_SYSTEM_PROMPT` + `get_master_decision()` + `_run_master_decision()`
- 待辦：Phase 3 Streamlit 中央決策儀表板（四象限視覺化 + action_signal 燈號）

## Phase 3 中央決策儀表板（2026-06-29，PR #84 已併入 main）
- ✅ `update_data.main()` 接入 `_run_master_decision(today)`：每日 06:00 自動產生 `latest_decision.json`（含 features 嵌入）
- ✅ `app.py` 新增 `page_ai_brain()`：操作訊號燈號/信心分數/四路權重長條圖/核心驅動/風險提示/四象限特徵明細
- ✅ 側邊欄新增「🧠 AI 決策大腦」第六個頁面入口

## 重構排毒計畫（2026-06-30 進行中）

### PR #85 — news_analyzer SSOT + Gemini 穩定性（已併入 main）
- ✅ 新建 `news_analyzer.py`（CalcEngine SSOT）：集中 5 個重複新聞分析函數 + `BULL_WORDS`/`BEAR_WORDS` 情感常數
  - `extract_news_date` / `expand_match_keys` / `matches_news_keywords` / `count_keyword_mentions` / `summarize_news_span` / `score_headline_sentiment`
- ✅ `update_data.py`：移除 5 函數，9 個呼叫點改用 `news_analyzer.*`
- ✅ `feature_aligner.py`：移除私有情感常數與 `_sentiment_score`，改用 `news_analyzer.score_headline_sentiment()`
- ✅ `gemini_client.py`：`max_attempts` 4→8，退避上限 60s→120s，基礎倍率 5s→15s（防 06:00 尖峰 503 造成雙推）
- 待辦：GitHub Variables 手動新增 `GEMINI_RETRIES=8`（保險用）

### PR #87 — P1-A System Prompts 外移 prompts/*.yaml（已併入 main）
- ✅ 新建 `prompt_loader.py`（SSOT）：`functools.lru_cache` 讀取 `prompts/*.yaml`，唯一 `load(name)` 入口
- ✅ 新建 `prompts/` 目錄含 14 個 YAML 檔（analysis/trend/stock/us_stock/intl_alert/focus_translate/stock_query_translate/focus/stock_query/news_etf_strategy/housing/master_decision/market_digest/watch）
- ✅ `update_data.py`：14 個 `*_SYSTEM_PROMPT` 常數改為 `prompt_loader.load("name")`，縮減 648 行（2461→1813）
- ✅ `requirements.txt` 新增 `pyyaml>=6.0`

### PR #88 — P1-B app.py 拆分 pages/（已併入 main）
- ✅ `app.py` 縮減至 50 行純路由（原 3076 行）
- ✅ 新建 `app_core.py`（495 行）：共用常數、路徑別名、`render_*` 函式的 SSOT
- ✅ 新建 `pages/` 目錄含 6 個領域模組：tw(810)/us(116)/global_(450)/housing(593)/ai_brain(153)/etf(509)
- ✅ 架構：`app.py → pages/*.py → app_core.py`（零循環匯入）

### PR #89 — P1-B SSOT 稽核修補（已併入 main）
- ✅ `app_core.py`：移除殘留雙重 docstring（Python SyntaxError）
- ✅ `pages/tw.py`：補回 `ALERT_BADGE` 常數（原 `app.py:962`）；移除 dead `import paths`
- ✅ `pages/us.py`：補 `load_json` 匯入；補 `from pages.tw import tool_stock_query`
- ✅ `pages/global_.py`：補 `import freshness` + `STALE_REPORT_DAYS`；移除 dead `import paths`
- ✅ `pages/etf.py` / `pages/housing.py`：移除 dead `import paths`
- 稽核結果：零循環匯入 / 零 raw path 字面值 / 零裸 datetime / 零 google.generativeai 直接 import

### PR #91 — P2-A prompt_builder.py SSOT（已併入 main）
- ✅ 新建 `prompt_builder.py`（235 行）：4 個 `format_*` helper + 10 個 `build_*_user_prompt` builder
- ✅ `update_data.py`：移除 14 個函數定義，改 `from prompt_builder import` 取用；1813→1608 行（−205 行）
- ✅ 架構：`update_data.py → prompt_builder.py → index_fetcher`（零循環匯入）

### PR #93 — P2-B line_notify.py DRY 截斷邏輯（已併入 main）
- ✅ `line_notify.py`：新增 `_clip(text, limit)` + `_finalize(msg)` 兩個私有 helper
- ✅ 消除 5 個 builder 內 9 處 inline 截斷重複；`build_confluence_line_message` 同步修正遺漏截斷提示 bug

### PR #94 — P3-B query_config.json 關鍵字外移（已併入 main）
- ✅ 新建 `query_config.json`：14 組查詢清單集中管理，可直接在 GitHub UI 修改無需重新部署
- ✅ `update_data.py`：移除 14 個硬編碼清單，改由 `_QUERY_CONFIG` 讀取；env var 覆寫機制不變

### PR #96 — P4-A prompt_builder.py 模板化（已併入 main）
- ✅ 新增 `_compose(today, instruction, news)` 私有 helper：擷取 5 個 builder 共用的「日期 header + 指令 + report_date + news footer」結構
- ✅ `build_trend/stock/us_stock/focus/stock_query_user_prompt` 改用 `_compose`：+37/−41 行
- ✅ 另外 5 個 builder（結構差異大）保持原狀，刻意不勉強套模板

### PR #97 — P4-B dead code 清除（已併入 main）
- ✅ `update_data.py`：補 `import prompt_loader`（修復 NameError）、移除 `import re` + bare `import prompt_builder`
- ✅ `app_core.py`：移除 9 個未使用模組 import（etf_data/etf_fetcher/etf_holdings/etf_profile_fetcher/freshness/numutil/housing_fetcher/price_fetcher/season_chart）
- ✅ `pages/housing.py`：補 `import numutil` + `_render_evidence_news`（修復 NameError）、移除 dead `render_key_hint`
- ✅ `pages/tw.py`：移除 numutil/SENTIMENT_STYLE/get_topic/render_github_save/save_to_github
- ✅ `pages/us.py`：移除 SENTIMENT_STYLE/_render_evidence_news/mention_caption
- ✅ `pages/etf.py`：移除 numutil/SENTIMENT_STYLE/STALE_REPORT_DAYS
- ✅ `season_chart.py`：移除 bare `import matplotlib`
- pyflakes 全庫掃描零警告；修復 2 個潛在 runtime NameError

### PR #98 — reversal_signals.py 中線翻轉偵測（已併入 main）
- ✅ 新建 `reversal_signals.py`（SSOT）：三大硬指標共振，≥2 同向 → 絕對買進/賣出
  - 指標一：60MA 慣性翻轉（連3天實體 + 扣抵值方向預測季線彎向）
  - 指標二：大盤（融資維持率+外資期貨淨部位）/ 個股（集保大戶vs散戶持股分級）
  - 指標三：TSM+NVDA 週K結構（雙標的破位確認轉壞；任一吞噬/長下影確認好轉）
  - chip_df=None 時用確定性 mock；接通真實 API 只需替換 `_mock_*` 兩函數
- ✅ `requirements.txt` 新增 `yfinance>=0.2`
- ✅ `pages/tw.py`：新增 `tool_reversal_detector()` + `_detect_reversal_cached(ttl=3600)` + 互動工具 expander

### PR #99 — reversal_signals Streamlit UI（已併入 main）
- ✅ `pages/tw.py`：新增 `_detect_reversal_cached(ttl=3600)` + `tool_reversal_detector()` 互動面板
- ✅ `page_tw()` 新增「🔭 中線翻轉偵測」expander（台股/美股大盤與個股共振訊號）
- ✅ `STATE.md` 補記 PR #96/#97/#98 結案、重構藍圖 P4-A/P4-B 標記完成

### PR #100 — chip_calendar SSOT 修正（已併入 main）
- ✅ SSOT 稽核發現：`chip_calendar.upcoming_chip_events()` fallback 使用 `date.today()`（UTC）
- ✅ 修正：新增 `import tz_utils`；`today = today or tz_utils.taiwan_now().date()`
- 影響：UTC 00:00–07:59（台灣 08:00–15:59）期間季底/MSCI/除息事件判斷差一天的 Bug 修復

### SSOT 全庫稽核結果（2026-06-30，reversal_signals 上線後）
- ✅ reversal_signals.py：yfinance lazy import SSOT；TSM/NVDA 常數集中；門檻常數集中；純計算層零 st 依賴
- ✅ pages/tw.py：_detect_reversal_cached @st.cache_data(ttl=3600) 正確；無循環 import
- ✅ pyflakes 全庫掃描零警告（10 個模組確認）
- ⚠️ verify_chip_data.py：診斷腳本用 date.today()（非生產路徑，刻意保留，僅供一次性驗證用）

### PR #102 — reversal_signals 排程整合 + UI 唯讀面板（已併入 main）
- ✅ `paths.py`：新增 `LATEST_REVERSAL` / `ARCHIVE_REVERSAL` SSOT 路徑
- ✅ `reversal_signals.py`：新增 `_load_real_market_chip()`，讀 `latest_futures_chip.json` 外資期貨淨部位混入 9 筆 mock 歷史列；`detect_trend_reversal()` 大盤模式優先用真實籌碼
- ✅ `update_data.py`：新增 `_REVERSAL_SYMBOLS` + `_run_reversal_detection()`；`main()` 每日自動存 `latest_reversal.json` + `data/reversal/{date}.json`
- ✅ `app_core.py`：對外暴露 `REVERSAL_PATH`
- ✅ `pages/tw.py`：新增 `sec_reversal()` 唯讀面板（排程存檔顯示）；`tool_reversal_detector()` 改呼叫共用 `_render_reversal_result()`，消除重複渲染邏輯；`page_tw()` 接入 `sec_reversal()`

### 重構藍圖待辦（依優先順序）
- [x] P1：System Prompts 外移 `prompts/*.yaml`（PR #87 結案）
- [x] P1：`app.py` 拆分 `pages/`（PR #88 + #89 結案）
- [x] P2-A：`prompt_builder.py`（PR #91 結案）
- [x] P2-B：`line_notify.py` DRY 截斷邏輯（PR #93 結案）
- [x] P3-A：Fetcher 快取強化（已審查，架構正確—button-triggered fetchers 刻意用 session_state，無需改動）
- [x] P3-B：`query_config.json` 關鍵字外移（PR #94 結案）
- [x] P4-A：`prompt_builder.py` 模板化（PR #96 結案）
- [x] P4-B：dead code 清除（PR #97 結案）

### PR #86 — 期現背離偵測（已併入 main）
- ✅ `index_fetcher.py`：新增 `detect_spot_futures_divergence()` — ^SOX 現貨 vs NQ=F（優先）/ES=F（fallback）期貨，訊號類型：`reversal`（⚡）/ `follow_through`（⚠️）/ `caution`（⚠️）/ `normal`（靜默）；背離門檻 ≥ 2%
- ✅ `update_data.py`：`build_intl_alert()` drop list 後立即偵測背離，注入 Gemini prompt「【期現背離偵測（程式算，非 AI）】」區塊；結果以 `futures_divergence` 欄位存入 intl alert dict
- ✅ `line_notify.py`：`build_intl_alert_line_message()` 在免責聲明前插入期現背離訊號行（⚡/⚠️ + 中文說明）

### PR #104 — SSOT 集中化（已併入 main）
- ✅ 同 SSOT 稽核修正條目內容，正式合併 main

### SSOT 稽核修正（2026-06-30，config.py 集中化）
- ✅ `config.py`：新增 `env_str()` 字串輔助函式
- ✅ `feature_aligner.py`：`datetime.now().date()` → `tz_utils.taiwan_now().date()`（台灣時區 SSOT）
- ✅ `update_data.py`：26 處裸 `os.environ.get` → `config.env_int/float/str`
- ✅ `app_core.py`：3 處 `os.environ.get` → `config.*`；移除多餘 try/except
- ✅ `chip_signals/vcp_signals/tech_signals/futures_chip_fetcher`：移除 `import os` → `import config`，env var 讀取走 SSOT
- ✅ `index_fetcher`：移除函數內 local `import os` → `config.env_float`
- ✅ `price_fetcher/housing_fetcher/etf_fetcher/etf_profile_fetcher`：移除 local `import os` → `config.env_str` 讀 PROXY_URL
- 稽核結果：全庫 `os.environ.get` 使用點 SSOT 合規（`config.py`/`proxy_helper.py`/`gemini_client.py`/`github_store.py` 為自身 SSOT，合理例外）

### SSOT 全庫稽核結果（2026-06-30，PR #104 後）
- ✅ Rule 1 datetime：全通過；`datetime.now()` 僅剩 `scripts/`（standalone）與 `verify_chip_data.py`（一次性診斷）
- ✅ Rule 2 google.generativeai：全通過；僅 `gemini_client.py` 走官方 SDK
- ✅ Rule 3 os.environ.get：全通過；僅 `config/proxy_helper/gemini_client/github_store/scripts/verify_chip_data` 六個合法位置
- ✅ Rule 4 numutil 函數：全通過；`pct_change/parse_number` 唯一定義在 `numutil.py`
- ✅ Rule 5 LINE API 呼叫：`scripts/nas_line_bot.py:52` 的 `LINE_REPLY_ENDPOINT` 屬刻意例外（NAS 單檔零相依，無法 import `line_notify`），已就地加注說明
- ✅ Rule 6 路徑字面值：全通過；所有路徑集中 `paths.py`
- ✅ Rule 7 循環 import：全通過；`pages/*.py → app_core.py`，零循環

### PR #103 — 隱藏 Streamlit 自動多頁導覽列（已併入 main）
- ✅ `app.py`：`main()` 開頭注入 CSS `[data-testid='stSidebarNav']{display:none}`，隱藏 Streamlit 1.28+ 自動偵測 `pages/` 產生的多頁導覽列，消除與 `st.sidebar.radio()` 自訂導覽的衝突

### ETF 淨值/折溢價 LINE 推播（2026-07-09，開發中）
- ✅ `nav_fetcher.py`（新建 SSOT）：ETF 淨值 NAV + 折溢價，**fail-loud 不造假**
  - NAV 官方源 = 投信投顧公會 SITCA / 發行投信；實作重用已接 proxy 的 MoneyDJ Basic0004（含官方每日淨值**+日期**），yfinance navPrice 因無獨立日期不採用
  - 折溢價 = (市價−淨值)/淨值×100%；**強制比對 NAV 日期 vs 市價日期**，不同日→標「NAV 延遲」不計算（避免舊 NAV 配今日市價的假溢價）
  - 狀態機：ok / stale_nav / no_nav_date / no_nav / no_price；配息走 yfinance dividends
  - 內建離線確定性 demo（`python nav_fetcher.py`，沙箱無網路也能跑四分支）；只對 `00` 開頭 ETF 生效，個股略過
- ✅ `update_data._push_watch_for`：新增 `nav_lines = nav_fetcher.nav_lines_for(stocks)`（2.7 段），傳入 builder
- ✅ `line_notify.build_watch_line_message`：新增 `nav_lines` 參數，逐檔在 VCP 後附淨值/折溢價行
- 待辦：帶真實 `PROXY_URL` 排程實跑驗收（沙箱 yfinance/MoneyDJ 均被擋，僅離線 demo 驗證邏輯）

### 就業人口熱區 × 空屋率地圖（2026-07-08，開發中）
- ✅ `taiwan_map_data.py`（新建 SSOT）：22 縣市 Mock 就業人口（勞保投保人數）+ 空屋率（低度使用住宅比率）；`load_df()` 唯一入口；內附真實資料接入說明（勞動部 / 內政部不動產資訊平台）
- ✅ `pages/housing.py`：新增 `sec_population_map()`（3 tabs：就業熱區 choropleth / 空屋率地圖 / 雙變數氣泡圖 + 轉向分析列表）；複用 `render_taiwan_choropleth()`；`@st.cache_data(ttl=3600)`
- ✅ `paths.py`：新增 `EMPLOYMENT_VACANCY_DATA`（未來真實資料存放路徑）
- 待辦：接入真實勞動部 / 內政部 CSV，替換 `taiwan_map_data._mock_df()` 即可上線

## 深度 Code Review 落地(2026-07-10,PR #110 已併入 main)
依外部 code review 說明書實作兩衝刺(連線效能 + 正確性 + 診斷頁):
- **效能(第一衝刺)**
  - `news_fetcher`:多來源 RSS 改 `ThreadPoolExecutor(max_workers=8)` 平行抓取(彙整保序,去重優先序不變);單 feed timeout 30→10s。新聞頁預估 24s→3~4s
  - `github_store`:commit 前以 git blob sha 比對,內容未變更即跳過 PUT;GET/PUT 共用模組層 Session。`app.py` 的「抓取後自動存 GitHub」預設 True→False(抓取與存檔解耦);各頁 session fallback 同步改 False
  - `proxy_helper`:模組層共用 Session(`get_shared_session`,pool_maxsize=20,執行緒安全)取代每次 fetch 新建;新增 `prefer_direct` 參數 — Yahoo 等全球可達來源直連優先、NAS 中繼降級為備援(`index_fetcher`/`season_chart` 已接);重試 sleep 只安插在「還有下一次」時
  - `index_fetcher`:HTTP_TIMEOUT 20→10、retries 3→2;`_drop_threshold` 公開為 `drop_threshold()`(供降級路徑 SSOT 取用)
  - `app_core.load_json` 加 `@st.cache_data`(以 path+mtime_ns 為鍵,檔案更新自動失效);新增 `fetch_live_news_cached(kind)`(ttl=900)與 `fetch_index_quotes_cached()`(ttl=600),tw/us/global_/housing 四頁「立即抓取」接入
- **正確性(第二衝刺,修 review Bug ①②③④)**
  - ①裸下標:`update_data._run_chip_data` 印出與 `pages/tw.render_chip` 融資/台指期欄位全改 `.get()`+`isinstance`(缺單欄只顯示「—」,故障半徑整段→單欄);`build_intl_alert` 台指期夜盤欄位同步防呆(`change_pct` 非數值即略過)
  - ②指數全失敗:`build_intl_alert` 報價抓取包降級 → 空 quotes + `quotes_ok=False` 續跑(僅新聞面研判);`validate_intl_alert` 允許「明示降級」的空 quotes;前端顯示警示
  - ③`season_chart.fetch_sp500_2026`:裸 `except:` 收斂為具型別 except + stderr 日誌;`base_price=0` 防除零;`pages/tw` 空結果不再佔 1 小時快取(失敗清快取 + session 冷卻 5 分鐘)
  - ④mock 訊號污染:`reversal_signals` mock 列標 `is_mock=True`,`_check_chip_market/_check_chip_stock` 偵測到 mock 一律 `triggered=False`(**不納入共振**,訊號僅由 60MA+半導體週K 兩真實指標決定);頂層回 `chip_is_mock` 供 UI/LINE 標示;`hash()` 改 `hashlib.md5`(跨進程確定性);UI expander 加「🧪 模擬值(不計入共振)」徽章
  - `gemini_client.call_gemini_for_json` 強制頂層為 dict(非物件視同解析失敗 raise),15 個呼叫端的 `setdefault` 一律安全(SSOT 單點修)
- **可觀測性**:新增 `pages/diagnostics.py`「🩺 資料診斷」第 7 頁(SSOT):17 個資料檔新鮮度總覽(歸屬日/mtime/過期判定,門檻沿用 freshness+領域常數)、4 資料源平行輕量健檢(NAS/Yahoo/TWSE/Google News,按鈕觸發)+ 金鑰/Token 設定狀態、mock 清單透明化
- 驗證:pyflakes 全庫零警告(順手清 `pages/housing.py` 既有 dead import/變數)、離線 smoke test 30 項全過(平行保序/blob sha 對 git hash-object/降級路徑/mock 排除/確定性 seed)、`test_numeric_audit.py` 11/11
- 未落地(review 建議、留待後續):房市 3 tab lazy render(中風險 UX 改動)、5 頁兩步驟面板抽工廠、`etf_fetcher.crawl` 保守平行化

## Streamlit Cloud 中文字型(2026-07-10,PR #111 已併入 main;同日因雲端 Segfault 緊急回退,見下節)
- ✅ 新建 `packages.txt`(Streamlit Cloud apt 清單,單行 `fonts-noto-cjk`,**格式限制:一行一套件、不可加註解**):部署時安裝 Noto CJK 字型
- 原理:`season_chart.py` import 時偵測 `_CJK_FONTS` 候選(第一順位 `Noto Sans CJK TC`),之前雲端無中文字型 → `_ZH=False` 整張圖退回英文;裝字型後自動切回中文,**零程式碼改動**
- 驗證:沙箱 apt 實裝後 matplotlib 看到全部 15 個 Noto CJK 家族、`season_chart._ZH=True` 選中 `Noto Sans CJK TC`;實際 build_cycle_figure 渲染零缺字警告

## 緊急回退:雲端 Segfault 事故(2026-07-10,PR #112 已併入 main)
- 症狀:PR #111 部署後 Streamlit Cloud 反覆整站當機(`Segmentation fault`,原生層、無 Python 堆疊):開站後頁面渲染數秒即死,每次重啟必復發
- 診斷(排除法):
  - 同日稍早 #110 部署(無字型)在同一 Python 3.14.6 雲端環境運作正常 → 平台/Python 版本非唯一兇手
  - 沙箱 Python 3.13 + 與雲端 uv 解析完全同版套件(numpy 2.5.1/matplotlib 3.11.0/pandas 3.0.3/pyarrow 25.0.0)+ 靜態 Noto CJK ttc:單獨渲染季節圖與 AppTest 整頁重跑全過 → 程式碼與套件版本組合無罪
  - 唯一無法本機重現的變因:雲端 cp314 二進位 × 裝字型後的 CJK 繪製路徑(app 預設頁「📊 台股」即繪季節圖,與「渲染數秒後死亡、必復發」時序吻合)
- 處置:刪除 `packages.txt`(一次只動一個變因)→ 圖表暫回英文
  - 若復活 → 證實字型路徑是兇手,改用「repo 內建靜態子集字型 + `fm.addfont` + 執行緒安全 Figure」復中文(不依賴雲端 apt)
  - 若未復活 → 兇手是 Python 3.14 環境:依官方文件刪除 app 重建,Advanced settings 改選 Python 3.12(已部署 app 無法直接改版本)
- 教訓:`packages.txt`(apt 層)改動會觸發雲端整個環境重建、且沙箱無法重現其基底映像,屬高風險變更;字型類需求優先用 repo 內建資產

## 待辦 ⏳
- [x] 全市場化 ETF **程式已完成**:看板「🌐 一鍵匯入全市場 ETF」(`etf_fetcher.import_all_etfs`)→ 重抓成分股/圖鑑(`etf_fetcher.crawl` / `etf_profile_fetcher.crawl`)→ 自動存 GitHub 全接妥(`app.py` 443-455 / 404 / 546)。**待帶真實 `PROXY_URL` 在看板按一次**即生效(沙箱無代理,無法代跑)。
- [x] repo Secrets `PROXY_URL` 早已設妥，排程(ETF/股價/房價)持續正常運作。
- [x] 個股盯盤(第二個 LINE bot)**已上線驗收通過(2026-06-28)**:傳「加 2330」bot 正確回「已加入 2330」並顯示 watchlist 4 檔(6770/6239/3231/2330);NAS `nas_line_bot.py` webhook 對外可達,Secrets 全設妥,watchlist.json 寫回 GitHub 正常。
- [x] **上櫃月營收已實作**(`earnings_fetcher._fetch_otc_bulk`):MOPS `ajax_t05st10_q` POST 一次全抓,`fetch_monthly_revenue()` 透明合併上市(TWSE) + 上櫃(MOPS),呼叫端零改動;需 proxy 過境 MOPS。
- [x] **季報 EPS 已實作(2026-06-28)**:`fetch_quarterly_eps()` 向 MOPS `ajax_t163sb04` 逐檔 POST,sii/otc 自動辨識;`_push_watch_for` 加 EPS dedup 區塊;LINE 訊息新增「📊 新季報(EPS)」段落。需 proxy + 實機驗收(MOPS 境外限速)。
- 註:§5 向量化已實查結案 — 全庫零 `numpy`/`.iterrows()`,既有 pandas(melt/dropna/line_chart)皆已向量化,其餘為小型巢狀 dict 迴圈(縣市×市場×年),改 pandas 反增風險無效益,**刻意保留**。
