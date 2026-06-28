# ARCHITECTURE.md — mynews 全球政經戰略看板

> **版本**：v1.0（2026-06-28，由 Phase-1 架構審查生成）
> **禁止**：此檔案描述的是「應有」架構；修改程式碼前必須先更新此檔。

---

## 一、專案定位

**全球政經戰略看板**：以 Streamlit 為前端，每日透過 NAS Squid Proxy 抓取
台股籌碼、國際指數、ETF、房市、財報等數據，交由 Gemini LLM 分析後
以 LINE Notify 推送，並在看板上即時展示。

---

## 二、目錄結構樹

```
mynews/
│
├── app.py                      ★ Streamlit UI 入口（2979 行，待拆分）
├── update_data.py              ★ 資料更新管線入口（2928 行，待拆分）
│
├── ── 基礎設施層（零業務邏輯，可任意引入）──
│   ├── paths.py                SSOT：所有檔案路徑常數
│   ├── tz_utils.py             SSOT：台灣時區工具（taiwan_now / taiwan_today）
│   ├── numutil.py              SSOT：數值工具（pct_change）
│   ├── freshness.py            資料新鮮度判斷（stale / as_of_date）
│   └── proxy_helper.py         HTTP 代理中繼（NAS Squid proxy → 直連 fallback）
│
├── ── 資料擷取層（Fetcher，各自獨立，無 UI 相依）──
│   ├── chip_fetcher.py         三大法人買賣超（TWSE BFI82U）
│   ├── chip_signals.py         法人買賣超訊號聚合（T86）
│   ├── chip_calendar.py        重大籌碼事件行事曆（純計算，無網路請求）
│   ├── futures_chip_fetcher.py 台指期夜盤籌碼（TAIFEX OpenAPI）
│   ├── taifex_night_fetcher.py 台指期夜盤即時報價（TAIFEX MIS）
│   ├── index_fetcher.py        美股指數 / 期貨報價（Yahoo Finance chart）
│   ├── margin_fetcher.py       融資融券餘額（TWSE MI_MARGN）
│   ├── price_fetcher.py        個股收盤價（TWSE STOCK_DAY）
│   ├── earnings_fetcher.py     月營收（TWSE monthly revenue）
│   ├── tech_signals.py         技術面訊號（MA / RSI / 量價，via TWSE）
│   ├── vcp_signals.py          VCP 收縮形態偵測（依賴 tech_signals）
│   ├── etf_fetcher.py          ETF 淨值 / 折溢價（MoneyDJ HTML 解析）
│   ├── etf_profile_fetcher.py  ETF 資產規模 / 費用率（MoneyDJ HTML 解析）
│   ├── etf_holdings.py         ETF 成分股轉換（純資料變換，無網路）
│   ├── housing_fetcher.py      台灣房市成交資料（內政部 ZIP/CSV）
│   └── news_fetcher.py         Google News RSS 聚合（stdlib urllib，無 requests）
│
├── ── 資料層（Data，SSOT 靜態定義 + 快取讀取）──
│   ├── etf_data.py             ETF 基本資料 SSOT（Streamlit cache 包裝）
│   ├── watchlist.py            自選股清單 CRUD（含 tz_utils 時間戳）
│   ├── github_store.py         GitHub API 寫入（自動備份到 repo）
│   │
│   ├── etf_holdings.json       ETF 成分股靜態資料
│   ├── etf_profiles.json       ETF 資產規模 / 費用率快照
│   ├── etf_sources.json        ETF 爬蟲來源設定
│   ├── watchlist.json          自選股清單持久化
│   ├── house_prices.json       最新縣市房價快照
│   ├── house_price_history.json 縣市房價歷史
│   ├── stock_prices.json       個股收盤價快照
│   ├── taiwan_counties.geo.json GeoJSON（縣市邊界，choropleth 用）
│   │
│   ├── latest_chip.json        最新法人籌碼（update_data 寫入）
│   ├── latest_futures_chip.json 最新台指期籌碼
│   ├── latest_margin.json      最新融資融券
│   ├── latest_intl_alert.json  最新國際警示
│   ├── latest_report.json      最新策略報告
│   ├── latest_trends.json      最新趨勢雷達
│   ├── latest_stocks.json      最新台股精選
│   ├── latest_us_stocks.json   最新美股精選
│   ├── latest_focus.json       最新焦點人物
│   └── latest_housing.json     最新房市分析
│
├── data/                       歷史資料歸檔目錄
│   ├── chip/YYYY-MM-DD.json    每日法人籌碼歸檔
│   ├── focus/YYYY-MM-DD.json   每日焦點人物歸檔
│   ├── housing/YYYY-MM-DD.json 每日房市歸檔
│   ├── intl_alert/YYYY-MM-DD.json
│   ├── reports/YYYY-MM-DD.json
│   ├── stocks/YYYY-MM-DD.json
│   ├── trends/YYYY-MM-DD.json
│   └── us_stocks/YYYY-MM-DD.json
│
├── scripts/                    NAS 端獨立腳本（單檔執行，允許零相依）
│   ├── nas_trigger.py          NAS 排程觸發器（★ 刻意零相依，見 CLAUDE.md §2）
│   ├── nas_line_bot.py         NAS LINE Bot Webhook 伺服器
│   ├── test_line_push.py       LINE 推播測試工具
│   └── force_send.sh           強制觸發推播 Shell 腳本
│
├── ── 測試 / 驗證 ──
│   ├── test_numeric_audit.py   數值不變量回歸測試（numutil 相關）
│   └── verify_chip_data.py     籌碼資料完整性驗證
│
├── ── 設定 / 文件 ──
│   ├── requirements.txt        Python 依賴清單
│   ├── .streamlit/secrets.toml.example  密鑰範例（不入版本控制）
│   ├── CLAUDE.md               AI 協作協議（開發治理 SSOT）
│   ├── STATE.md                專案狀態（任務記憶 SSOT）
│   ├── ARCHITECTURE.md         ← 本檔（架構 SSOT）
│   ├── README.md               使用說明
│   ├── NAS_PROXY_GUIDE.md      NAS Proxy 設定指南
│   ├── NAS_PROXY_FOR_AI.md     AI 代理使用指南
│   └── NAS_WATCH_BOT_SETUP.md  Watch Bot 設定指南
│
└── .devcontainer/devcontainer.json  Dev Container 設定
```

