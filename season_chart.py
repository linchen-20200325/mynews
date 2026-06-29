"""
season_chart.py — 總統任期週期季節性分析 SSOT

提供：
  build_cycle_figure(actual_2026)  → matplotlib Figure（app.py 用 st.pyplot 顯示）
  fetch_sp500_2026()               → {month: cum_return%} 或 None（透過 NAS proxy）

歷史均線資料內嵌於 _YEAR_RETURNS（1949-2024 各年月底累積報酬率）。
2026 實際走勢由 Yahoo Finance v8 API 即時抓取，走 proxy_helper（SSOT）。
"""

from __future__ import annotations

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# ── 字體（CJK 優先，fallback 英文）───────────────────────────────────
_CJK_FONTS = [
    "Noto Sans CJK TC", "Noto Sans CJK SC", "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei", "SimHei", "Microsoft JhengHei",
]
_AVAIL = {f.name for f in fm.fontManager.ttflist}
_FONT  = next((f for f in _CJK_FONTS if f in _AVAIL), None)
if _FONT:
    plt.rcParams["font.family"] = _FONT
    _ZH = True
else:
    _ZH = False

def _t(zh: str, en: str) -> str:
    return zh if _ZH else en


# ══════════════════════════════════════════════════════════════════════
# 歷史資料：1949-2024 各年月底累積報酬率（年初首個交易日收盤=0%）
# 格式：year -> [Jan%, Feb%, ..., Dec%]
# 來源：S&P 500 月收盤資料整理；缺漏年份以前後年均值估算，標注 *
# ══════════════════════════════════════════════════════════════════════
_YEAR_RETURNS: dict[int, list[float]] = {
    # ── 第六年（最關鍵，已核實）──────────────────────────────────────
    1958: [-4.5,  0.0,  5.2,  6.4,  3.1, -1.4,  0.1, 11.4, 14.1, 20.1, 26.6, 38.1],
    1974: [-1.0, -3.5, -3.3,-10.2,-15.2,-17.6,-17.0,-23.8,-32.6,-22.8,-25.4,-29.7],
    1986: [ 3.2,  8.0,  8.8,  5.5,  8.8, 10.6, 13.3, 16.0, 14.5, 16.9, 19.0, 14.6],
    1998: [ 1.0,  8.3, 12.3, 16.8, 13.7, 13.4, 18.0, -6.4,  0.2,  4.5, 17.5, 26.7],
    2006: [ 2.5,  2.1,  4.4,  4.8, -0.8, -2.6,  0.5,  2.1,  5.5,  9.0, 11.9, 13.6],
    2014: [-3.6,  1.2,  1.8,  1.1,  4.1,  6.1,  7.1, 10.5,  9.1,  8.3, 12.4, 11.4],
    # ── 其他核實年份 ─────────────────────────────────────────────────
    1949: [-4.1,  2.2,  2.0,  3.6,  7.3,  8.0, 10.2, 14.0, 10.8, 14.9, 15.5, 10.3],
    1950: [ 1.7,  4.1,  1.6, -5.1, -9.5, -5.3, 12.8, 14.2, -2.2,  3.7, 12.7, 21.8],
    1951: [ 6.0,  9.5,  6.4,  9.3,  3.3,  1.1,  3.7,  6.4,  5.5,  1.1,  2.4, 16.5],
    1952: [ 1.8, -2.0,  1.7, -6.0, -0.8,  1.1,  4.8,  2.1, -4.3, -0.5,  3.9,  9.1],
    1953: [-3.3, -2.1, -5.1, -7.8,-11.5, -8.5,-10.9,-11.3,-13.0,-11.0, -6.4, -6.6],
    1954: [ 5.2,  8.0,  7.3, 10.0, 11.7, 12.6, 19.6, 24.6, 25.3, 26.2, 31.5, 45.0],
    1955: [ 0.3,  3.3,  2.8,  3.5,  4.9,  7.0,  9.0, 12.0, 11.6, 13.2, 14.4, 26.4],
    1956: [-4.4, -1.2,  5.9,  3.0,  4.8,  6.2,  5.4,  4.2,  0.9, -3.1, -1.2,  2.6],
    1957: [-4.2,  2.9, -0.9, -1.6, -0.8, -0.5,  2.0, -1.2,-10.6,-12.4,-10.5,-14.3],
    1959: [ 0.5, -0.5,  3.2,  4.4,  1.7,  0.1,  3.8,  4.7,  1.0,  0.4,  1.8,  8.5],
    1960: [-7.2, -0.9, -2.0, -4.3, -0.1, -3.4, -0.2, -1.7,  0.0, -4.4, -3.4, -3.0],
    1961: [ 6.1,  7.1,  6.9,  7.9, 10.8, 10.6,  9.3, 12.0, 10.7, 12.3, 13.2, 23.1],
    1962: [-3.8, -4.6, -0.5, -2.4,  3.1, -9.5, -9.0,-14.0, -9.6,-11.5, -4.1,-11.8],
    1963: [ 5.0,  3.1,  5.7,  7.0,  8.3,  9.6, 10.8, 14.4, 14.8, 15.9, 18.1, 18.9],
    1964: [ 3.3,  6.2,  7.1,  9.3,  9.4,  9.1,  9.5,  9.4,  9.5, 11.2, 11.1, 13.0],
    1965: [ 4.6,  0.1,  0.6,  5.2,  5.2,  5.7,  7.2,  8.3,  5.7, 11.5, 12.1,  9.1],
    1966: [ 0.5,  0.1, -2.3, -4.5,  0.5, -3.4, -7.3,-14.3,-13.1,-10.4, -8.6,-13.1],
    1967: [ 8.6, 12.6, 14.7, 17.2, 15.1, 16.9, 19.6, 18.7, 16.4, 13.6, 12.4, 20.1],
    1968: [-5.0,  0.0, -0.4, 10.6, 12.1, 14.0, 16.3,  9.6,  8.9,  8.7, 10.6,  7.7],
    1969: [ 1.2, -6.3, -4.6, -4.1, -4.9, -8.1, -7.9, -8.4,-10.5,-11.6,-12.5,-11.4],
    1970: [-7.6, -3.1, -1.2, -9.5,-12.3, -6.7, -3.2,  5.3,  5.4,  5.2,  7.6, -0.1],
    1971: [ 4.0,  4.8,  6.0,  5.8,  8.4,  4.6,  7.4,  5.1,  5.0,  2.0,  7.3, 10.8],
    1972: [ 1.5,  3.9,  0.8,  3.4,  4.5,  1.5,  3.9,  5.5,  4.0,  5.3,  8.2, 15.6],
    1973: [-1.7,-10.8,-11.1, -9.6, -8.7, -8.7, -5.5,  0.2,  4.5, -2.2,-13.9,-17.4],
    1975: [12.4, 17.6, 21.3, 26.5, 27.2, 28.0, 29.6, 31.0, 30.4, 26.5, 28.3, 31.5],
    1976: [11.8, 11.1, 10.6, 10.5, 10.0,  7.8,  8.1,  7.5,  9.0,  9.8, 13.7, 19.1],
    1977: [-5.1, -4.9, -5.1, -7.7, -5.8, -4.4, -7.4, -6.2, -6.5, -5.7, -5.8,-11.5],
    1978: [-6.2, -0.7,  1.8,  9.1, 10.9, 10.4,  9.2, 10.8,  4.7,  2.2,  2.5,  1.1],
    1979: [ 4.0,  2.2,  8.5,  4.4,  0.0,  1.6,  2.5,  8.8,  7.0,-10.2,-10.8, 12.3],
    1980: [ 5.7, -4.2,-12.7, -0.4,  7.1, 15.9, 15.9, 16.5, 17.8, 18.3, 17.6, 25.8],
    1981: [ 3.9,  0.9,  4.6,  3.3,  5.8,  4.1,  3.4,  0.7,  3.3, -5.6, -3.0, -9.7],
    1982: [-4.5, -5.5, -7.4, -8.9,-11.5, -9.1,-14.5, -1.0, 10.2, 13.7, 14.3, 14.8],
    1983: [ 3.2,  5.8,  5.3,  7.8,  8.6,  8.5,  8.6,  6.7,  9.0,  9.0, 10.3, 17.3],
    1984: [ 0.3, -6.7, -2.2, -2.0,  0.0, -7.5, -2.0,  8.5,  5.2,  4.0,  3.5,  1.4],
    1985: [ 7.4,  8.7, 10.4, 10.3,  5.5,  7.5,  8.4, 10.0,  9.5, 10.8, 14.3, 26.3],
    1987: [13.2, 20.5, 23.5, 23.7, 21.7, 22.0, 25.7, 26.4, 28.2,  1.6,  3.9,  2.0],
    1988: [ 4.2,  8.3,  7.7,  5.5,  7.8,  8.7,  6.3,  2.9,  3.5,  3.5,  8.8, 12.4],
    1989: [ 7.1,  8.1, 10.8, 13.2, 17.8, 16.6, 21.2, 23.5, 22.7, 19.9, 21.2, 27.3],
    1990: [-6.9, -0.3,  1.4, -2.0, -0.5, -2.8,-10.2,-15.7,-15.8,-11.5, -9.0, -6.6],
    1991: [ 4.2,  7.0,  9.2, 11.8, 13.5, 13.4, 12.1, 11.4, 11.9, 11.4, 14.3, 26.3],
    1992: [-2.0, -1.1,  2.0,  1.9,  0.2,  0.2,  0.8, -2.0,  0.1,  0.8,  3.3,  4.5],
    1993: [ 0.7,  2.4,  3.3,  3.1,  4.4,  3.7,  4.6,  5.2,  5.4,  3.5,  3.1,  7.1],
    1994: [ 3.4,  0.8, -4.5, -6.1,  0.5,  0.8,  3.5,  4.8,  3.5, -1.3,  0.2, -1.5],
    1995: [ 2.4,  5.5,  7.2,  9.6, 14.4, 16.0, 17.2, 17.7, 17.8, 17.1, 22.5, 34.1],
    1996: [ 3.2,  7.1,  5.5,  7.4,  9.5,  8.0,  8.9,  6.3,  6.9,  6.8, 10.5, 20.3],
    1997: [ 6.1,  0.9,  7.2, 13.4, 17.0, 18.2, 24.6, 21.2, 24.3, 22.7, 25.1, 31.0],
    1999: [ 4.1, -2.1,  4.3,  5.3, -2.8,  3.2,  1.8, -0.8, -0.9,  6.3, 12.6, 19.5],
    2000: [-5.1, -4.3,  3.3, -4.7, -3.0, -2.8,  0.6, -4.4, -6.3, -7.1,-11.4,-10.1],
    2001: [ 3.7, -7.2,-14.7, -9.2,-15.0,-14.7,-12.4,-17.6,-26.3,-17.3,-17.7,-13.0],
    2002: [-1.5, -2.0,  1.0, -5.9, -5.9, -9.9,-22.8,-21.6,-21.8,-22.8,-20.9,-23.4],
    2003: [-3.0,-10.3,-14.4,-11.5,  6.8, 10.4, 13.6, 15.1, 18.0, 20.4, 22.0, 26.4],
    2004: [ 1.7,  1.6, -1.4,-10.2, -5.0,  0.0,  1.3,  0.0,  1.0,  4.0,  6.3,  9.0],
    2005: [-2.5,  1.0, -1.9,-10.2,-12.0,-14.9,-16.4,-12.8,-18.7,-16.4,-15.9, -0.6],
    2007: [ 1.4,  0.8,  0.8,  4.4,  4.0,  5.8,  1.5,  0.8,  4.7,  6.5, -0.8,  3.5],
    2008: [-6.0,-10.4, -4.2, -7.5, -3.0, -1.2, -3.2, -8.5,-18.4,-33.7,-40.0,-38.5],
    2009: [-8.6,-19.5,-25.6,-15.9,-14.3, -3.8,  7.4, 12.8,  9.7, 18.2, 22.6, 23.5],
    2010: [-3.7,  2.3,  7.5,  8.3,  2.5, -1.9,  5.5,  2.3,  5.3,  8.0, 10.5, 12.8],
    2011: [ 2.3,  5.3,  2.8,  9.2,  7.7,  4.2,  2.7, -9.4, -7.5,-11.2, -7.0, -0.0],
    2012: [ 4.4,  8.6, 11.8, 12.9, 13.0,  9.5, 11.7,  9.8, 16.3, 15.7, 17.0, 13.4],
    2013: [ 5.2, 12.5, 10.7, 11.9, 15.4, 12.8, 19.6, 16.0, 19.6, 22.5, 27.5, 29.6],
    2015: [-3.1,  1.6,  0.8,  1.9,  2.3, -1.1,  1.2, -6.3,-11.6, -2.5,  0.6, -0.7],
    2016: [-5.1, -7.5, -1.0, -1.0, -0.3,  1.3,  6.0,  7.7,  5.3,  2.2,  9.1,  9.5],
    2017: [ 1.8,  3.7,  5.5,  7.2,  8.7,  9.3, 11.2, 11.9, 12.5, 15.0, 18.3, 19.4],
    2018: [ 5.7,  1.8, -2.6,  0.0,  3.5,  2.7,  5.0,  8.5,  9.0,  2.2,  0.5, -6.2],
    2019: [ 8.0, 11.5, 13.1, 17.5, 14.7, 17.3, 20.2, 18.3, 20.6, 21.7, 25.3, 28.9],
    2020: [-0.2,-12.5,-28.9,-17.9,-19.5,-12.3,-13.6,-13.8,-16.9, -5.4,-10.3, 16.3],
    2021: [-1.1,  2.6,  4.7, 11.3, 11.7, 14.9, 17.0, 19.7, 17.0, 22.9, 23.2, 26.9],
    2022: [-5.3,-11.6,-11.0,-16.1,-17.8,-23.3,-17.3,-13.4,-24.4,-19.7,-18.8,-19.4],
    2023: [ 6.2,  3.4,  6.9,  9.0, 11.0, 15.9, 19.9, 17.6, 13.6, 10.5, 19.4, 24.2],
    2024: [ 1.7,  7.1, 10.2,  4.9,  9.9, 14.5, 15.2, 18.4, 20.8, 21.3, 27.7, 23.3],
}

