"""
feature_aligner.py — 四路數據時間對齊合流 SSOT

以「台股交易日（Date）」為基準，整合：
  1. macro  — 總經/全球金流（index_fetcher：美股指數前夜、DXY、TNX、TWD）
  2. chip   — 籌碼/三大法人（chip_fetcher：盤後公布）
  3. news   — 新聞輿情（news_fetcher：當日關鍵字新聞 + 簡易情感分數）
  4. tech   — 價格/技術面概況（latest_stocks.json 快取：多空比、情感分布）

輸出：flat dict（可直接 json.dumps），供 update_data.get_master_decision()
      一次注入 Gemini 中央決策大腦。

設計原則：
  - 全部 fallback 容錯（任一路失敗 → 回空 dict，不阻斷主流程）
  - 不做任何 HTTP 請求，全部委派給既有 fetcher SSOT
  - 不使用 Gemini（純數據對齊層）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numutil       # OKU 億元係數 SSOT
import news_analyzer  # 新聞分析工具（情感評分/關鍵字比對）SSOT
import paths          # 路徑 SSOT
import tz_utils       # 台灣時區 SSOT

# 情感關鍵字已移至 news_analyzer.BULL_WORDS / BEAR_WORDS（SSOT）

_FRESHNESS_DAYS = 2  # 快取最長可接受天數


# ── 私有工具 ─────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | None:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        pass
    return None


def _is_fresh(data: dict) -> bool:
    for key in ("report_date", "date", "as_of"):
        val = str(data.get(key, ""))[:10]
        if len(val) == 10:
            try:
                age = (tz_utils.taiwan_now().date() -
                       datetime.strptime(val, "%Y-%m-%d").date()).days
                return age <= _FRESHNESS_DAYS
            except ValueError:
                continue
    return True  # 無日期欄位時樂觀通過


# ── 四路特徵抓取 ─────────────────────────────────────────────────────────

def _get_macro() -> dict:
    """總經/全球金流：從 index_fetcher 取美股指數 + DXY + TNX + TWD。"""
    try:
        import index_fetcher
        data = index_fetcher.fetch_index_quotes()
        quotes = data.get("quotes", {})

        def chg(sym: str) -> float | None:
            q = quotes.get(sym)
            return round(q["change_pct"], 2) if q else None

        def last(sym: str) -> float | None:
            q = quotes.get(sym)
            return q.get("last") if q else None

        return {
            "sp500_chg_pct":    chg("^GSPC"),
            "nasdaq_chg_pct":   chg("^IXIC"),
            "sox_chg_pct":      chg("^SOX"),
            "sp500_fut_chg":    chg("ES=F"),
            "us_10y_yield":     last("^TNX"),
            "dxy":              last("DX-Y.NYB"),
            "usd_twd":          last("TWD=X"),
            "as_of":            data.get("as_of", ""),
        }
    except Exception:
        return {}


def _get_chip() -> dict:
    """籌碼/三大法人：優先讀今日快取，fallback 呼叫 chip_fetcher。"""
    # 優先讀快取（已由排程寫入 latest_chip.json）
    cached = _load_json(paths.LATEST_CHIP)
    if cached and _is_fresh(cached):
        days = cached.get("days") or []
        if days:
            lat = days[0]  # 由新到舊，[0] 最新
            return _chip_row(lat)

    # Fallback：即時抓取
    try:
        import chip_fetcher
        data = chip_fetcher.fetch_chip_flow(days=1, log=lambda *a: None)
        days = data.get("days") or []
        if days:
            return _chip_row(days[0])
    except Exception:
        pass
    return {}


def _chip_row(row: dict) -> dict:
    oku = numutil.OKU
    return {
        "date":        row.get("date", ""),
        "foreign":     row.get("foreign"),
        "trust":       row.get("trust"),
        "dealer":      row.get("dealer"),
        "total":       row.get("total"),
        "foreign_oku": round(row.get("foreign", 0) / oku, 1),
        "trust_oku":   round(row.get("trust", 0) / oku, 1),
        "total_oku":   round(row.get("total", 0) / oku, 1),
    }


def _get_news(since_hours: int = 24) -> dict:
    """新聞輿情：抓台股/總經相關新聞，回傳情感分數 + top headlines。"""
    try:
        import news_fetcher
        queries = [
            "台股 外資 買賣超",
            "美股 大盤 今日走勢",
            "台灣股市 指數 今天",
            "Fed 聯準會 利率",
        ]
        articles = news_fetcher.fetch_news(
            queries=queries, lang="zh", region="TW",
            limit=30, since_hours=since_hours,
        )
        headlines = [a.get("title", "") for a in articles if a.get("title")]
        return {
            "sentiment_score": news_analyzer.score_headline_sentiment(headlines),
            "headline_count":  len(headlines),
            "top_headlines":   headlines[:5],
        }
    except Exception:
        return {}


def _get_tech() -> dict:
    """技術面概況：從 latest_stocks.json 快取讀取多空比與情感分布。"""
    cached = _load_json(paths.LATEST_STOCKS)
    if not cached or not _is_fresh(cached):
        return {}
    stocks = cached.get("stocks") or []
    if not stocks:
        return {}

    bull = sum(1 for s in stocks if "多" in str(s.get("sentiment", "")))
    bear = sum(1 for s in stocks if "空" in str(s.get("sentiment", "")))
    neutral = len(stocks) - bull - bear
    total = len(stocks)

    return {
        "report_date":  cached.get("report_date", ""),
        "stock_count":  total,
        "bull_count":   bull,
        "bear_count":   bear,
        "neutral_count": neutral,
        "bull_ratio":   round(bull / total, 3) if total else 0.0,
        "bear_ratio":   round(bear / total, 3) if total else 0.0,
    }


# ── 公開 API ─────────────────────────────────────────────────────────────

def build_feature_json(date: str | None = None) -> dict:
    """
    合流四路特徵，回傳以台股交易日對齊的 flat JSON。

    Parameters
    ----------
    date : str or None
        台股交易日（YYYY-MM-DD），預設今日。

    Returns
    -------
    dict
        {date, macro, chip, news, tech}
        各路失敗時對應值為空 dict（{}）；主流程不因個別失敗而中斷。
    """
    return {
        "date":  date or tz_utils.taiwan_today(),
        "macro": _get_macro(),
        "chip":  _get_chip(),
        "news":  _get_news(),
        "tech":  _get_tech(),
    }
