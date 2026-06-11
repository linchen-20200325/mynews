"""taifex_night_fetcher.py — 抓期交所「台指期(大台 TXF)近月」最新報價(夜盤盤前風向)。

來源:台灣期貨交易所 MIS 即時行情系統(POST JSON API):
  https://mis.taifex.com.tw/futures/api/getQuoteList
用途:台指期夜盤(台灣 15:00–次日 05:00)直接反映「台股自身對隔夜美股的定價」,
     是對台股開盤最直接的【盤前即時】訊號(比 ES=F/NQ=F 更貼近台股個別利空)。

【真實性】成交價/前結算價一律取自期交所原始 JSON,不經 AI 估算。漲跌幅由程式計算。

【連線】MIS 常擋境外 IP,故走 proxy_helper 的 NAS 代理(PROXY_URL)送 POST,
       連不上自動降級直連;沙箱/無代理時抓不到屬正常,回 None 不拋例外。

輸出:fetch_night_quote() -> {
  "symbol": "TXF-NIGHT", "group": "台股期貨", "lead_type": "盤前即時",
  # name/session 依抓取當下台灣時段誠實標示,避免日盤中跑卻寫「夜盤」誤導:
  "name": "台指期夜盤|台指期夜盤收盤|台指期(日盤即時)|台指期(日盤收盤)",
  "session": "night|night_close|day|day_close",
  "last": <float>, "prev": <float>, "change_pct": <float %>,
  "expiry": "YYYYMMDD", "contract": "<期交所中文名>",
  "as_of": "YYYY-MM-DD HH:MM UTC (TAIFEX MIS)",
}  抓不到回 None。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

GETQUOTELIST_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
# 大台「臺股期貨」月契約:KindID=1、CID=TXF;RowSize 取全部後由程式挑近月。
PAYLOAD = {
    "MarketType": "0", "SymbolType": "F", "KindID": "1", "CID": "TXF",
    "ExpireMonth": "", "RowSize": "全部", "PageNo": "",
    "SortColumn": "", "SortOrder": "",
}
HTTP_TIMEOUT = 20
# 期交所欄位命名各版本略有差異,逐一嘗試(找不到回 None)。
_LAST_KEYS = ("CLastPrice", "CDealPrice", "CLast", "DealPrice")
_REF_KEYS = ("CRefPrice", "CYClosePrice", "CRef", "RefPrice")
_VOL_KEYS = ("CTotalVolume", "CTotalMatchQty", "CVolume", "CAccVolume", "Volume")
_NAME_KEYS = ("DispCName", "CName", "SymbolName")
_EXPIRY_KEYS = ("CExpiryDate", "ExpiryDate", "CSettlementDate")


def _session_label() -> tuple[str, str]:
    """依台灣現在時間判斷台指期所處時段,回 (誠實顯示名稱, 時段標籤)。

    台指期交易時段(台灣):一般(日盤)08:45–13:45、盤後(夜盤)15:00–次日 05:00。
    其餘為休市,最後一筆報價即前一時段收盤,故標「收盤」避免標籤誤導。
    """
    mins = (datetime.now(timezone.utc) + timedelta(hours=8))
    m = mins.hour * 60 + mins.minute
    if 8 * 60 + 45 <= m < 13 * 60 + 45:
        return "台指期(日盤即時)", "day"
    if 13 * 60 + 45 <= m < 15 * 60:
        return "台指期(日盤收盤)", "day_close"
    if m >= 15 * 60 or m < 5 * 60:
        return "台指期夜盤", "night"
    return "台指期夜盤收盤", "night_close"  # 05:00–08:45 夜盤已收、日盤未開


def _to_float(s) -> float | None:
    try:
        v = str(s).replace(",", "").replace("%", "").strip()
        return float(v) if v not in ("", "-", "--") else None
    except (TypeError, ValueError):
        return None


def _first(row: dict, keys: tuple[str, ...]):
    """回傳 row 中第一個有非空值的 key 內容;都沒有回 None。"""
    for k in keys:
        if k in row and str(row[k]).strip() not in ("", "-", "--"):
            return row[k]
    return None


def _post_json(payload: dict, log=print) -> dict | None:
    """POST 期交所 MIS:先走 NAS 代理,連不上/被擋自動降級直連。非 200/非 JSON 回 None。"""
    import requests
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://mis.taifex.com.tw",
        "Referer": "https://mis.taifex.com.tw/futures/",
    }
    try:
        import proxy_helper
        proxies = proxy_helper.get_proxy_config()
    except Exception:  # noqa: BLE001 — 無 proxy_helper → 直連
        proxies = None

    # (mode, proxies, verify):有代理先走代理(verify=False 相容 Squid),失敗再直連。
    attempts = []
    if proxies:
        attempts.append(("代理", proxies, False))
    attempts.append(("直連", None, True))

    for mode, prox, verify in attempts:
        try:
            resp = requests.post(
                GETQUOTELIST_URL, json=payload, headers=headers,
                proxies=prox, timeout=HTTP_TIMEOUT, verify=verify,
            )
            if resp.status_code == 200:
                return resp.json()
            log(f"  [台指期夜盤] {mode} 非 200:{resp.status_code}")
        except Exception as exc:  # noqa: BLE001 — 換下一個連線方式
            log(f"  [台指期夜盤] {mode} 連線失敗:{type(exc).__name__}")
    return None


def fetch_night_quote(log=print) -> dict | None:
    """抓台指期(大台)近月最新報價;以成交量最大者為近月,算相對前結算的漲跌幅。"""
    data = _post_json(PAYLOAD, log=log)
    if not data:
        return None
    rt = data.get("RtData") or data.get("rtData") or {}
    quote_list = rt.get("QuoteList") or rt.get("quoteList") or []
    if not isinstance(quote_list, list) or not quote_list:
        log("  [台指期夜盤] 回傳無 QuoteList,略過")
        return None

    # 近月 = 成交量最大的契約(避開週/遠月);需同時有成交價與前結算價才採計。
    best = None
    best_vol = -1.0
    for row in quote_list:
        if not isinstance(row, dict):
            continue
        last = _to_float(_first(row, _LAST_KEYS))
        ref = _to_float(_first(row, _REF_KEYS))
        if last is None or not ref:
            continue
        vol = _to_float(_first(row, _VOL_KEYS)) or 0.0
        if vol > best_vol:
            best_vol = vol
            best = (row, last, ref)

    if not best:
        log("  [台指期夜盤] 各契約皆缺成交價/前結算價,略過")
        return None

    row, last, ref = best
    change_pct = round((last - ref) / ref * 100, 2)
    contract = str(_first(row, _NAME_KEYS) or "台指期").strip()
    expiry = str(_first(row, _EXPIRY_KEYS) or "").strip()
    disp_name, session = _session_label()  # 依現在時段給誠實名稱(日盤即時/夜盤/收盤)
    log(f"  [台指期/{session}] {contract} {last:g}(前結算 {ref:g}){change_pct:+.2f}%")
    return {
        "symbol": "TXF-NIGHT",
        "name": disp_name,
        "session": session,
        "group": "台股期貨",
        "lead_type": "盤前即時",
        "last": round(last, 2),
        "prev": round(ref, 2),
        "change_pct": change_pct,
        "expiry": expiry,
        "contract": contract,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC (TAIFEX MIS)"),
    }


if __name__ == "__main__":
    q = fetch_night_quote()
    if not q:
        print("台指期夜盤抓取失敗", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(q, ensure_ascii=False, indent=2))
    sys.exit(0)
