"""export_news_db.py — 把 mynews 抓的新聞 + 情緒落地成 SQLite news.db。

供 2026_strategy_0719 多智能體系統讀取（schema: date / title / content / sentiment_score）。
這是「各源專案各自 export」架構的第一段（news）：mynews 是新聞資料的源頭，由它把資料
落地成下游要的 DB，避免下游重抓/重算（SSOT）。

SSOT 重用（不重寫任何邏輯）：
* 新聞抓取 → news_fetcher.fetch_news
* 情緒分數 → news_analyzer.score_headline_sentiment（[-1,1] 關鍵字式，已 SSOT）
* 台灣時間 → tz_utils（TW_OFFSET / taiwan_now / taiwan_today）
* 輸出路徑 → paths.NEWS_DB（可用環境變數 NEWS_DB 覆蓋為 NAS 共享路徑）

零新相依：只用標準庫 sqlite3。Fail-Loud：抓不到任何新聞 → raise（不留空表誤導下游）。

用法：
    python export_news_db.py                        # 產到 paths.NEWS_DB（或 env NEWS_DB）
    NEWS_DB=/volume1/data/news.db python export_news_db.py   # 指向 NAS 共享路徑
    NEWS_QUERIES="台股,台積電,Fed" python export_news_db.py   # 自訂查詢（逗號分隔）
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import news_analyzer
import news_fetcher
import paths
import tz_utils

# 預設查詢：涵蓋「國際情勢 + 台股」，讓下游可依各自關鍵字篩得到新聞。
# 可用環境變數 NEWS_QUERIES（逗號分隔）覆蓋。
_DEFAULT_QUERIES: list[str] = [
    "台股", "台積電", "半導體", "外資 台股", "加權指數",
    "Fed 利率", "美股", "通膨 CPI", "降息", "美債殖利率",
]
_RETENTION_DAYS = 10   # news.db 只留最近 N 天（下游 lookback 預設 7 天，留餘裕）
_TW_TZ = timezone(tz_utils.TW_OFFSET)


def _to_date_str(published: str) -> str:
    """RSS published（RFC822/含時區）→ 台灣時區 YYYY-MM-DD;解析失敗 → 今天（tz_utils）。"""
    if published:
        try:
            dt = parsedate_to_datetime(published)
        except (TypeError, ValueError, IndexError):
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_TW_TZ).strftime("%Y-%m-%d")
    return tz_utils.taiwan_today()


def write_news_db(news_items: list[dict], db_path: Path) -> int:
    """把新聞寫進 news.db（INSERT OR REPLACE 去重 + 保留最近 N 天）；回寫入筆數。

    每則情緒用 news_analyzer.score_headline_sentiment（標題 + 摘要一起評，關鍵字面較廣）。
    schema 對齊下游 seed_demo_dbs：date/title/content/sentiment_score。
    """
    rows: list[tuple[str, str, str, float]] = []
    for it in news_items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        content = (it.get("summary") or "").strip()
        date = _to_date_str(it.get("published", ""))
        score = news_analyzer.score_headline_sentiment([f"{title} {content}".strip()])
        rows.append((date, title, content, score))

    if not rows:
        raise RuntimeError("抓不到任何有效新聞 → 拒絕寫入 news.db（Fail-Loud，不留空表誤導下游）")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS news ("
            "date TEXT NOT NULL, title TEXT, content TEXT, sentiment_score REAL)"
        )
        # 去重鍵：同一天同標題只留一則（重跑 idempotent）。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_date_title ON news(date, title)"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO news(date, title, content, sentiment_score) "
            "VALUES (?,?,?,?)",
            rows,
        )
        cutoff = (tz_utils.taiwan_now() - timedelta(days=_RETENTION_DAYS)).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM news WHERE date < ?", [cutoff])
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def _resolve_output(cli_output: str | None) -> Path:
    return Path(cli_output or os.environ.get("NEWS_DB") or str(paths.NEWS_DB))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="產生 news.db 供多智能體系統讀取")
    parser.add_argument("--output", help="news.db 輸出路徑（預設 env NEWS_DB 或 paths.NEWS_DB）")
    parser.add_argument("--limit", type=int, default=60, help="抓取新聞則數上限")
    parser.add_argument("--since-hours", type=int, default=168, help="回溯小時（預設 7 天）")
    args = parser.parse_args(argv)

    queries = [q.strip() for q in os.environ.get("NEWS_QUERIES", "").split(",") if q.strip()]
    queries = queries or _DEFAULT_QUERIES

    news = news_fetcher.fetch_news(queries, limit=args.limit, since_hours=args.since_hours)
    out = _resolve_output(args.output)
    n = write_news_db(news, out)
    print(f"✅ news.db 已更新：{n} 筆 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
