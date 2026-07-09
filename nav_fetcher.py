"""nav_fetcher.py — ETF 淨值(NAV)與折溢價的單一真相源(SSOT),fail-loud。

服務「個股盯盤」LINE 推播:對 ETF 代號(台灣一律 00 開頭)附一行『淨值/折溢價』。
個股(如 2330/6770)沒有 NAV 概念,自動略過。

━━ 資料來源與定義 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
淨值 NAV:官方權威來源為**投信投顧公會(SITCA)每日 ETF 淨值**與發行投信官網。
    本模組實作端沿用本庫既接通 proxy 的 **MoneyDJ Basic0004**(同載官方每日淨值
    「50.1500（05/29）」**含日期**),重用 ``etf_profile_fetcher.diagnose``,不另開輪子。
    ⚠️ yfinance 的 navPrice 常延遲且**不帶獨立日期**→ 無法驗證新鮮度,本模組不拿它算折溢價。

折溢價 premium/discount = (市價 − 淨值) / 淨值 × 100%。
    ⚠️ 陷阱:若 NAV 是「上一交易日的舊值」配「今天已變動的市價」→ 算出假的高溢價。
    ★ 本模組**強制比對 NAV 日期與市價日期**:不同日 → 標「NAV 延遲」**不硬算**(fail-loud)。

配息:除息紀錄用 yfinance ``dividends`` 或發行投信官網。

━━ fail-loud 契約(絕不造假)━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
compute_premium 的 status:
    ok         → NAV 與市價同日,回 premium_pct
    stale_nav  → NAV 日期 ≠ 市價日期(NAV 延遲),標記不計算
    no_nav_date→ NAV 沒有日期無法驗證新鮮度,標記不計算
    no_nav     → NAV 抓不到,標記
    no_price   → 市價抓不到,整檔略過

純資料/計算層,不畫任何 Streamlit 元件。沙箱無網路 → 直接跑 ``python nav_fetcher.py``
會用內建確定性樣本示範四種分支(同 reversal_signals 的 mock 模式);接真實資料只需
讓 fetch_nav / fetch_price 回傳真值即可,呼叫端零改動。
"""
from __future__ import annotations

import re

import tz_utils  # 台灣時區 SSOT(NAV/市價日期的年份推斷、今日)

# 台灣 ETF 代號一律 00 開頭(可帶英文尾,如 00679B、00940、00980A)。
_ETF_RE = re.compile(r"^00\d{2,4}[A-Z]?$")

# 折溢價「正常範圍」門檻:|折溢價| ≤ 1% 視為正常,不需急追。
_NORMAL_BAND_PCT = 1.0

# yfinance 除息紀錄回溯天數(涵蓋季配/半年配至少一次)。
_DIVIDEND_LOOKBACK_DAYS = 400


def is_etf(ticker) -> bool:
    """台灣 ETF 判別:代號 00 開頭(4~6 碼,可帶單一英文尾)。個股回 False。"""
    return bool(_ETF_RE.match(str(ticker or "").strip().upper()))


def _parse_valued_date(raw: str, today=None) -> tuple[float | None, str | None]:
    """解析 MoneyDJ『50.1500（05/29）』→ (50.15, '2026-05-29')。

    日期只有 MM/DD,年份以台灣今日推斷:若 MM/DD 落在未來 → 視為去年(跨年防呆)。
    抓不到數字回 (None, None);抓不到日期回 (值, None)。
    """
    if not raw:
        return None, None
    num_m = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw.replace(",", ""))
    value = float(num_m.group(1)) if num_m else None
    date_m = re.search(r"(1[0-2]|0?[1-9])[/\-月.](3[01]|[12][0-9]|0?[1-9])", raw)
    if not date_m:
        return value, None
    mm, dd = int(date_m.group(1)), int(date_m.group(2))
    today = today or tz_utils.taiwan_now().date()
    from datetime import date
    try:
        d = date(today.year, mm, dd)
    except ValueError:
        return value, None
    if d > today:                      # MM/DD 在未來 → 其實是去年的資料
        d = date(today.year - 1, mm, dd)
    return value, d.isoformat()


# ── 官方 NAV(含日期)──────────────────────────────────────────────────

