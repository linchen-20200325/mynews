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
- 跨專案匯出:`export_news_db.py` → `news.db`(schema `date/title/content/sentiment_score`),重用 `news_fetcher` + `news_analyzer`(情緒 SSOT),供 `2026_strategy_0719` 多智能體系統讀取。路徑走 `paths.NEWS_DB`(可 env `NEWS_DB` 覆蓋為 NAS 共享路徑),排在下游 07:30 pull 前跑(NAS)。
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

## 緊急回退:雲端 Segfault 事故(2026-07-10,PR #112 回退字型 + PR #113 回釘依賴,均已併入 main)
- 症狀:PR #111 部署後 Streamlit Cloud 反覆整站當機(`Segmentation fault`,原生層、無 Python 堆疊):開站後頁面渲染數秒即死,每次重啟必復發
- 診斷(排除法):
  - 同日稍早 #110 部署(無字型)在同一 Python 3.14.6 雲端環境運作正常 → 平台/Python 版本非唯一兇手
  - 沙箱 Python 3.13 + 與雲端 uv 解析完全同版套件(numpy 2.5.1/matplotlib 3.11.0/pandas 3.0.3/pyarrow 25.0.0)+ 靜態 Noto CJK ttc:單獨渲染季節圖與 AppTest 整頁重跑全過 → 程式碼與套件版本組合無罪
  - 唯一無法本機重現的變因:雲端 cp314 二進位 × 裝字型後的 CJK 繪製路徑(app 預設頁「📊 台股」即繪季節圖,與「渲染數秒後死亡、必復發」時序吻合)
