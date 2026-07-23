"""paths.py — 全專案檔案/目錄路徑的單一真相源(Single Source of Truth)。

所有資料檔與封存目錄的路徑字面值只在這裡定義一次;app.py、update_data.py、
各 ETF/籌碼 fetcher 一律 import 這裡的常數,杜絕同一路徑在多個檔案各寫一份、
日後改了 A 忘了改 B 而漂移不一致。

零相依(只用標準庫 pathlib/os/tempfile),可被任何模組安全 import,不會造成循環匯入。
各模組可保留自己原本的常數名,只把「值」綁定到這裡(引用處不必改)。
另提供 atomic_write_text():這些路徑的原子寫入原語(SSOT),供各狀態寫入點共用,
杜絕「併發讀到半寫檔」「寫入途中崩潰留壞檔」造成的狀態競態。
"""

from __future__ import annotations

import json
import os
import tempfile
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
PUSH_HEARTBEAT = Path("data/push_heartbeat.json")  # 推播心跳:上次成功推播日期(次日自檢遺漏)
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

# ── 跨專案匯出:多智能體系統(2026_strategy_0719)讀取的新聞 DB ──
# export_news_db.py 產;schema: date/title/content/sentiment_score。
# 部署時常以環境變數 NEWS_DB 覆蓋為 NAS 共享絕對路徑(與下游 2026 一致)。
NEWS_DB = Path("news.db")

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

# ── 房產法規月報（每月更新一次）──
LATEST_HOUSING_REG = Path("latest_housing_reg.json")  # 最新房產法規摘要
ARCHIVE_HOUSING_REG = Path("data/housing_reg")         # 法規月報歸檔目錄（YYYY-MM.json）

# ── 就業人口 × 空屋率地圖（taiwan_map_data.py）──
EMPLOYMENT_VACANCY_DATA = Path("data/employment_vacancy.json")  # 未來接真實資料時的存放路徑


# ── 原子寫入原語(SSOT):狀態/資料檔的安全落地,防狀態競態 ──────────────────────

def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """原子化寫入文字檔:先寫「同目錄」唯一 temp,fsync 落盤後 os.replace 覆蓋目標。

    保證任何讀者只會看到「舊的完整檔」或「新的完整檔」,絕不會讀到半寫內容;
    寫入途中崩潰只遺下 temp、不污染目標。temp 與目標同目錄 → 確保同一檔案系統
    (os.replace 跨檔案系統會失敗)。目錄不存在時自動建立。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:  # noqa: BLE001 — 任何失敗都要清掉 temp、不留半殘檔,再原樣拋出
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default=None, encoding: str = "utf-8"):
    """讀 JSON 檔的 SSOT(與 atomic_write_text 讀寫對稱):檔不存在/空白/損毀/非法 JSON
    → 回 default(不 raise)。消除各模組重複的 `json.loads(path.read_text())` + try/except
    讀檔輪子;讀者只會拿到完整解析結果或 default。
    """
    try:
        text = path.read_text(encoding=encoding)
    except (FileNotFoundError, OSError):
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return default