def fetch_nav(ticker: str, proxy: str | None = None, log=print) -> dict | None:
    """抓官方每日淨值 + 其日期。回 {nav, nav_date, source} 或 None(抓不到)。

    實作:重用 etf_profile_fetcher 的 MoneyDJ Basic0004(含官方每日淨值與日期)。
    ⚠️ 絕不造假:任何失敗(無 proxy、境外被擋、欄位缺)一律回 None,由上層 fail-loud。
    要換成 SITCA 官方 API,只需替換本函數內部,回傳同樣的 {nav, nav_date, source}。
    """
    code = str(ticker or "").strip().upper()
    if not is_etf(code):
        return None
    try:
        import etf_profile_fetcher
        etfid = code if code.endswith(".TW") else f"{code}.TW"
        kv = etf_profile_fetcher.diagnose(etfid, proxy=proxy, page="0004")
    except Exception as exc:  # noqa: BLE001 — 無 proxy / 網路失敗 → fail-loud 回 None
        log(f"  NAV {code}: 官方淨值抓取失敗({exc})")
        return None
    raw = kv.get("ETF淨值") or kv.get("淨值") or ""
    nav, nav_date = _parse_valued_date(raw)
    if nav is None:
        log(f"  NAV {code}: 官方頁面無淨值欄位")
        return None
    return {"nav": nav, "nav_date": nav_date, "source": "MoneyDJ(官方每日淨值)"}


# ── 市價(含日期)──────────────────────────────────────────────────────

def fetch_price(ticker: str, log=print) -> dict | None:
    """抓最新市價 + 其交易日。回 {price, price_date} 或 None。

    用 yfinance 日K 最後一根(帶日期,可比對 NAV 新鮮度)。沙箱無網路 → None(fail-loud)。
    """
    code = str(ticker or "").strip().upper()
    yf_symbol = code if code.endswith(".TW") else f"{code}.TW"
    try:
        import yfinance as yf
        hist = yf.Ticker(yf_symbol).history(period="5d")
        if hist is None or hist.empty:
            log(f"  市價 {code}: yfinance 無資料")
            return None
        last = hist.iloc[-1]
        price_date = hist.index[-1].date().isoformat()
        return {"price": float(last["Close"]), "price_date": price_date}
    except Exception as exc:  # noqa: BLE001 — 無網路 / 代理被擋 → fail-loud
        log(f"  市價 {code}: 抓取失敗({exc})")
        return None


# ── 折溢價 fail-loud 狀態機 ───────────────────────────────────────────

def compute_premium(nav_info: dict | None, price_info: dict | None) -> dict:
    """折溢價狀態機。日期不同/無日期/無 NAV 一律標記不計算,絕不硬算假溢價。"""
    if not price_info or price_info.get("price") is None:
        return {"status": "no_price"}
    price = price_info["price"]
    price_date = price_info.get("price_date")
    if not nav_info or nav_info.get("nav") is None:
        return {"status": "no_nav", "price": price, "price_date": price_date}
    nav = nav_info["nav"]
    nav_date = nav_info.get("nav_date")
    common = {"nav": nav, "price": price, "nav_date": nav_date, "price_date": price_date,
              "source": nav_info.get("source")}
    if not nav_date:
        return {"status": "no_nav_date", **common}
    if nav_date != price_date:            # ★ 核心:NAV 舊值配今日市價 → 假溢價,拒算
        return {"status": "stale_nav", **common}
    if nav == 0:
        return {"status": "no_nav", "price": price, "price_date": price_date}
    return {"status": "ok", "premium_pct": (price - nav) / nav * 100.0, **common}


def _premium_verdict(premium_pct: float) -> str:
    """折溢價白話判讀。"""
    if premium_pct > _NORMAL_BAND_PCT:
        return "溢價偏高⚠️(追價風險)"
    if premium_pct < -_NORMAL_BAND_PCT:
        return "折價偏高(潛在撿便宜)"
    return "正常範圍"


# ── 配息(除息紀錄)────────────────────────────────────────────────────

def fetch_dividends(ticker: str, lookback_days: int = _DIVIDEND_LOOKBACK_DAYS,
                    log=print) -> list[dict]:
    """近 lookback_days 的除息紀錄。回 [{ex_date, amount}](新→舊);抓不到回 []。"""
    code = str(ticker or "").strip().upper()
    yf_symbol = code if code.endswith(".TW") else f"{code}.TW"
    try:
        import yfinance as yf
        s = yf.Ticker(yf_symbol).dividends
        if s is None or len(s) == 0:
            return []
        from datetime import timedelta
        cutoff = tz_utils.taiwan_now().date() - timedelta(days=lookback_days)
        out = []
        for ts, amt in s.items():
            d = ts.date()
            if d >= cutoff:
                out.append({"ex_date": d.isoformat(), "amount": float(amt)})
        return sorted(out, key=lambda r: r["ex_date"], reverse=True)
    except Exception as exc:  # noqa: BLE001
        log(f"  配息 {code}: 抓取失敗({exc})")
        return []


# ── LINE 文字(fail-loud)──────────────────────────────────────────────

