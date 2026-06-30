"""paths.py — 全專案檔案/目錄路徑的單一真相源(Single Source of Truth)。

所有資料檔與封存目錄的路徑字面值只在這裡定義一次;app.py、update_data.py、
各 ETF/籌碼 fetcher 一律 import 這裡的常數,杜絕同一路徑在多個檔案各寫一份、
日後改了 A 忘了改 B 而漂移不一致。

零相依(只用 pathlib),可被任何模組安全 import,不會造成循環匯入。
各模組可保留自己原本的常數名,只把「值」綁定到這裡(引用處不必改)。
"""

from __future__ import annotations

from pathlib import Path

# ── 每日產物:最新版(latest_*.json)與對應封存目錄(data/*) ──
LATEST_REPORT = Path("latest_report.json")
ARCHIVE_REPORTS = Path("data/reports")
LATEST_REPORTS_MULTI = Path("latest_reports.json")
ARCHIVE_REPORTS_MULTI = Path("data/reports_multi")
LATEST_TRENDS = Path("latest_trends.json")
ARCHIVE_TRENDS = Path("data/trends")
LATEST_STOCKS = Path("latest_stocks.json")
ARCHIVE_STOCKS = Path("data/stocks")
LATEST_US_STOCKS = Path("latest_us_stocks.json")
ARCHIVE_US_STOCKS = Path("data/us_stocks")
LATEST_INTL_ALERT = Path("latest_intl_alert.json")
ARCHIVE_INTL_ALERT = Path("data/intl_alert")
LATEST_CHIP = Path("latest_chip.json")
ARCHIVE_CHIP = Path("data/chip")
CHIP_PUSHED_STATE = Path("data/chip_pushed.json")  # 法人事件 LINE 已推清單(防洗版)
LATEST_MARGIN = Path("latest_margin.json")  # 融資餘額(散戶斷頭訊號)
LATEST_FUT_CHIP = Path("latest_futures_chip.json")  # 三大法人台指期留倉
LATEST_FOCUS = Path("latest_focus.json")
ARCHIVE_FOCUS = Path("data/focus")
LATEST_HOUSING = Path("latest_housing.json")
ARCHIVE_HOUSING = Path("data/housing")

# ── 市場數據快取(收盤價 / 實價登錄房價) ──
STOCK_PRICES = Path("stock_prices.json")  # 台股每日收盤價(TWSE/TPEx)
HOUSE_PRICES = Path("house_prices.json")  # 各縣市最新一季每坪均價
HOUSE_PRICE_HISTORY = Path("house_price_history.json")  # 各縣市歷年每坪均價

# ── ETF 相關設定檔(成分股 / 圖鑑基本資料 / 來源網址) ──
ETF_HOLDINGS = Path("etf_holdings.json")
ETF_PROFILES = Path("etf_profiles.json")
ETF_SOURCES = Path("etf_sources.json")

# ── 個股盯盤(第二個 LINE bot:自選 watchlist + 月營收已推 dedup)──
WATCHLIST = Path("watchlist.json")  # 自選台股/ETF 盯盤清單(NAS LINE bot 編輯,排程讀)
WATCH_REVENUE_PUSHED = Path("data/watch_revenue_pushed.json")  # 已推月營收 id(防重複推播)

# ── 其他 ──
GEOJSON = Path("taiwan_counties.geo.json")  # 全台縣市界(房市地圖)

# ── 中央決策大腦輸出（feature_aligner → Gemini master decision）──
LATEST_DECISION = Path("latest_decision.json")   # 最新綜合決策 JSON
ARCHIVE_DECISION = Path("data/decision")          # 決策歸檔目錄

# ── 中線翻轉偵測（reversal_signals 排程產物）──
LATEST_REVERSAL = Path("latest_reversal.json")   # 最新翻轉偵測 JSON
ARCHIVE_REVERSAL = Path("data/reversal")          # 翻轉偵測歸檔目錄