---

## 三、模組職責定義（精確）

### 基礎設施層（Infrastructure）

| 模組 | 唯一職責 | 禁止事項 |
|------|----------|----------|
| `paths.py` | 定義全域檔案路徑常數（Path 物件） | 不得含業務邏輯；不得直接讀寫檔案 |
| `tz_utils.py` | 台灣時區（UTC+8）日期 / 時間工具 | 不得含網路請求；不得含路徑 |
| `numutil.py` | 數值工具：漲跌幅計算、數字格式化 | 不得含時間；不得含路徑 |
| `freshness.py` | 判斷 JSON 資料新鮮度（stale / as_of） | 不得修改資料；不得含網路請求 |
| `proxy_helper.py` | HTTP Proxy 中繼（NAS Squid → 直連） | 不得含業務 URL；不得含解析邏輯 |

### 資料擷取層（Fetcher）

所有 Fetcher 共用合約：
- **輸入**：可選 `proxy: str | None`, `log=print`
- **輸出**：dict（含 `as_of` UTC 時間戳）或 raise `RuntimeError`
- **禁止**：不得直接 import Streamlit；不得含 UI 邏輯；不得含 Gemini 調用

| 模組 | 資料來源 | 輸出路徑（via paths.py） |
|------|----------|--------------------------|
| `chip_fetcher.py` | TWSE BFI82U | `CHIP_PATH` |
| `chip_signals.py` | TWSE T86 | 無（被 update_data 調用） |
| `chip_calendar.py` | 純計算 | 無（回傳 list） |
| `futures_chip_fetcher.py` | TAIFEX OpenAPI | `FUTURES_CHIP_PATH` |
| `taifex_night_fetcher.py` | TAIFEX MIS | 無（即時查詢） |
| `index_fetcher.py` | Yahoo Finance chart | 無（即時查詢） |
| `margin_fetcher.py` | TWSE MI_MARGN | `MARGIN_PATH` |
| `price_fetcher.py` | TWSE STOCK_DAY | `PRICES_PATH` |
| `earnings_fetcher.py` | TWSE monthly revenue | 無（回傳 dict） |
| `tech_signals.py` | TWSE STOCK_DAY | 無（計算後回傳） |
| `vcp_signals.py` | via tech_signals | 無（計算後回傳） |
| `etf_fetcher.py` | MoneyDJ | `ETF_SOURCES_PATH` |
| `etf_profile_fetcher.py` | MoneyDJ | `ETF_PROFILES_PATH` |
| `etf_holdings.py` | etf_holdings.json（靜態） | 無 |
| `housing_fetcher.py` | 內政部 ZIP/CSV | `HOUSE_PRICES_PATH` |
| `news_fetcher.py` | Google News RSS | 無（回傳 list） |