def nav_line(ticker: str, name: str = "", proxy: str | None = None,
             nav_info: dict | None = None, price_info: dict | None = None,
             fetch: bool = True, log=print) -> str | None:
    """組單檔 ETF 的『淨值/折溢價』一行。非 ETF 或無市價回 None(整檔略過該行)。

    nav_info / price_info 可外部注入(供測試/離線);fetch=True 時缺者才實際抓取,
    fetch=False 則完全用注入值(None 即視為抓不到,不連網)。
    """
    code = str(ticker or "").strip().upper()
    if not is_etf(code):
        return None
    if fetch and nav_info is None:
        nav_info = fetch_nav(code, proxy=proxy, log=log)
    if fetch and price_info is None:
        price_info = fetch_price(code, log=log)
    r = compute_premium(nav_info, price_info)
    st = r["status"]
    if st == "no_price":
        return None                                   # 沒市價,整段免談
    if st == "ok":
        prem = r["premium_pct"]
        return (f"💧 淨值 {r['nav']:.2f}｜市價 {r['price']:.2f}｜"
                f"折溢價 {prem:+.2f}% {_premium_verdict(prem)}（{r['nav_date']}）")
    if st == "stale_nav":
        return (f"💧 ⚠️ NAV 延遲:淨值日 {r['nav_date']} ≠ 市價日 {r['price_date']},"
                f"不計折溢價(避免假溢價)｜淨值 {r['nav']:.2f}／市價 {r['price']:.2f}")
    if st == "no_nav_date":
        return (f"💧 ⚠️ NAV 無日期無法驗證新鮮度,不計折溢價｜"
                f"淨值 {r['nav']:.2f}／市價 {r['price']:.2f}")
    # no_nav
    return f"💧 ⚠️ NAV 抓不到,未計折溢價｜市價 {r.get('price', float('nan')):.2f}"


def nav_lines_for(stocks: list[dict], proxy: str | None = None,
                  log=print) -> dict[str, str]:
    """逐檔算 ETF 淨值/折溢價文字;回 {ticker: 文字}。只收 ETF、只收有市價的檔。

    fail-loud:NAV 過期/抓不到的 ETF **仍收錄**(標記警語),不像技術面那樣靜默略過。
    """
    out: dict[str, str] = {}
    for s in stocks or []:
        ticker = str(s.get("ticker", "")).strip()
        if not is_etf(ticker):
            continue
        try:
            line = nav_line(ticker, name=s.get("name", ""), proxy=proxy, log=log)
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不影響其他檔
            log(f"  NAV {ticker}: 計算失敗:{exc}")
            continue
        if line:
            out[ticker] = line
    return out


# ── 離線確定性 demo(沙箱無網路也能跑)─────────────────────────────────

def _demo_rows() -> list[dict]:
    """四種分支的確定性樣本(市價/NAV 皆帶日期),示範 fail-loud。"""
    return [
        {"ticker": "0050", "name": "元大台灣50",
         "nav": {"nav": 190.30, "nav_date": "2026-07-09", "source": "MoneyDJ(官方每日淨值)"},
         "price": {"price": 190.55, "price_date": "2026-07-09"}},              # ok
        {"ticker": "00878", "name": "國泰永續高股息",
         "nav": {"nav": 22.10, "nav_date": "2026-07-08", "source": "MoneyDJ(官方每日淨值)"},
         "price": {"price": 22.75, "price_date": "2026-07-09"}},               # stale_nav(假溢價陷阱)
        {"ticker": "00929", "name": "復華台灣科技優息",
         "nav": {"nav": 18.40, "nav_date": None, "source": "MoneyDJ(官方每日淨值)"},
         "price": {"price": 18.52, "price_date": "2026-07-09"}},               # no_nav_date
        {"ticker": "00940", "name": "元大台灣價值高息",
         "nav": None,
         "price": {"price": 9.88, "price_date": "2026-07-09"}},                # no_nav
        {"ticker": "2330", "name": "台積電",
         "nav": None, "price": {"price": 1085.0, "price_date": "2026-07-09"}}, # 個股 → 略過
    ]


def demo() -> None:
    """離線示範:對四種 NAV 狀態各印一行,證明 fail-loud 不造假。"""
    print("=== nav_fetcher fail-loud 折溢價 demo(確定性樣本,無網路)===")
    for row in _demo_rows():
        line = nav_line(row["ticker"], name=row["name"],
                        nav_info=row["nav"], price_info=row["price"], fetch=False)
        tag = "(個股,略過 NAV)" if not is_etf(row["ticker"]) else ""
        print(f"[{row['ticker']} {row['name']}]{tag}")
        print(f"   {line if line else '— 無 NAV 行'}")
    print("\n說明:00878 NAV 是 07/08 舊值,市價 07/09 → 標『NAV 延遲』不算,"
          "避免把上漲市價配舊淨值算出假溢價。")


if __name__ == "__main__":
    demo()