- 處置一(PR #112):刪除 `packages.txt` → 重建後**仍當機** → 字型無罪(等於做了對照組實驗)
- 真因確立:雲端 venv 平日被快取沿用(僅改 code 的部署不重解析依賴);#111 的 packages.txt 觸發**環境整個重建**,首次抓進事故當日剛發布的 cp314 wheel(pyarrow 25.0.0 08:25 UTC/websockets 16.1 06:30 UTC,numpy 2.4→2.5.1 同批帶入),此後每次重建都中毒 → 與「#110 部署(18:37,僅改 code,沿用舊環境)正常、#111(19:29)起必掛」完全吻合。頭號嫌犯 pyarrow:st.dataframe 每次渲染必經其 C++ 核心,且 17.0.0 曾有特定平台 wheel 載入即段錯誤前科(apache/arrow#44342,退版即解)
- 處置二(PR #113):requirements.txt 回釘 `pyarrow<25`+`websockets<16.1`+`numpy<2.5`(退回版皆確認有 cp314 linux wheel,不會觸發源碼編譯)→ 服務恢復後逐一解除回釘鎖定真兇並回報上游
- 教訓:雲端 venv 只在環境層變更時重建,unpinned 依賴的「實際版本」≠「最新版」;任何觸發重建的變更都可能一次引入多個未知新版 — 關鍵原生套件(pyarrow/numpy)建議常態鎖上限
- 字型後續:字型本身無罪,服務穩定後可直接恢復 `packages.txt`(fonts-noto-cjk)復中文,無需 repo 內建字型方案

## 推播交易日守門(2026-07-11,PR #114 已併入 main)
- 問題:排程天天跑(NAS 06:00 主力 + GitHub 06:40/07:30 兜底,見 daily_update.yml 註解),推播鏈完全沒有開盤日判斷 → 週末/國定假日照推全套 LINE
- 處置:新增 `tz_utils.is_tw_trading_day()`(週六日 + TW_HOLIDAYS,單一定義);非台股交易日 LINE **僅推 ① 國際盤快報**(週六早上=美股週五收盤仍有閱讀價值),② 共振/③ 法人事件預告/④ 戰略報告與 ⑤ 個股盯盤靜音;**報告與看板資料照常產出**(Gemini 照跑、存檔不受影響)
- 彈性:repo vars 設 `PUSH_ALL_DAYS=1` 可恢復天天全量推播(測試用)
- 注意:③ 法人事件預告靠 pushed-id 去重,假日靜音只是延後到下個交易日推,不會漏
- NAS 端 `scripts/nas_trigger.py` 刻意不改(零相依原則):守門統一放程式端,NAS/GitHub 兩種觸發都蓋得到

### 守門旁通修復(2026-07-12,PR #115)
- 症狀:週日 06:00 守門首戰即失效,④戰略報告+⑤個股盯盤照推(使用者截圖 + run 29169755761 日誌證實,全程無守門訊息)
- 根因:GitHub Actions 對未定義的 `vars.X` 注入**空字串**(而非不設定);`config.env_bool()` 只把 None 當未設定,空字串落入 `not in ("0","false","no")` → True → `PUSH_ALL_DAYS`(預設 False)被誤開 → `trading_day` 恆 True
- 為何潛伏至今:既有 ENABLE_* 旗標預設全 True,空字串誤判 True 結果恰好相同;PUSH_ALL_DAYS 是全 repo 第一個預設 False 的 env_bool 旗標
- 修法:env_bool 空/純空白字串一律回 default(SSOT 單點修復、與 docstring 一致,ENABLE_* 行為不變);smoke 補 5 個空字串案例——教訓:測環境變數要測「未設/真值/假值/**空字串**」四態,Actions 的 vars 未定義=空字串
- 驗收:下一個非台股交易日 = 2026-07-18(週六) 06:00,應只收一則國際盤快報

## 棄用分支稽核(2026-07-12,PR #116)
- `claude/context-restoration-migration-UX6l1`(PR #105 原稿,2026-07-09 因衝突關閉未合併,落後 main 58 commit)逐檔盤點:功能本體已全數由 PR #106 進 main(taiwan_map_data.py 一字不差;housing.py 差異行多為分支「舊版」程式,如 auto_save_github 預設 True,合回即倒退)
- 其「轉向潛力區列表」子集表格已被 main 更完整的「全台 22 縣市明細表」取代(排名/進度條/指標卡/CSV 下載),刻意不補
- 唯一遺漏:nas_line_bot.py LINE_REPLY_ENDPOINT 的 SSOT 例外就地註解(上方稽核 Rule 5 宣稱已加注,程式實缺)→ PR #116 補上,文件與程式一致
- 結論:該分支**零損失可刪**。教訓:棄用分支刪前先 `git diff main...branch` 逐檔盤點,「行數差」≠「功能遺失」(可能已被更好版本取代)

## 互動/推播體驗改善 A 層(2026-07-22,PR #119)
- 策略盤點結論:零件紮實但整體偏「全推全顯示」、缺個人化;先上高槓桿低成本三項(A 層),機器負擔近乎零,主風險是維護面積(人)→ 分批做、之間看 A3 心跳數據。
- **A1** 主 bot 四則推播(①②③④)結尾附「📊 完整分析看板」連結(Pull 入口),走 `config.DASHBOARD_URL`、未設不顯示。釐清:主 bot 無 webhook 收不到回話,能互動的是盯盤 bot(⑤,已有加/刪/清單);故 A1 對主 bot 只放看板連結、盯盤 bot 不動。
- **A2** `line_notify.MORNING_TAGLINE` 單一常數,掛①④推播與看板 caption,明示「每日晨間更新、非盤中即時」管理預期。
- **A3** 推播心跳自檢:`paths.PUSH_HEARTBEAT` + `line_notify.load/save_push_heartbeat`+`heartbeat_gap_note`;①推成功寫日期,次日間隔≥2天在①置頂警語;workflow commit-back 補 `push_heartbeat.json` 跨 run 持久化。邊界:抓「偶爾漏班」,抓不到「全服務死」(需外部 uptime 監控)。
- 啟用:repo var 設 `DASHBOARD_URL` 即開 A1;A2/A3 合併即生效。驗證:py_compile+pyflakes 零警告+離線 smoke 全過。
- 待評估(未動工):B2 per-user 推播偏好/靜音(先省額度)→ B3「查 2330」即時查詢(吃額度、兩段式繞 reply token 1 分鐘)→ B1 訂閱指令;C1 看板「我的一頁」碰 Streamlit 脆弱點,最後做。

## F1 外部心跳 dead-man's-switch(2026-07-22,PR #120)
- 補 A3 邊界(抓不到「全服務死」):`line_notify.ping_heartbeat_monitor()` — best-effort urllib GET(timeout 10s),未設 `HEARTBEAT_PING_URL`→靜默回 False、任何網路/HTTP 錯誤全吞(絕不拖垮推播管線),不印 URL(可能含機密);①國際盤推成功後於 `update_data._run_line_push` 呼叫(沿用 A3 同一每日載體①,交易日/非交易日皆涵蓋)。
- yml env 加 `HEARTBEAT_PING_URL`(放 Secrets 顧告警完整性:避免他人 ping 假冒存活壓掉真警報;未設→空字串→零影響)。啟用:healthchecks.io 建「expect daily ping」check → URL 貼進 repo Secret,收不到每日 ping 由該監控「從系統外」反向通知。
- 誠實邊界:補足「連載體①都沒推」也能被外部察覺;但仍依賴該第三方監控本身可用。驗證:py_compile+pyflakes 零警告 + 離線 smoke(未設略過/連不上不炸/mock 2xx→True、500→False)全過。

## F6 主頁資料新鮮度警示(2026-07-22,PR #120)
- 補「靜默失效:舊資料無警示」——`freshness.stale_note()` SSOT 早在(已用於 global_/etf/diagnostics),只差接主要每日面板。接進四處,過期才 `st.warning`、新鮮/無日期不顯示:`pages/tw.py::render_stocks`(report_date/`STALE_REPORT_DAYS`=2)、`render_chip`(as_of/`update_data.CHIP_STALE_DAYS`=5)、`pages/us.py::render_us_stocks`(report_date/2)、`pages/housing.py::render_housing_price_map`(as_of/`update_data.HOUSE_STALE_DAYS`=40)。全用既有具名門檻常數,零新字面值。
- 刻意跳過 `render_intl_alert`:其 as_of 追市場報價時間、週末必落後(Fri→Mon=3天)→門檻2每週一誤報;且「整段沒推」已由 F1/A3 覆蓋。選用門檻皆週末安全(chip 5/house 40 吸收假日;report_date 追每日產出日)。
- 順手:`render_housing_regulation` 內 local 變數 `freshness` 更名 `fresh_label`,避免與新 import 的模組同名遮蔽。驗證:py_compile+pyflakes 零 + 離線門檻邏輯全過;UI 未能在沙箱實跑(Streamlit),idiom 逐字沿用 global_/etf 既有可運作寫法。

## F2 推播回饋訊號(2026-07-22,PR #120,A案:白嫖盯盤 bot)
- 補「Push 單向廣播、零回饋」——主 bot 無 webhook,改走**既有盯盤 bot**(`nas_line_bot.py` 已有 webhook)收「打字回饋」。`watchlist.py`(SSOT 正本)新增純邏輯 `parse_feedback`/`record_feedback`/`format_feedback`/`feedback_help` + `_FEEDBACK_TYPES`(①②③④=國際盤/共振/法人/戰略);記進 `watchlist.json` 新 `feedback` 區的 per-user up/down 計數。`nas_line_bot.py` 逐字鏡像(遵守檔頭 SSOT 例外紀律),`handle_text` 授權後、加/刪/清單前派發,走既有 `gh_save` 寫回 repo。
- 指令:「讚 ③」「少推 ①」「回饋」(看累計);`help_text` 加一行讓人發現。v1 **只被動記訊號、不改推播行為**(broadcast 對全體一視同仁、無法 per-user 靜音,那是 multicast 化後的 B2)。
- 邊界:NAS webhook 無法在沙箱實跑;以「正本 vs 鏡像逐項一致」+ 正本邏輯離線測試 + py_compile/pyflakes 零替代,全過。

## F3 Gemini 降級露出(2026-07-22,PR #121)
- 補「Gemini 單點依賴」的**使用者可見**缺口:資料層降級早已存在(`build_intl_alert` try/except→`gemini={}`→`ai_ok=bool(gemini)`、`alert_level` 規則式 fallback),但 `ai_ok` 存了沒露出。①推播與看板改在 `ai_ok is False` 時明說「⚠️ AI 研判暫離線,以下為真實報價(數字可信),原因待補」——事實層(Yahoo 真實報價/程式算大跌)不靠 AI、獨立活著,保住每日心跳載體①不因 Gemini 掛而死(連帶保 F1/A3 不誤報)。
- 落點:`line_notify.build_intl_alert_line_message`(警示級別下)+ `pages/tw.py::render_intl_alert`(比照 quotes_ok 警語);用 `is False` → 舊資料缺 ai_ok/正常成功都不顯示,向後相容。驗證:py_compile+pyflakes 零 + 離線(False→提示、True/缺→無、降級仍保留真實報價)全過。

## F8 設定總表啟動自檢(2026-07-22,PR #121)
- 補「設定散佈的維運負擔」:`config.summary_lines()` 開機在 log 印一張功能開關(on/off)+ 金鑰在否(有/缺)總表,`update_data.main()` 於 Gemini 檢查後呼叫。讓像 `env_bool` 空字串誤關功能那種**靜默失效開機即現形**。**只印狀態、絕不印任何金鑰值**(硬規則:金鑰不進版控/log)。總表放 `config.py`(env/開關 SSOT),與 `*_enabled()` 同檔、日後加旗標順手更新。
- 驗證:py_compile+pyflakes 零 + 離線(結構正確 + 存在/缺失偵測 + 塞假金鑰驗證零洩漏)全過。

## F5 通知疲勞:平靜日壓縮 + 推播靜音(2026-07-22,PR #122,a+b)
- **(a) 平靜日壓縮**:`build_intl_alert_line_message` 在 `not alarm`(平靜、無領先大跌)時早退精簡版(標題+summary 一句+同步小跌/ai_ok 若有+非投資建議+tagline+看板連結),不展開利空/美股/台股/背離;大跌/警戒維持完整版。gap_note、ai_ok、看板連結、心跳全保留——只省平靜日長篇研判,降低習慣性略過。
- **(b) 推播靜音**:`watchlist.py`(正本)+`nas_line_bot.py`(鏡像)加 `parse_mute/set_mute/muted_types/format_mutes/mute_help`(重用 F2 `_FEEDBACK_TYPES`),存 `watchlist.json["muted"]`(全域,因主 bot broadcast)。指令「靜音 ②/恢復 ②/靜音清單」,走盯盤 bot webhook + `gh_save`。`update_data._run_line_push` 讀 `watchlist.muted_types()` 跳過 ②③④。
- ⚠️ **安全決定:①不開放靜音**——它是每日心跳載體(F1/A3 靠它偵測系統存活),靜音①會讓心跳誤報;①疲勞已由(a)壓縮解決。`set_mute` 對①友善拒絕、`muted_types` 雙保險永濾掉①。
- 驗證:py_compile+pyflakes 零 + 離線((a)壓縮/展開/gap/ai_ok +(b)靜音全案+①拒絕+正本/鏡像逐項一致)全過。NAS webhook 與 Streamlit 無法沙箱實跑,以鏡像一致性替代。

## F10 佐證來源對帳(2026-07-22,PR #123)
- 補「AI 附來源沒被驗證」的破口:9 種報告**早就輸出** `evidence_news{title,source,url}`,但 `app_core._render_evidence_news` 只把 AI 自由重打的 title/url 照印,程式從沒核對它是否真的出自本次餵入的 RSS → 硬規則1「只能依提供的新聞」無法被查核(AI 若編一個像真的來源,看板照樣顯示成佐證)。
- **`news_analyzer.verify_evidence_news(parsed, real_news)`**(新 SSOT,遞迴):走訪報告任意巢狀,對每份 `evidence_news` 逐則以「url 正規化(去 scheme/www/尾斜線)為主、title 正規化(小寫後只留字母數字漢字)為輔」比對真實新聞,就地標 `verified:True/False`,回 `{matched,total}`。**保守偏誤**:只採正規化後精確相等,寧標『無法核對』也不亂標『已核對』——一個 ✓ 保證該來源確在本次新聞中。
- 落點:7 個 `get_*`(trend/stock/us_stock/focus/housing/housing_reg/stock_query)validate 後各加一行,與既有 `count_keyword_mentions` 同 pattern、無 evidence_news 者自動 no-op;顯示端唯一升級 `_render_evidence_news`——抬頭「✓ N/M 來源已核對(⚠ K 則無法自動核對)」、逐則標 ✓/⚠,舊報告無 verified 旗標 → 顯示與改版前完全一致(向後相容)。
- ⚠️ **刻意跳過 `build_intl_alert`**:①心跳載體(F1/A3/F3/F5 全在此、最敏感),且 `render_intl_alert` 未渲染其 `interpretation[].evidence_news`(核對了也看不到),且它已是全報告信任故事最強者(真實 Yahoo 報價+程式算大跌+F3 ai_ok)——邊際價值最低,不為一致性去碰最危險函數。
- 零 prompt 改動、零資料膨脹(每則多一個 bool)、LINE 不動(尊重 F5 壓縮)。驗證:py_compile+pyflakes 零 + 離線 19 案(精確/正規化/巢狀/杜撰/翻譯標題/空輸入/向後相容/tally)全過 + **真實 intl_alert 資料 smoke(120 則餵入、AI 6 則佐證全數對帳成功 6/6)**;Streamlit 顯示無法沙箱實跑,以抬頭字串鏡像測 + idiom 沿用替代。

## F7 狀態競態:JSON 狀態檔原子寫入(2026-07-22,PR #124)
- 補「狀態競態」:`line_notify._save_json` docstring 宣稱「原子化寫入」卻是直接 `write_text`(**名實不符**)、`update_data.save_json` 亦同且重複 → 併發讀者讀到半寫檔會讓 `load` 落 except 回空 → dedup/心跳被當「沒推過」→ **重複推播 / 誤報**;寫入途中崩潰留壞檔。
- **`paths.atomic_write_text(path, text)`**(新 SSOT 原語,放零相依的 `paths.py`):寫「同目錄」唯一 temp(`tempfile.mkstemp`)→ fsync 落盤 → `os.replace` 原子 rename;讀者只會見到舊的完整檔或新的完整檔,崩潰只遺 temp 不污染目標,失敗清 temp 後原樣拋出。
- 三個狀態寫入點改用它:`update_data.save_json`(報告 + 月營收 dedup)、`line_notify._save_json`(法人事件 dedup + A3 心跳)、`watchlist.save`(盯盤清單/回饋/靜音本機檔);消除既有兩份重複非原子寫、兌現 docstring。
- ⚠️ **邊界**:①只解單機「崩潰/半寫/併發覆蓋損毀」;NAS 06:00 與 GitHub 兜底「各推一次」的**跨觸發邏輯重複推**屬架構級(40 分錯開 + 兜底僅失敗才跑已使罕見),不在此範圍。②NAS 端 `nas_line_bot.gh_save` 走 GitHub API sha-based 樂觀鎖(併發 PUT → 409 → 回「寫回失敗請重試」),本就 fail-loud、無需改。③市場數據快取(etf/housing/price 共 6 處 `write_text`)同樣可受惠,列相鄰後續、本次不擴散。
- 驗證:py_compile+pyflakes 零 + 離線 11 案(roundtrip/覆蓋/自動建目錄/無 tmp 殘留/**崩潰時舊檔完整+temp 清除**/三委派點 roundtrip)全過;NAS 鏡像邊界確認(`nas_line_bot` 無本機 save、走 gh_save,不需同步)。

## F4 儀表板脆弱:頁面級斷路器(2026-07-22,PR #125)
- 補「看板一掛反過來拖垮 Push 信任」:`app.py main()` 的 7 個 `page_*()` 派發**無 top-level try/except** → 任一頁在觸及自己內層兜底前拋例外,Streamlit 整頁噴紅色 traceback;而每則推播末尾都掛看板連結 → 使用者點進去見壞頁 → 砸掉整站信任。這正是 F4 的字面破口。
- **`app.py::_safe_render(page_fn, name)`**(頁面級斷路器,SSOT 單一兜底點):`try` 呼叫頁面、`except Exception` 顯示友善降級橫幅(「『X』頁暫時無法載入,側邊欄與其他頁不受影響,可到 🩺 資料診斷 查資料源」)+ traceback 收進 expander;7 個派發點全改走它,一個壞頁不再拖垮整站。
- **正確性關卡**:只攔 `Exception`——Streamlit `st.rerun()`/`st.stop()` 走 `ScriptControlException`(BaseException 子類、**非** Exception),不會被吞、控制流照常穿透(頁面用了 11 處 `st.rerun()`,誤攔即壞)。已於 streamlit 1.59.1 實測基底類別 MRO 確認。
- ⚠️ **邊界**:F4 另半(`packages.txt`/pyarrow segfault、冷啟動、Cloud 休眠)屬部署/基礎設施,歸既有 Task #11/#12 依賴治本,無法沙箱驗、本次不併入。驗證:py_compile+pyflakes 零 + 離線 17 案(一般例外兜住+橫幅含頁名+st.exception/expander;Stop/RerunException 穿透且不觸發橫幅;happy path 不碰 st.error;前頁崩潰不阻斷後頁)全過;Streamlit 真實渲染無法沙箱實跑,以斷路器邏輯測替代。

## 中文字型恢復 b1(2026-07-22,PR #126 已併入 main;雲端驗收綠燈 ✅)
- 承「緊急回退:雲端 Segfault 事故」收尾:三個兇手(`pyarrow<25`/`websockets<16.1`/`numpy<2.5`)仍回釘在 `requirements.txt`,故恢復 `packages.txt`(單行 `fonts-noto-cjk`、**格式硬限制:一行一套件、不可加註解**)所觸發的雲端重建會解析到安全版 → 當初 segfault 不會重演。
- 零碼改:`season_chart.py` import 時偵測 `_CJK_FONTS`,裝字型後 `_ZH` 自動 False→True 選中 `Noto Sans CJK TC`,季節圖復中文(機制自 PR #111 未變)。沙箱實證(本環境已裝 fonts-noto-cjk):`season_chart._ZH=True`、`_FONT=Noto Sans CJK TC`。
- ⚠️ 邊界:合併觸發**整個雲端 venv 重建**;三兇手已釘死 → 已知 segfault 不會重演,但任何重建都會重解析整棵樹,殘留「其他新 wheel」風險(事故已 12 天、上游點版多半沉澱)。回滾:壞了就 revert 本 PR(移除 packages.txt);因回釘仍在,revert 即回到最後穩定的釘版環境。
- b2(逐一解鎖三回釘找真兇 + 回報上游)屬**高風險**(移除安全網),留作後續「一次一個、每步觀察雲端」;本次只做 b1、不動回釘。
- ✅ **雲端驗收(2026-07-22,cp314 Python 3.14.6 重建)**:Uvicorn 乾淨啟動、**無 Segmentation fault**、季節圖完整渲染且**全中文**(`_ZH=True`)→ 回釘守住、字型裝成、2026-07-10 事故未復發。b1 結案。

## b2 回釘逐一解鎖(2026-07-22 起,階梯式)
- 目標:鎖定 cp314 真兇 + 清技術債。上游查證:pyarrow 仍 25.0.0、**無修正版、無公開 cp314 issue**;沙箱 cp311 無法重現(雲端 cp314 專屬)→ 只能雲端實測。
- 階梯(低→高風險,一次一個、每步合併後觀察雲端):**rung-1 websockets**(傳輸層、最低險,→16.1.1)→ rung-2 numpy(2.4→2.5)→ **碰 rung-3 pyarrow(頭號嫌犯、無上游修正)前停下重評估**(尊重「原生套件常態鎖上限」教訓)。
- **rung-1** ✅(#127 已併入 main,2026-07-22):刪 `websockets<16.1`(→16.1.1)。雲端 websockets 實驗進行中(待看板確認穩定)。
- **rung-2**(本次,PR 待開):刪 `numpy<2.5`(2.4→2.5),僅剩 `pyarrow<25` 續釘。⚠️ **須待 rung-1 雲端確認穩定後才合**,否則 websockets/numpy 兩變數同一次重建無法歸因。A3/F1 心跳不受影響。

## 待辦 ⏳
- [x] 全市場化 ETF **程式已完成**:看板「🌐 一鍵匯入全市場 ETF」(`etf_fetcher.import_all_etfs`)→ 重抓成分股/圖鑑(`etf_fetcher.crawl` / `etf_profile_fetcher.crawl`)→ 自動存 GitHub 全接妥(`app.py` 443-455 / 404 / 546)。**待帶真實 `PROXY_URL` 在看板按一次**即生效(沙箱無代理,無法代跑)。
- [x] repo Secrets `PROXY_URL` 早已設妥，排程(ETF/股價/房價)持續正常運作。
- [x] 個股盯盤(第二個 LINE bot)**已上線驗收通過(2026-06-28)**:傳「加 2330」bot 正確回「已加入 2330」並顯示 watchlist 4 檔(6770/6239/3231/2330);NAS `nas_line_bot.py` webhook 對外可達,Secrets 全設妥,watchlist.json 寫回 GitHub 正常。
- [x] **上櫃月營收已實作**(`earnings_fetcher._fetch_otc_bulk`):MOPS `ajax_t05st10_q` POST 一次全抓,`fetch_monthly_revenue()` 透明合併上市(TWSE) + 上櫃(MOPS),呼叫端零改動;需 proxy 過境 MOPS。
- [x] **季報 EPS 已實作(2026-06-28)**:`fetch_quarterly_eps()` 向 MOPS `ajax_t163sb04` 逐檔 POST,sii/otc 自動辨識;`_push_watch_for` 加 EPS dedup 區塊;LINE 訊息新增「📊 新季報(EPS)」段落。需 proxy + 實機驗收(MOPS 境外限速)。
- 註:§5 向量化已實查結案 — 全庫零 `numpy`/`.iterrows()`,既有 pandas(melt/dropna/line_chart)皆已向量化,其餘為小型巢狀 dict 迴圈(縣市×市場×年),改 pandas 反增風險無效益,**刻意保留**。
