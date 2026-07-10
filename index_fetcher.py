"""index_fetcher.py — 抓美股指數 / 美股期貨的「最新 vs 前收」漲跌幅。

用途:利用「時區時間差」做台股盤前預警 —
  * 美股指數(收盤約台灣 04:00)→ 對台股開盤(09:00)是【隔夜領先】訊號。
  * 美股期貨(近 24h 連續)→ 台股【盤前即時】最新風向。
  (台指期夜盤的盤前即時訊號另由 taifex_night_fetcher 抓取。)

來源:Yahoo Finance chart API(JSON、免金鑰、全球可達):
  https://query1.finance.yahoo.com/v8/finance/chart/<symbol>?range=5d&interval=1d
連線走 proxy_helper 的 prefer_direct 模式:Yahoo 全球可達,直連優先(免「境外→台灣
NAS→境外」三角繞路,9 個 symbol 可省數秒);直連失敗才走 NAS 中繼備援。
沙箱無網路時抓不到屬正常。

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

import config   # 環境變數讀取 SSOT
import numutil  # 漲跌幅公式 + 方向對帳的單一真相源(SSOT)

YF_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"

# 互動場景逾時/重試收斂:單 symbol 最壞情況必須有界,9 個 symbol 串抓才不會卡住整頁。
HTTP_TIMEOUT = 10
HTTP_RETRIES = 2

# 追蹤標的:symbol(Yahoo)→ 中文名 / 分組 / 時間差性質。
# lead_type:對台股的時間差定位(隔夜領先 / 同步連動 / 盤前即時)。
SYMBOLS: list[dict] = [
    {"symbol": "^GSPC", "name": "標普 500", "group": "美股指數", "lead_type": "隔夜領先"},
    {"symbol": "^IXIC", "name": "那斯達克綜合", "group": "美股指數", "lead_type": "隔夜領先"},
    {"symbol": "^DJI", "name": "道瓊工業", "group": "美股指數", "lead_type": "隔夜領先"},
    {"symbol": "^SOX", "name": "費城半導體", "group": "美股指數", "lead_type": "隔夜領先"},
    {"symbol": "ES=F", "name": "標普 500 期貨", "group": "美股期貨", "lead_type": "盤前即時"},
    {"symbol": "NQ=F", "name": "那斯達克 100 期貨", "group": "美股期貨", "lead_type": "盤前即時"},
    # 債匯總經訊號(Fed 利率預期):殖利率走升 / 美元走強 = 資金收緊。不列入「大跌」清單。
    {"symbol": "^TNX", "name": "美10年期公債殖利率", "group": "債匯", "lead_type": ""},
    {"symbol": "DX-Y.NYB", "name": "美元指數", "group": "債匯", "lead_type": ""},
    # 新台幣匯率(USD/TWD):走升=台幣貶值=外資賣股後匯出提款的真實訊號。不列入「大跌」清單。
    {"symbol": "TWD=X", "name": "新台幣匯率", "group": "債匯", "lead_type": ""},
]

# 預設「大跌」門檻(%):當日跌幅 <= 此值才標警示。可用 INTL_DROP_THRESHOLD 覆寫。
DEFAULT_DROP_THRESHOLD = -1.5


def drop_threshold() -> float:
    """目前生效的「大跌」門檻(%):環境變數 INTL_DROP_THRESHOLD 可覆寫預設值。

    公開供 update_data 在報價全失敗的降級路徑組空 quotes 文件時取用(SSOT)。
    """
    return config.env_float("INTL_DROP_THRESHOLD", DEFAULT_DROP_THRESHOLD)


def _http_get_json(url: str) -> dict | None:
    """GET JSON:Yahoo 全球可達 → 直連優先、NAS 中繼備援(prefer_direct);失敗回 None。"""
    import proxy_helper
    return proxy_helper.fetch_json(
        url, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES, prefer_direct=True)


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
    threshold = drop_threshold()
    quotes: dict[str, dict] = {}
    for item in SYMBOLS:
        sym = item["symbol"]
        try:
            parsed = _parse_chart(_http_get_json(YF_CHART_URL.format(symbol=sym)))
            if not parsed:
                log(f"  [{sym}] 無有效報價,略過")
                continue
            last, prev = parsed
            change_pct = numutil.pct_change(last, prev)  # 含 prev>0 與方向對帳不變量
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


# ── 期現背離偵測常數 ──────────────────────────────────────────────────────
_REVERSAL_DIVERGENCE_MIN = 2.0  # 期貨與現貨差距 ≥ 此值（%）才算顯著回彈


def detect_spot_futures_divergence(
    qmap: dict,
    threshold: float = DEFAULT_DROP_THRESHOLD,
) -> dict:
    """偵測費半現貨（已定案）與那指/標普期貨（即時）的期現背離訊號。

    訊號類型
    --------
    reversal      現貨大跌 + 期貨顯著回彈（divergence ≥ 2%）→ 留意翻轉
    follow_through 現貨 + 期貨皆大跌 → 跌勢延伸，開盤賣壓未解除
    caution       現貨未跌但期貨已轉弱 → 預警
    normal        無顯著背離（不顯示）

    優先使用那指 100 期貨（NQ=F，科技/半導體相關性高），fallback 標普期貨（ES=F）。

    Returns
    -------
    dict
        keys: sox_pct, futures_symbol, futures_name, futures_pct,
              divergence, signal, description
    """
    sox_pct: float | None = (qmap.get("^SOX") or {}).get("change_pct")

    # 選期貨：優先 NQ=F（那指期貨，科技相關性強），fallback ES=F
    futures_sym: str | None = None
    fut: dict = {}
    for sym in ("NQ=F", "ES=F"):
        if sym in qmap:
            futures_sym = sym
            fut = qmap[sym]
            break

    futures_pct: float | None = fut.get("change_pct")
    futures_name: str = fut.get("name", "期貨")

    if sox_pct is None or futures_pct is None:
        return {"signal": "normal", "description": ""}

    divergence = round(futures_pct - sox_pct, 2)
    spot_is_drop = sox_pct <= threshold
    futures_is_drop = futures_pct <= threshold

    if spot_is_drop and divergence >= _REVERSAL_DIVERGENCE_MIN:
        signal = "reversal"
        desc = (
            f"費半 {sox_pct:+.1f}% 但{futures_name}回彈至 {futures_pct:+.1f}%"
            f"（背離 {divergence:+.1f}%），留意翻轉機會"
        )
    elif spot_is_drop and futures_is_drop:
        signal = "follow_through"
        desc = (
            f"費半 {sox_pct:+.1f}%，{futures_name}同步 {futures_pct:+.1f}%"
            f"，跌勢延伸，留意開盤賣壓"
        )
    elif not spot_is_drop and futures_is_drop:
        signal = "caution"
        desc = (
            f"現貨尚未大跌，但{futures_name} {futures_pct:+.1f}%"
            f"，留意隔日開盤方向"
        )
    else:
        signal = "normal"
        desc = ""

    return {
        "sox_pct": sox_pct,
        "futures_symbol": futures_sym,
        "futures_name": futures_name,
        "futures_pct": futures_pct,
        "divergence": divergence,
        "signal": signal,
        "description": desc,
    }


if __name__ == "__main__":
    try:
        data = fetch_index_quotes()
    except Exception as exc:  # noqa: BLE001
        print(f"指數抓取失敗:{exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(0)
