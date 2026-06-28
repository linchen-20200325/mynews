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


def iter_trading_days(n: int, from_date: date | None = None):
    """由新到舊產出最多 n 個交易日(date 物件),跳過週六/日。

    n: 最多 yield 的交易日數量(呼叫端以此控制回看上限或蒐集上限)。
    from_date: 起始日期,None 則以本機今日為基準。
    國定假日由呼叫端依 API 回傳結果自然略過,本函式只保證週末不出現。
    """
    d = from_date if from_date is not None else date.today()
    count = 0
    while count < n:
        if d.weekday() < 5:  # 0~4 = 週一~週五
            yield d
            count += 1
        d -= timedelta(days=1)
