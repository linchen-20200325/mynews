"""index_fetcher.py — 抓美股指數 / KOSPI / 美股期貨的「最新 vs 前收」漲跌幅。

用途:利用「時區時間差」做台股盤前預警 —
  * 美股指數(收盤約台灣 04:00)→ 對台股開盤(09:00)是【隔夜領先】訊號。
  * KOSPI(08:00–14:00 台灣時間)與台股近乎同步 → 視為【同步連動】半導體 peer 對照。
  * 美股期貨(近 24h 連續)→ 台股【盤前即時】最新風向。

來源:Yahoo Finance chart API(JSON、免金鑰、全球可達):
  https://query1.finance.yahoo.com/v8/finance/chart/<symbol>?range=5d&interval=1d
連線優先走 proxy_helper(PROXY_URL)中繼,連不上自動降級直連;沙箱無網路時抓不到屬正常。

【真實性】本模組只回傳「真實市場報價算出的漲跌幅」,數字一律來自 Yahoo,
        不經 AI 估算;Gemini 僅在 update_data 端負責「解讀利空原因」,不得竄改數字。

輸出:fetch_index_quotes() -> {
  "as_of": "YYYY-MM-DD HH:MM UTC (Yahoo Finance)",
  "threshold": -1.5,
  "quotes": {
    "^GSPC": {"name","group","lead_type","last","prev","change_pct","is_drop"}, ...
  }
}
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

YF_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"

HTTP_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 追蹤標的:symbol(Yahoo)→ 中文名 / 分組 / 時間差性質。
# lead_type:對台股的時間差定位(隔夜領先 / 同步連動 / 盤前即時)。
SYMBOLS: list[dict] = [
    {"symbol": "^GSPC", "name": "標普 500", "group": "美股指數", "lead_type": "隔夜領先"},
    {"symbol": "^IXIC", "name": "那斯達克綜合", "group": "美股指數", "lead_type": "隔夜領先"},
    {"symbol": "^DJI", "name": "道瓊工業", "group": "美股指數", "lead_type": "隔夜領先"},
    {"symbol": "^SOX", "name": "費城半導體", "group": "美股指數", "lead_type": "隔夜領先"},
    {"symbol": "^KS11", "name": "韓股 KOSPI", "group": "亞股", "lead_type": "同步連動"},
    {"symbol": "ES=F", "name": "標普 500 期貨", "group": "美股期貨", "lead_type": "盤前即時"},
    {"symbol": "NQ=F", "name": "那斯達克 100 期貨", "group": "美股期貨", "lead_type": "盤前即時"},
]

# 預設「大跌」門檻(%):當日跌幅 <= 此值才標警示。可用 INTL_DROP_THRESHOLD 覆寫。
DEFAULT_DROP_THRESHOLD = -1.5


def _drop_threshold() -> float:
    import os
    try:
        return float(os.environ.get("INTL_DROP_THRESHOLD", DEFAULT_DROP_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_DROP_THRESHOLD


def _http_get_json(url: str, proxy: str | None) -> dict:
    """GET JSON:優先走 proxy_helper(含自動降級直連),取不到再直連保底。"""
    try:
        import proxy_helper
        resp = proxy_helper.fetch_url(
            url, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT,
        )
        if resp is not None and resp.status_code == 200:
            return resp.json()
    except Exception:  # noqa: BLE001 — proxy_helper 不可用 → 走直連保底
        pass

    import requests
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        proxies=proxies, timeout=HTTP_TIMEOUT, verify=not bool(proxies),
    )
    resp.raise_for_status()
    return resp.json()


def _parse_chart(payload: dict) -> tuple[float, float] | None:
    """從 Yahoo chart 回傳取 (最新值, 前收)。

    優先用 meta.regularMarketPrice 對 chartPreviousClose(收盤市場=當日完整漲跌,
    期貨盤中=即時隔夜漲跌,正是要的訊號);缺值時退回收盤序列最後兩個有效值。
    """
    try:
        res = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None

    meta = res.get("meta") or {}
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")

    if last is None or not prev:
        try:
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        except (KeyError, IndexError, TypeError):
            closes = []
        if len(closes) >= 2:
            last, prev = closes[-1], closes[-2]
        elif len(closes) == 1 and prev:
            last = closes[-1]

    try:
        last_f, prev_f = float(last), float(prev)
    except (TypeError, ValueError):
        return None
    if prev_f == 0:
        return None
    return last_f, prev_f


def fetch_index_quotes(proxy: str | None = None, log=print) -> dict:
    """抓所有追蹤指數/期貨的最新漲跌幅;單一標的失敗只略過,不影響其他。"""
    try:
        import proxy_helper
        cfg = proxy_helper.get_proxy_config(proxy)
        proxy_url = cfg["http"] if cfg else None
    except Exception:  # noqa: BLE001
        import os
        proxy_url = (proxy or os.environ.get("PROXY_URL") or "").strip() or None

    threshold = _drop_threshold()
    quotes: dict[str, dict] = {}
    for item in SYMBOLS:
        sym = item["symbol"]
        try:
            parsed = _parse_chart(_http_get_json(YF_CHART_URL.format(symbol=sym), proxy_url))
            if not parsed:
                log(f"  [{sym}] 無有效報價,略過")
                continue
            last, prev = parsed
            change_pct = round((last - prev) / prev * 100, 2)
            quotes[sym] = {
                "name": item["name"],
                "group": item["group"],
                "lead_type": item["lead_type"],
                "last": round(last, 2),
                "prev": round(prev, 2),
                "change_pct": change_pct,
                "is_drop": change_pct <= threshold,
            }
            log(f"  [{sym}] {item['name']} {change_pct:+.2f}%")
        except Exception as exc:  # noqa: BLE001 — 單一標的失敗不影響其他
            log(f"  [{sym}] 失敗:{exc}")

    if not quotes:
        raise RuntimeError("所有指數/期貨皆抓取失敗(檢查 PROXY_URL / 來源是否可達)")

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC (Yahoo Finance)"),
        "threshold": threshold,
        "quotes": quotes,
    }


if __name__ == "__main__":
    try:
        data = fetch_index_quotes()
    except Exception as exc:  # noqa: BLE001
        print(f"指數抓取失敗:{exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(0)
