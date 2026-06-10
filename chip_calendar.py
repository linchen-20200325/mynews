"""chip_calendar.py — 可預測的法人賣壓/籌碼事件行事曆(純規則,零網路零 AI)。

把『事先就知道』的籌碼事件算成帶日期的清單,供國際盤預警頁顯示與 LINE 預警:
  - 季底/年底作帳行情(3/6/9/12 月底)
  - 除權息旺季(7–8 月,棄息賣壓)
  - MSCI 季度調整(2/5/8/11 月,慣例約當月最後交易日;實際以官方公告為準)
  - ETF 除息潮(直接讀 etf_profiles.json 的【真實】除息月份,非推測)

【真實性】本模組不產生任何市場數字;日期為曆法/慣例推算,逐筆標註來源。
ETF 除息檔數取自真實爬取的 dividend_months,屬真實資料。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

ETF_PROFILES_PATH = Path("etf_profiles.json")

# 季底作帳月份;MSCI 季度生效月份。
_QUARTER_END_MONTHS = {3, 6, 9, 12}
_MSCI_MONTHS = {2, 5, 8, 11}
_DIVIDEND_PEAK_MONTHS = {7, 8}  # 台股除權息旺季


def _last_weekday(year: int, month: int) -> date:
    """該月最後一個工作日(週末往前推;不含國定假日,僅供慣例近似)。"""
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _nth_last_weekday(year: int, month: int, n: int) -> date:
    """該月『倒數第 n 個工作日』(作帳行情常見啟動點,取倒數第 5 個工作日)。"""
    d = _last_weekday(year, month)
    cnt = 1
    while cnt < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            cnt += 1
    return d


def _trading_days_until(today: date, target: date) -> int:
    """today→target 之間的工作日數(近似交易日;國定假日順延,故為上界)。"""
    if target < today:
        return -1
    days = 0
    d = today
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _load_etf_dividend_counts(profiles_path: Path) -> dict[int, int]:
    """從 etf_profiles.json 統計各月『真實』除息檔數(排除推測月份)。"""
    try:
        doc = json.loads(profiles_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 無檔/壞檔 → 不阻斷,回空
        return {}
    counts: dict[int, int] = {}
    for prof in (doc.get("profiles") or {}).values():
        if prof.get("months_estimated"):
            continue  # 只採真實除息月份,推測值不計
        for m in (prof.get("dividend_months") or []):
            if isinstance(m, int) and 1 <= m <= 12:
                counts[m] = counts.get(m, 0) + 1
    return counts


def _event(eid, etype, title, target, today, detail, source):
    return {
        "id": eid, "type": etype, "title": title,
        "date": target.isoformat(),
        "days_until": (target - today).days,
        "trading_days_until": _trading_days_until(today, target),
        "detail": detail, "source": source,
    }


def upcoming_chip_events(
    today: date | None = None,
    horizon_days: int = 30,
    profiles_path: Path | None = None,
) -> list[dict]:
    """回傳未來 horizon_days 內、帶日期的可預測籌碼事件(由近到遠)。"""
    today = today or date.today()
    profiles_path = profiles_path or ETF_PROFILES_PATH
    horizon = today + timedelta(days=horizon_days)
    events: list[dict] = []

    # 掃描今天起 horizon 內所牽涉的月份(本月 + 下月足以覆蓋 30 天)
    months = {(today.year, today.month)}
    nxt = (today.replace(day=28) + timedelta(days=7))
    months.add((nxt.year, nxt.month))

    for (y, m) in months:
        # 1) 季底/年底作帳(倒數第 5 個工作日起算)
        if m in _QUARTER_END_MONTHS:
            t = _nth_last_weekday(y, m, 5)
            if today <= t <= horizon:
                year_end = (m == 12)
                events.append(_event(
                    f"window-{y}-{m:02d}", "年底作帳" if year_end else "季底作帳",
                    f"{m}月{'年底' if year_end else '季底'}投信作帳窗口(約月底前一週)",
                    t, today,
                    "投信為衝季/年度績效常於季底拉抬持股,作帳結束後可能獲利了結出量。",
                    "慣例",
                ))
        # 2) MSCI 季度調整(慣例約當月最後交易日生效)
        if m in _MSCI_MONTHS:
            t = _last_weekday(y, m)
            if today <= t <= horizon:
                events.append(_event(
                    f"msci-{y}-{m:02d}", "MSCI調整",
                    f"{m}月 MSCI 季度成分股調整生效(慣例約當月最後交易日)",
                    t, today,
                    "被動資金須依新權重換股,生效日尾盤常見爆量調節;實際日期以 MSCI 官方公告為準。",
                    "慣例(以 MSCI 公告為準)",
                ))
        # 3) 除權息旺季(7/8 月,於月初提示棄息賣壓)
        if m in _DIVIDEND_PEAK_MONTHS:
            t = date(y, m, 1)
            while t.weekday() >= 5:
                t += timedelta(days=1)
            if today <= t <= horizon:
                events.append(_event(
                    f"exdiv-season-{y}-{m:02d}", "除權息旺季",
                    f"{m}月台股除權息旺季",
                    t, today,
                    "除權息集中期,部分外資為避股利稅於除息前調節,易見棄息賣壓。",
                    "慣例",
                ))

    # 4) ETF 除息潮(真實除息月份;於月初提示,門檻 >=8 檔才算『潮』)
    counts = _load_etf_dividend_counts(profiles_path)
    for (y, m) in months:
        n = counts.get(m, 0)
        if n < 8:
            continue
        t = date(y, m, 1)
        while t.weekday() >= 5:
            t += timedelta(days=1)
        if today <= t <= horizon:
            events.append(_event(
                f"etf-exdiv-{y}-{m:02d}", "ETF除息潮",
                f"{m}月 ETF 除息密集({n} 檔)",
                t, today,
                f"本月有 {n} 檔 ETF 除息(取自真實除息月份);留意除息前後的棄息賣壓與填息表現。",
                "真實(etf_profiles 除息月份)",
            ))

    events.sort(key=lambda e: e["date"])
    return events


def pick_new_pushable(
    events: list[dict], pushed_ids, window_trading_days: int = 3
) -> list[dict]:
    """挑出『首次進入 N 個交易日窗口』且尚未推播過的事件(供 LINE 防洗版)。"""
    pushed = set(pushed_ids or [])
    out = []
    for e in events:
        td = e.get("trading_days_until", -1)
        if 0 <= td <= window_trading_days and e["id"] not in pushed:
            out.append(e)
    return out


if __name__ == "__main__":
    import sys
    for e in upcoming_chip_events():
        print(f"{e['date']} (T-{e['trading_days_until']}) [{e['type']}] {e['title']}")
    sys.exit(0)