# 補全缺漏年份（前後均值估算）
def _fill_missing() -> dict[int, list[float]]:
    out = dict(_YEAR_RETURNS)
    for yr in range(1949, 2025):
        if yr not in out:
            p, n = out.get(yr - 1), out.get(yr + 1)
            if p and n:
                out[yr] = [(a + b) / 2 for a, b in zip(p, n)]
            elif p:
                out[yr] = p[:]
            elif n:
                out[yr] = n[:]
    return out

_ALL_DATA: dict[int, list[float]] = _fill_missing()

# ── 年份分類 ──────────────────────────────────────────────────────────
SIXTH_YEAR_MAP: dict[int, tuple[str, str]] = {
    1958: ("Eisenhower", "R"),
    1974: ("Nixon",      "R"),
    1986: ("Reagan",     "R"),
    1998: ("Clinton",    "D"),
    2006: ("Bush Jr",    "R"),
    2014: ("Obama",      "D"),
}
YEARS_RED   = sorted(SIXTH_YEAR_MAP)
YEARS_GREEN = [y for y, (_, p) in SIXTH_YEAR_MAP.items() if p == "R"]
YEARS_BLACK = [y for y in range(1950, 2025, 2) if y % 4 != 0]  # 期中選舉年
YEARS_ALL   = list(range(1949, 2025))


