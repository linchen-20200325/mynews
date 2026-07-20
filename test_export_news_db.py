"""test_export_news_db.py — news.db 匯出（不打網路，注入假新聞驗 DB + 下游相容）。"""

from __future__ import annotations

import sqlite3

import pytest

import news_analyzer
from export_news_db import _to_date_str, write_news_db


def _news(title, summary="", published="Mon, 20 Jul 2026 08:00:00 +0000"):
    return {"title": title, "summary": summary, "url": f"http://x/{title}",
            "published": published, "source": "t"}


def test_write_and_downstream_query(tmp_path):
    db = tmp_path / "news.db"
    n = write_news_db([
        _news("台積電獲利創高 利多", "外資買超"),
        _news("升息衝擊 美股下殺", "衰退疑慮"),
    ], db)
    assert n == 2

    conn = sqlite3.connect(str(db))
    # 欄位需與下游 2026 seed 完全一致
    cols = [r[1] for r in conn.execute("PRAGMA table_info(news)")]
    assert cols == ["date", "title", "content", "sentiment_score"]
    # 用 2026 data_agent 的同款查詢（title/content LIKE）
    rows = conn.execute(
        "SELECT date, title, sentiment_score FROM news WHERE title LIKE ? OR content LIKE ?",
        ["%台積電%", "%台積電%"],
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == "台積電獲利創高 利多"


def test_sentiment_reuses_analyzer(tmp_path):
    db = tmp_path / "s.db"
    write_news_db([_news("利多 買超 降息")], db)
    conn = sqlite3.connect(str(db))
    score = conn.execute("SELECT sentiment_score FROM news").fetchone()[0]
    conn.close()
    assert score > 0                                           # 全多頭詞 → 正分
    assert score == news_analyzer.score_headline_sentiment(["利多 買超 降息"])  # 與 SSOT 一致


def test_dedup_same_day_title(tmp_path):
    db = tmp_path / "d.db"
    write_news_db([_news("同標題新聞")], db)
    write_news_db([_news("同標題新聞")], db)                     # 重跑 → 不應增列
    conn = sqlite3.connect(str(db))
    cnt = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    conn.close()
    assert cnt == 1


def test_empty_raises(tmp_path):
    with pytest.raises(RuntimeError):
        write_news_db([], tmp_path / "e.db")
    with pytest.raises(RuntimeError):
        write_news_db([{"title": "   "}], tmp_path / "e2.db")   # 空標題略過 → 0 筆 → raise


def test_to_date_str_tw_conversion_and_fallback():
    assert _to_date_str("Mon, 20 Jul 2026 08:00:00 +0000") == "2026-07-20"
    assert _to_date_str("Sun, 19 Jul 2026 20:00:00 +0000") == "2026-07-20"   # UTC+8 跨日
    assert len(_to_date_str("看不懂的日期")) == 10                            # 失敗 → 今天
