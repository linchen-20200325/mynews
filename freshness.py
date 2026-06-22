"""freshness.py — 資料新鮮度(staleness)判定的單一真相源(SSOT)。

憲法 §2.4:資料可能是真的但「過期」。本模組提供「以資料歸屬日(as_of)判斷
落後天數」的純函式,零相依(只用 stdlib + tz_utils),供看板與排程共用。

各 fetcher 輸出的 as_of 字串開頭一律為 'YYYY-MM-DD'(資料歸屬日,非抓取日),
故抓開頭日期對台灣今日算落後天數;解析不出日期回 None,由呼叫端決定如何處置
(看板:顯示警語不阻斷;排程:可改 raise)。各來源的 max_days 門檻屬領域決策,
一律由呼叫端以具名常數/環境變數帶入,不在此寫死(避免腦補門檻)。
"""
from __future__ import annotations

import re
from datetime import date

import tz_utils

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def as_of_date(as_of: str | None) -> date | None:
    """從 as_of 字串開頭解析資料歸屬日(YYYY-MM-DD);解析不出回 None。"""
    if not as_of:
        return None
    m = _DATE_RE.search(as_of)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def staleness_days(as_of: str | None) -> int | None:
    """資料歸屬日落後台灣今日幾天;無法解析回 None。未來日回負值(原樣)。"""
    d = as_of_date(as_of)
    if d is None:
        return None
    return (tz_utils.taiwan_now().date() - d).days


def stale_note(as_of: str | None, max_days: int, label: str = "資料") -> str | None:
    """過期(落後 > max_days)回一句可顯示警語;新鮮或無法判定回 None。"""
    n = staleness_days(as_of)
    if n is None or n <= max_days:
        return None
    return f"⚠️ {label}可能過期:歸屬日落後今日 {n} 天(as_of={as_of})"
