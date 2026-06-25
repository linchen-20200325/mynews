"""tech_signals.py — 個股技術面訊號(日K → 均線/乖離/KD/RSI)的單一真相源。

只服務「個股盯盤」LINE 推播:每檔在消息面後多附一行技術面白話文字。
資料源:臺灣證交所 STOCK_DAY(個股歷史日K,免金鑰、官方 JSON),走既有
``proxy_helper`` 中繼(境外 IP 被擋時自動降級直連)。

註(同 earnings_fetcher.py 的上市/上櫃慣例):上櫃(TPEx)個股歷史日K另有來源,
    屬後續擴充;目前先涵蓋上市。抓不到日K或資料不足以算 20MA 的代號,
    ``signals_for`` 不會收錄 → 推播時該檔靜默略過技術面那行,不影響消息面/月營收。

純資料/計算層,不畫任何 Streamlit 元件;早上排程批次跑一次,故不需 st.cache。
"""

from __future__ import annotations

import os
import time

import tz_utils  # 台灣時區 SSOT(決定要抓哪幾個月)

TWSE_STOCK_DAY = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"

_DEFAULT_MONTHS = 4   # 約 80 個交易日,足夠算 60MA / KD(9) / RSI(14)
_HTTP_TIMEOUT = 30
_THROTTLE_SEC = 1.0   # 尊重 TWSE 流量限制(每幾秒數次),逐月抓之間小睡


def _f(value) -> float | None:
    """把 TWSE 字串數字(可能含千分位逗號或 '--')轉 float;無效回 None。"""
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _iter_months(months: int):
    """由舊到新產出 (year, month) 字串 'YYYYMM01',以台灣時間當月為錨。"""
    now = tz_utils.taiwan_now()
    y, m = now.year, now.month
    seq = []
    for _ in range(months):
        seq.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    for yy, mm in reversed(seq):  # 舊 → 新,結果自然按時間排序
        yield f"{yy}{mm:02d}01"


def fetch_daily_k(ticker: str, months: int = _DEFAULT_MONTHS, log=print) -> list[dict]:
    """抓個股近 months 個月日K(由舊到新);回 [{date, open, high, low, close, volume}]。

    volume = 當日成交股數(VCP 量縮判斷用;技術面均線/KD/RSI 不讀它,故新增不影響)。
    逐月呼叫 STOCK_DAY(一次回一個月),去重後依日期排序。任一月抓失敗就略過該月。
    """
    import proxy_helper

    rows: list[dict] = []
    seen: set[str] = set()
    for date_param in _iter_months(months):
        resp = proxy_helper.fetch_url(
            TWSE_STOCK_DAY,
            params={"response": "json", "date": date_param, "stockNo": ticker},
            timeout=_HTTP_TIMEOUT,
        )
        time.sleep(_THROTTLE_SEC)
        if resp is None:
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 — 非 JSON(被擋/維護頁)→ 略過該月
            continue
        if data.get("stat") != "OK":
            continue
        for r in data.get("data", []):
            # 欄位:日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數
            if not isinstance(r, (list, tuple)) or len(r) < 7:
                continue
            day = str(r[0]).strip()
            if day in seen:
                continue
            o, h, lo, c = _f(r[3]), _f(r[4]), _f(r[5]), _f(r[6])
            if c is None or c <= 0:
                continue  # 停牌/無成交 → 不以 0 充數
            seen.add(day)
            rows.append({"date": day, "open": o, "high": h, "low": lo, "close": c,
                         "volume": _f(r[1])})  # r[1]=成交股數;無效→None
    rows.sort(key=lambda x: x["date"])  # ROC 日期字串('115/06/03')同世紀內可直接字典序
    return rows


def _sma(vals: list[float], n: int) -> float | None:
    """最後 n 筆的簡單移動平均;不足 n 筆回 None。"""
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI;資料不足回 None。全跌(avg_loss=0)回 100。"""
    if len(closes) <= period:
        return None
    gain = loss = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gain += ch if ch > 0 else 0.0
        loss += -ch if ch < 0 else 0.0
    avg_gain, avg_loss = gain / period, loss / period
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + (ch if ch > 0 else 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + (-ch if ch < 0 else 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _kd(rows: list[dict], n: int = 9) -> tuple[float | None, float | None]:
    """KD(9):RSV→K(2/3 舊+1/3 RSV)→D(2/3 舊+1/3 K),初值 50。不足 n 筆回 (None, None)。"""
    if len(rows) < n:
        return None, None
    k = d = 50.0
    for i in range(n - 1, len(rows)):
        window = rows[i - n + 1: i + 1]
        highs = [r["high"] for r in window if r["high"] is not None]
        lows = [r["low"] for r in window if r["low"] is not None]
        if not highs or not lows:
            continue
        hi, lo, close = max(highs), min(lows), rows[i]["close"]
        rsv = 50.0 if hi == lo else (close - lo) / (hi - lo) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
    return k, d


def compute_signals(rows: list[dict]) -> dict | None:
    """由日K算技術指標;不足 20 筆(算不了 20MA)視為無技術面回 None。"""
    closes = [r["close"] for r in rows]
    if len(closes) < 20:
        return None
    ma20, ma60, close = _sma(closes, 20), _sma(closes, 60), closes[-1]
    k, d = _kd(rows, 9)
    return {
        "close": close,
        "ma20": ma20,
        "ma60": ma60,
        "bias20": (close - ma20) / ma20 * 100 if ma20 else None,
        "rsi": _rsi(closes, 14),
        "k": k,
        "d": d,
    }


def signal_text(sig: dict) -> str:
    """把指標組成一行 LINE 白話技術面文字。"""
    close = sig["close"]
    parts = [f"收{close:g}"]
    ma20, ma60, bias = sig.get("ma20"), sig.get("ma60"), sig.get("bias20")
    if ma20:
        tag = "✅站上" if close >= ma20 else "❌跌破"
        parts.append(f"20MA{tag}" + (f"({bias:+.1f}%)" if bias is not None else ""))
    if ma60:
        parts.append("60MA" + ("✅" if close >= ma60 else "❌"))
    if sig.get("k") is not None and sig.get("d") is not None:
        parts.append(f"KD {sig['k']:.0f}/{sig['d']:.0f}")
    if sig.get("rsi") is not None:
        parts.append(f"RSI {sig['rsi']:.0f}")
    return "📊 技術 " + "｜".join(parts)


def signals_for(stocks: list[dict], months: int | None = None, log=print) -> dict[str, str]:
    """逐檔算技術面文字;回 {ticker: 文字}。抓不到/資料不足的代號不收錄(該檔靜默略過)。"""
    months = months or int(os.environ.get("WATCH_TECH_MONTHS", str(_DEFAULT_MONTHS)))
    out: dict[str, str] = {}
    for s in stocks:
        ticker = str(s.get("ticker", "")).strip()
        if not ticker:
            continue
        try:
            sig = compute_signals(fetch_daily_k(ticker, months=months, log=log))
            if sig:
                out[ticker] = signal_text(sig)
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不影響其他檔
            log(f"  技術面 {ticker} 計算失敗:{exc}")
    return out
