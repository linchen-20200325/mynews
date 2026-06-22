"""tz_utils.py — 台灣時區(UTC+8,無日光節約)時間的單一真相源(SSOT)。

全專案凡是要「台灣現在時間 / 台灣今日日期」一律呼叫這裡,不再各自手寫
``datetime.now(timezone.utc) + timedelta(hours=8)``,杜絕同一段時區邏輯散落多檔漂移。

零相依(只用 datetime),可被任何模組安全 import。
注意:scripts/nas_trigger.py 刻意維持獨立(在 NAS 上單檔執行、不依賴本專案其他模組),
故不 import 本模組——那是經評估的例外,非疏漏。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TW_OFFSET = timedelta(hours=8)  # 台灣 UTC+8,全年固定,無日光節約


def taiwan_now() -> datetime:
    """台灣現在時間(已加 +8 偏移的 naive datetime,可直接取 .hour/.minute/.strftime)。"""
    return datetime.now(timezone.utc) + TW_OFFSET


def taiwan_today(fmt: str = "%Y-%m-%d") -> str:
    """台灣今日日期字串,預設 YYYY-MM-DD(報告日期、去重判斷皆以此為準)。"""
    return taiwan_now().strftime(fmt)