### 資料層（Data）

| 模組 | 唯一職責 |
|------|----------|
| `etf_data.py` | ETF 靜態基本資料 SSOT（`@st.cache_data` 包裝讀取） |
| `watchlist.py` | 自選股清單 CRUD + 新鮮度時間戳 |
| `github_store.py` | 將 JSON 備份寫入 GitHub repo（單一出口） |

### 應用層（Application）

| 模組 | 唯一職責 | 禁止事項 |
|------|----------|----------|
| `update_data.py` | NAS 排程管線：抓資料 → Gemini 分析 → LINE 推播 | 不得含 Streamlit 相依；不得含 UI |
| `app.py` | Streamlit 前端：讀 latest_*.json → 渲染 UI | 不得含資料抓取；不得直接調用 API |

---

## 四、資料流向圖

```
外部 API / 來源
    │
    ▼
[Fetcher 層] ──寫入──► latest_*.json / data/YYYY-MM-DD.json
    │
    ▼
[update_data.py] ──讀取 latest_*.json──► Gemini 分析 ──► 覆寫 latest_*.json
    │
    ├──► LINE Notify（推播）
    └──► GitHub Store（備份）

[app.py] ──讀取 latest_*.json──► Streamlit UI 渲染
    │
    └──► 使用者觸發「即時抓取」──► 直接調用 update_data 函式 ──► session_state
```

---

## 五、SSOT 對照表（Single Source of Truth）

| 類別 | SSOT 所在 | 嚴禁重複定義於 |
|------|-----------|----------------|
| 檔案路徑 | `paths.py` | 所有其他模組 |
| 台灣時間 | `tz_utils.py` | 所有其他模組 |
| 漲跌幅計算 | `numutil.pct_change()` | 所有其他模組 |
| ETF 基本資料 | `etf_data.py` | `app.py`、`etf_fetcher.py` |
| `_to_int()` / `_to_float()` | `numutil.py`（待建立） | chip_fetcher / margin_fetcher 等 |
| 2-tier HTTP fetch | `proxy_helper.py`（待擴充） | chip_fetcher / margin_fetcher 等 6 檔 |
| env var 解析 | `config.py`（待建立） | `update_data.py` 內 60+ 個散落讀取 |

---

## 六、已知技術債（Phase-1 審查結論）

詳見「功能審查報告」（附於 ARCHITECTURE.md 同次提交），以下為嚴重度排序：

1. **[CRITICAL]** `update_data.py::main()` 294 行的單體 Pipeline
2. **[CRITICAL]** `app.py::render_stock_query()` 164 行 God Function
3. **[HIGH]** 6 個 Fetcher 各自重複 2-tier HTTP fetch 邏輯（~150 行重複）
4. **[HIGH]** 5 個模組各自定義 `_to_int()` / `_to_float()`（違反 numutil SSOT）
5. **[HIGH]** `render_stocks()` vs `render_us_stocks()` 90% 重複（~87 行）
6. **[HIGH]** `update_data.py` 缺少 `gemini_client.py`、`line_notify.py`、`config.py` 拆分
7. **[MEDIUM]** 10 個 `validate_*()` 函式結構雷同，可泛化
8. **[MEDIUM]** 日期範圍生成（交易日 iterator）在 3 個 Fetcher 中各自實作
