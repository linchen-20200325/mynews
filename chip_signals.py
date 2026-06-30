"""chip_signals.py — 個股籌碼面訊號(三大法人買賣超)的單一真相源。

只服務「個股盯盤」LINE 推播:每檔在技術面後多附一行籌碼面白話文字
(外資/投信當日買賣超張數 + 外資連買/連賣天數)。資料源:臺灣證交所 T86
(個股三大法人買賣超,免金鑰、官方 JSON),走既有 ``proxy_helper`` 中繼。

效率設計:T86 一次回「整個市場某一交易日」所有個股,故批次抓最近數個交易日
各一次(而非逐檔逐月),再為清單內各代號切出當日買賣超與連買/連賣天數;
清單再長,網路成本仍只跟「回看天數」成正比。

註(同 tech_signals / earnings_fetcher 的上市/上櫃慣例):T86 僅含上市(TWSE),
    上櫃(TPEx)另有來源,屬後續擴充。早上排程跑時,當日盤後資料尚未公告,
    最新一筆自然落在「前一個交易日」(抓不到的日期 stat≠OK 會被略過)。
    抓不到或近期無資料的代號,``signals_for`` 不收錄 → 該檔靜默略過籌碼面那行,
    不影響消息面/技術面/月營收。

純資料/計算層,不畫任何 Streamlit 元件;早上排程批次跑一次,故不需 st.cache。
"""

from __future__ import annotations

import time

import config  # 環境變數讀取 SSOT

import tz_utils

# T86:三大法人買賣超「個股」明細(selectType=ALL 回全市場個股),欄位以名稱定位(抗格式漂移)
TWSE_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86"

_DEFAULT_DAYS = 6      # 回看交易日數(足夠呈現連買/連賣最多 ~5 天的趨勢)
_HTTP_TIMEOUT = 25
_THROTTLE_SEC = 1.0    # 尊重 TWSE 流量限制,逐日抓之間小睡
_SHARES_PER_LOT = 1000  # 1 張 = 1000 股


def _to_lots(shares) -> int | None:
    """把 T86 字串股數(可能含千分位逗號或 '--')轉「張」整數;無效回 None。"""
    try:
        return round(float(str(shares).replace(",", "").strip()) / _SHARES_PER_LOT)
    except (TypeError, ValueError):
        return None



def _col_index(fields: list[str], *needles: str) -> int | None:
    """回第一個「名稱同時含所有 needles」的欄位索引;找不到回 None(抗 T86 欄名微調)。"""
    for i, name in enumerate(fields):
        if all(n in name for n in needles):
            return i
    return None


def fetch_t86_day(date_str: str, log=print) -> dict[str, dict] | None:
    """抓某一交易日 T86;回 {ticker: {"foreign":張, "trust":張, "total":張}}。

    當日無資料(假日/尚未公告)或抓取失敗回 None。買賣超皆已轉「張」(正買超、負賣超)。
    """
    import proxy_helper

    resp = proxy_helper.fetch_url(
        TWSE_T86,
        params={"date": date_str, "selectType": "ALL", "response": "json"},
        timeout=_HTTP_TIMEOUT,
    )
    if resp is None:
        return None
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — 非 JSON(被擋/維護頁)→ 當作無資料
        return None
    if data.get("stat") != "OK" or not data.get("data"):
        return None

    fields = data.get("fields", [])
    # 外資=外陸資買賣超(不含外資自營商);投信=投信買賣超;合計=三大法人買賣超
    i_code = _col_index(fields, "證券代號")
    # 外資:優先「外陸資買賣超(不含外資自營商)」,退而求其次任一含「外…買賣超」者
    i_fore = _col_index(fields, "外陸資買賣超")
    if i_fore is None:
        i_fore = _col_index(fields, "外", "買賣超")
    i_trust = _col_index(fields, "投信買賣超")
    i_total = _col_index(fields, "三大法人買賣超")
    if i_code is None or i_fore is None or i_trust is None or i_total is None:
        log(f"  籌碼面:T86 欄位定位失敗({date_str}),欄名={fields}")
        return None

    out: dict[str, dict] = {}
    for r in data["data"]:
        if not isinstance(r, (list, tuple)) or len(r) <= i_total:
            continue
        ticker = str(r[i_code]).strip()
        if not ticker:
            continue
        out[ticker] = {
            "foreign": _to_lots(r[i_fore]),
            "trust": _to_lots(r[i_trust]),
            "total": _to_lots(r[i_total]),
        }
    return out