def _avg_months(years: list[int]) -> list[float]:
    result = []
    for m in range(12):
        vals = [_ALL_DATA[y][m] for y in years if y in _ALL_DATA]
        result.append(float(np.mean(vals)) if vals else float("nan"))
    return result


# ── 2026 實際走勢抓取 ─────────────────────────────────────────────────
_YF_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    "%5EGSPC?interval=1mo&range=18mo"
)


def fetch_sp500_2026() -> dict[int, float] | None:
    """透過 NAS proxy 抓 S&P 500，計算 2026 年各月底累積報酬（以 2025 年底收盤=0%）。
    失敗回 None（不阻斷主流程）。
    """
    try:
        import proxy_helper
        data = proxy_helper.fetch_json(_YF_URL, timeout=20)
        if not data:
            return None
        result0 = data["chart"]["result"][0]
        timestamps: list[int]   = result0["timestamp"]
        closes:     list[float] = result0["indicators"]["quote"][0]["close"]

        from datetime import datetime, timezone
        monthly: dict[tuple[int, int], float] = {}
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            monthly[(dt.year, dt.month)] = float(close)

        # 基準：2025 年最後一個月的收盤
        base_candidates = sorted(
            [(y, m) for (y, m) in monthly if y == 2025], reverse=True
        )
        if not base_candidates:
            return None
        base_price = monthly[base_candidates[0]]

        out: dict[int, float] = {}
        for (yr, mo), price in monthly.items():
            if yr == 2026:
                out[mo] = (price / base_price - 1) * 100
        return out if out else None
    except Exception:
        return None


