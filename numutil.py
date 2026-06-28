"""numutil.py — 數值計算的單一真相源(SSOT)。

把跨模組重複的數值公式(如漲跌幅、字串轉數值)集中一處,並就地內建不變量與第二法對帳,
讓「公式 + 防呆」只定義與測試一次(憲法 §2.1 SSOT / §4.2 不變量 / §4.3 對帳)。
零相依(只用 stdlib),可被任何 fetcher 安全 import。
"""
from __future__ import annotations


def pct_change(last: float, prev: float, ndigits: int = 2) -> float:
    """漲跌幅(%)= (last - prev) / prev × 100,四捨五入到 ndigits 位。

    Fail-Loud 不變量(違反即 raise,絕不回假數字):
      - last/prev 必為數值;prev 必為正(價格/前結算不可為 0 或負)。
      - 結果方向須與 (last - prev) 同號 —— 第二法對帳,杜絕公式被改錯方向。
    """
    if not isinstance(last, (int, float)) or not isinstance(prev, (int, float)):
        raise TypeError(f"pct_change 需要數值,收到 last={last!r}, prev={prev!r}")
    if prev <= 0:
        raise ValueError(f"pct_change 前值必為正,收到 prev={prev!r}")
    pct = round((last - prev) / prev * 100, ndigits)
    # 方向對帳:漲跌方向必與 (last-prev) 同號(四捨五入到 0 時兩條件皆不成立,放行)
    if (last > prev and pct < 0) or (last < prev and pct > 0):
        raise AssertionError(
            f"pct_change 方向不一致(疑公式錯):last={last}, prev={prev}, pct={pct}")
    return pct


def parse_number(s, *, as_int: bool = False, default=None):
    """字串轉數值的 SSOT。去除逗號/空格/百分號後轉型;空字串、'-'、'--' 視為無效。

    - as_int=False(預設): 回傳 float;as_int=True: 回傳 int。
    - 解析失敗或無效符號 → 回傳 default(預設 None)。
    - 業務規則(如「必須 > 0」)由呼叫端自行判斷,本函式不介入。
    """
    try:
        v = str(s).replace(",", "").replace("%", "").replace(" ", "").strip()
        if v in ("", "-", "--"):
            return default
        return int(v) if as_int else float(v)
    except (TypeError, ValueError):
        return default