def fetch_recent(tickers: list[str], days: int, log=print) -> dict[str, list[dict]]:
    """批次抓最近 days 個交易日 T86,切出清單內各代號的時序(由舊到新)。

    回 {ticker: [{"date","foreign","trust","total"}, ...]};只收錄該代號當日有出現者。
    """
    want = {str(t).strip() for t in tickers if str(t).strip()}
    if not want:
        return {}
    series: dict[str, list[dict]] = {t: [] for t in want}
    got = 0
    for d in tz_utils.iter_trading_days(days * 2 + 4):
        if got >= days:
            break
        date_str = d.strftime("%Y%m%d")
        day = fetch_t86_day(date_str, log=log)
        time.sleep(_THROTTLE_SEC)
        if not day:
            continue  # 假日/未公告/失敗 → 不計入交易日,續抓更早一天
        got += 1
        for t in want:
            row = day.get(t)
            if row is not None:
                series[t].append({"date": date_str, **row})
    # iter_trading_days 由新到舊,故各代號時序反轉成由舊到新
    for t in series:
        series[t].reverse()
    return series


def _streak(series: list[dict], key: str = "foreign") -> tuple[int, int]:
    """由最新一筆往回數連續同號(非零)天數;回 (sign, days)。sign∈{1,-1,0}。"""
    sign = 0
    days = 0
    for row in reversed(series):
        v = row.get(key)
        if not v:  # None 或 0 → 中斷連續
            break
        s = 1 if v > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            break
        days += 1
    return sign, days


def signal_text(series: list[dict]) -> str | None:
    """把某代號的籌碼時序組成一行 LINE 白話文字;無有效最新一筆回 None。"""
    if not series:
        return None
    latest = series[-1]
    fore, trust, total = latest.get("foreign"), latest.get("trust"), latest.get("total")
    if fore is None and trust is None and total is None:
        return None

    parts: list[str] = []
    if fore is not None:
        seg = f"外資{fore:+,}張"
        sign, days = _streak(series, "foreign")
        if days >= 2:  # 連 2 天以上才標,單日不囉嗦
            seg += f"(連{days}{'買' if sign > 0 else '賣'})"
        parts.append(seg)
    if trust is not None:
        parts.append(f"投信{trust:+,}張")
    if total is not None:
        parts.append(f"三大法人{total:+,}張")
    if not parts:
        return None
    return "💰 籌碼 " + "｜".join(parts)


def signals_for(stocks: list[dict], days: int | None = None, log=print) -> dict[str, str]:
    """逐檔算籌碼面文字;回 {ticker: 文字}。抓不到/近期無資料的代號不收錄(該檔靜默略過)。"""
    days = days or config.env_int("WATCH_CHIP_DAYS", _DEFAULT_DAYS)
    tickers = [str(s.get("ticker", "")).strip() for s in stocks if s.get("ticker")]
    if not tickers:
        return {}
    try:
        series = fetch_recent(tickers, days=days, log=log)
    except Exception as exc:  # noqa: BLE001 — 整批失敗不影響消息面/技術面/財報
        log(f"  籌碼面整批抓取失敗:{exc}")
        return {}
    out: dict[str, str] = {}
    for ticker in tickers:
        text = signal_text(series.get(ticker, []))
        if text:
            out[ticker] = text
    return out