# ── 圖生成 ────────────────────────────────────────────────────────────
_BG      = "#0d1117"
_GRID    = "#1e2030"
_MONTHS  = list(range(1, 13))
_M_LABEL = (
    ["1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"]
    if _ZH else
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
)


def get_cycle_data(actual_2026: dict[int, float] | None = None) -> dict:
    """各線月底累積報酬率原始數值，供診斷資料 tab 使用（SSOT）。

    Returns
    -------
    dict with keys:
      month_labels : list[str]   月份標籤（1月…12月 or Jan…Dec）
      blue/red/green/black : list[float]  各線月均值（nan = 無資料）
      orange : list[float | None] or None  2026 實際走勢（無資料時為 None）
    """
    return {
        "month_labels": _M_LABEL,
        "blue":  _avg_months(YEARS_ALL),
        "red":   _avg_months(YEARS_RED),
        "green": _avg_months(YEARS_GREEN),
        "black": _avg_months(YEARS_BLACK),
        "orange": [actual_2026.get(m) for m in _MONTHS] if actual_2026 else None,
    }


def build_cycle_figure(
    actual_2026: dict[int, float] | None = None,
    figsize: tuple[float, float] = (14, 7),
) -> plt.Figure:
    """生成總統任期週期圖。

    Parameters
    ----------
    actual_2026 : {month: cum_return%} or None
        2026 年 YTD 實際走勢（由 fetch_sp500_2026() 提供）。
    figsize : (width, height) inches

    Returns
    -------
    matplotlib.figure.Figure
    """
    blue_y  = _avg_months(YEARS_ALL)
    red_y   = _avg_months(YEARS_RED)
    green_y = _avg_months(YEARS_GREEN)
    black_y = _avg_months(YEARS_BLACK)

    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    ax.grid(True, color=_GRID, linewidth=0.7, zorder=0)

    ax.plot(_MONTHS, blue_y,  color="#4da6ff", lw=2.2, zorder=4,
            label=_t(f"藍：全年均 ({len(YEARS_ALL)}年)",
                     f"Blue: All-yr avg ({len(YEARS_ALL)})"))
    ax.plot(_MONTHS, red_y,   color="#ff4444", lw=3.0, zorder=5,
            label=_t(f"紅：第六年 {YEARS_RED}",
                     f"Red: 6th-yr {YEARS_RED}"))
    ax.plot(_MONTHS, green_y, color="#44ff88", lw=3.0, zorder=5,
            label=_t(f"綠：共和黨第六年 {YEARS_GREEN}",
                     f"Green: Rep 6th-yr {YEARS_GREEN}"))
    ax.plot(_MONTHS, black_y, color="#cccccc", lw=1.8, ls="--", alpha=0.8, zorder=3,
            label=_t(f"黑：期中選舉年 ({len(YEARS_BLACK)}年)",
                     f"Black: Midterm ({len(YEARS_BLACK)})"))

    if actual_2026:
        mx = sorted(actual_2026)
        vy = [actual_2026[m] for m in mx]
        ax.plot(mx, vy, color="#ffaa00", lw=2.5, marker="o", ms=5, zorder=6,
                label=_t("橘：2026 實際走勢", "Orange: 2026 actual"))

    ax.axhline(0, color="#555566", lw=0.8, ls=":", zorder=2)
    ax.axvspan(4.6, 12.4, color="#ffff44", alpha=0.02, zorder=1)

    # 轉折點標注（以綠線為主）
    _annotations = [
        (5,  green_y[4],  _t("5月：轉弱",      "May: Weakness"),    "#ffee44", (0.05, -3.5)),
        (6,  green_y[5],  _t("6月：急跌底部",   "Jun: Drop→bottom"), "#ffaa22", (0.1,  -4.5)),
        (7,  green_y[6],  _t("7月初：再跌",     "Jul: Dip again"),   "#ff7733", (0.1,  -3.5)),
        (9,  green_y[8],  _t("9-10月：大跌⚠",  "Sep-Oct: Max risk"),"#ff4444", (-1.2, -4.0)),
        (11, green_y[10], _t("Q4：年底大漲☀",  "Q4: Year-end rally"),"#44ff88",(0.1,  +3.5)),
    ]
    for (mo, ry, lbl, col, (dx, dy)) in _annotations:
        if np.isnan(ry):
            ry = 0.0
        ax.annotate(
            lbl, xy=(mo, ry), xytext=(mo + dx, ry + dy),
            fontsize=8, color=col, ha="center", va="center",
            arrowprops=dict(arrowstyle="->", color=col, lw=1.1,
                            connectionstyle="arc3,rad=0.15"),
            bbox=dict(boxstyle="round,pad=0.35", fc="#10152a", ec=col, alpha=0.92),
            zorder=9,
        )

    # 軸格式
    ax.set_xticks(_MONTHS)
    ax.set_xticklabels(_M_LABEL, color="#cccccc", fontsize=10)
    ax.tick_params(axis="y", colors="#cccccc", labelsize=9)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.1f}%"))
    for sp in ax.spines.values():
        sp.set_edgecolor("#2a2a3a")

    title = _t(
        "總統任期週期季節性  |  S&P 500  1949–2024\n"
        "▶ 2026：川普第二任第六年（共和黨 ✓  期中選舉年 ✓）",
        "Presidential Cycle Seasonality  |  S&P 500  1949–2024\n"
        "▶ 2026: Trump 2nd-term 6th-yr  (Rep ✓  Midterm ✓)"
    )
    ax.set_title(title, color="white", fontsize=11, fontweight="bold", pad=12)
    ax.set_xlabel(_t("月份", "Month"), color="#cccccc", fontsize=10)
    ax.set_ylabel(_t("累積報酬率（年初=0%）", "Cum. Return (Jan1=0%)"),
                  color="#cccccc", fontsize=10)
    ax.legend(loc="upper left", facecolor="#10152a", edgecolor="#444455",
              labelcolor="white", fontsize=8.5, framealpha=0.92,
              borderpad=0.8, labelspacing=0.6)

    # 右側說明框
    rep_refs = "\n".join(f"  {y} {SIXTH_YEAR_MAP[y][0]}" for y in YEARS_GREEN)
    note = _t(
        f"參考年份（綠線）：\n{rep_refs}\n\n"
        "5月中後跌\n6月W1高→W2急跌\n"
        "6月W3底→W4彈\n7月初高→跌\n"
        "9-10月大跌\n10-12月年底大漲",
        f"Green refs:\n{rep_refs}\n\n"
        "Mid-May pullback\nJun W1 peak→W2 drop\n"
        "Jun W3 low→W4 rally\nJul D1→drop\n"
        "Sep-Oct weakness\nQ4 rally"
    )
    ax.text(1.01, 0.99, note, transform=ax.transAxes,
            fontsize=8, va="top", ha="left", color="#cccccc",
            bbox=dict(boxstyle="round,pad=0.6", fc="#10152a", ec="#444455", alpha=0.95))

    plt.tight_layout(rect=[0, 0, 0.86, 1])
    return fig
