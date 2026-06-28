"""tz_utils.py — 台灣時區(UTC+8,無日光節約)時間的單一真相源(SSOT)。

全專案凡是要「台灣現在時間 / 台灣今日日期」一律呼叫這裡,不再各自手寫
``datetime.now(timezone.utc) + timedelta(hours=8)``,杜絕同一段時區邏輯散落多檔漂移。

零相依(只用 datetime),可被任何模組安全 import。
注意:scripts/nas_trigger.py 刻意維持獨立(在 NAS 上單檔執行、不依賴本專案其他模組),
故不 import 本模組——那是經評估的例外,非疏漏。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

TW_OFFSET = timedelta(hours=8)  # 台灣 UTC+8,全年固定,無日光節約


def taiwan_now() -> datetime:
    """台灣現在時間(已加 +8 偏移的 naive datetime,可直接取 .hour/.minute/.strftime)。"""
    return datetime.now(timezone.utc) + TW_OFFSET


def taiwan_today(fmt: str = "%Y-%m-%d") -> str:
    """台灣今日日期字串,預設 YYYY-MM-DD(報告日期、去重判斷皆以此為準)。"""
    return taiwan_now().strftime(fmt)


def iter_trading_days(
    n: int,
    from_date: date | None = None,
    holidays: "frozenset[date] | set[date] | None" = None,
):
    """由新到舊產出最多 n 個交易日(date 物件),跳過週六/日及指定假日。

    n:        最多 yield 的交易日數量。
    from_date: 起始日期(date 物件),None 則以本機今日為基準。
    holidays:  要額外跳過的假日集合(date 物件)；None 表示不過濾假日
              (適用於 API 自動回無資料的台股 fetcher)。
              台股假日請傳 TW_HOLIDAYS；美股假日請傳 US_HOLIDAYS。
    """
    d = from_date if from_date is not None else date.today()
    _hols = holidays or frozenset()
    count = 0
    while count < n:
        if d.weekday() < 5 and d not in _hols:  # 非週末且非假日
            yield d
            count += 1
        d -= timedelta(days=1)


def _d(s: str) -> date:
    y, m, day = s.split("-")
    return date(int(y), int(m), int(day))


# ── 台灣證券交易所休市日 2025–2026 ──────────────────────────────────────────
# 來源：TWSE 公告 + 政府行政曆；每年初更新。週六/日已由 iter_trading_days 跳過，
# 此處只列「平日休市」(含補假、颱風假等固定公告日)。
TW_HOLIDAYS: frozenset[date] = frozenset(map(_d, [
    # 2025
    "2025-01-01",  # 元旦
    "2025-01-27", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",  # 春節
    "2025-02-28",  # 和平紀念日
    "2025-04-03", "2025-04-04",  # 兒童節/清明節連假
    "2025-05-01",  # 勞動節
    "2025-05-30",  # 端午節
    "2025-10-10",  # 國慶日
    # 2026
    "2026-01-01",  # 元旦
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",  # 春節
    "2026-02-27",  # 和平紀念日補假
    "2026-04-03", "2026-04-06",  # 兒童節/清明節連假
    "2026-05-01",  # 勞動節
    "2026-06-19",  # 端午節
    "2026-10-09",  # 國慶日補假
]))

# ── 美國證券交易所(NYSE/NASDAQ)休市日 2025–2026 ──────────────────────────
# 來源：NYSE 官方行事曆；週六/日已由 iter_trading_days 跳過。
US_HOLIDAYS: frozenset[date] = frozenset(map(_d, [
    # 2025
    "2025-01-01",  # New Year's Day
    "2025-01-20",  # MLK Day
    "2025-02-17",  # Presidents' Day
    "2025-04-18",  # Good Friday
    "2025-05-26",  # Memorial Day
    "2025-06-19",  # Juneteenth
    "2025-07-04",  # Independence Day
    "2025-09-01",  # Labor Day
    "2025-11-27",  # Thanksgiving
    "2025-12-25",  # Christmas
    # 2026
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents' Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed, falls on Friday)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
]))
