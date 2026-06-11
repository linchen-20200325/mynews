"""futures_chip_fetcher.py — 抓期交所「三大法人台指期留倉淨額」(外資期貨部位偏多/偏空)。

來源:台灣期貨交易所**官方 OpenAPI(JSON)**:
  https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsByDate
  (「三大法人-區分各期貨契約-依日期」,回傳最新一個交易日全表 JSON。)
用途:看外資/投信/自營在「臺股期貨(大台)」的**未平倉(留倉)口數淨額** —
     正=淨多單(偏多)、負=淨空單(偏空)。

【為何用 OpenAPI】先前的網頁下載端點(futContractsDateExcel)實測回傳整個 HTML 網頁
              而非 CSV,無法解析;OpenAPI 直接吐乾淨 JSON,欄位名與報表一致,穩定可靠。

【性質提醒】留倉是「庫存(現在仍持有的部位)」而非當日「流量」,即使是前一交易日盤後結算,
          仍代表外資抱著走進今日早盤的多空方向 → 與現貨買賣超(chip_fetcher)互補。

【真實性】口數淨額一律取自期交所原始 JSON,不經 AI 估算。

【連線】走 proxy_helper(NAS 代理 + 自動降級直連);抓不到回 None,不拋例外。

輸出:fetch_futures_chip() -> {
  "as_of": "YYYY-MM-DD HH:MM UTC (TAIFEX OpenAPI)",
  "date": "YYYY-MM-DD", "product": "臺股期貨", "unit": "口",
  "foreign_net_oi": <int>, "trust_net_oi": <int>, "dealer_net_oi": <int>,
  "total_net_oi": <int>, "stance": "偏多|偏空|中性",
}  抓不到回 None。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

OPENAPI_URL = ("https://openapi.taifex.com.tw/v1/"
               "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsByDate")
HTTP_TIMEOUT = 25
# 偏多/偏空門檻(口):外資淨留倉超過此絕對值才表態,避免雜訊。可用 FUT_STANCE_LOTS 覆寫。
DEFAULT_STANCE_LOTS = 3000
PRODUCT = "臺股期貨"  # 大台;需排除「小型臺指期貨」「微型臺指期貨」

_PROD_KEYS = ("商品名稱", "商品", "ContractName", "CommodityName")
_WHO_KEYS = ("身份別", "身分別", "InstitutionalInvestors", "Investors")
_DATE_KEYS = ("日期", "Date")


def _to_int(s) -> int:
    try:
        return int(str(s).replace(",", "").replace(" ", "").strip())
    except (TypeError, ValueError):
        return 0


def _first(rec: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in rec and str(rec[k]).strip() != "":
            return str(rec[k]).strip()
    return ""


def _net_oi_key(rec: dict) -> str | None:
    """找『多空未平倉口數淨額』欄位(要口數淨額,不要金額淨額)。找不到回 None。"""
    for k in rec:
        ks = str(k)
        if "未平倉" in ks and "淨" in ks and "金額" not in ks:
            return k
    for k in rec:  # 英文後備
        ks = str(k).lower().replace(" ", "")
        if "openinterest" in ks and "net" in ks and "amount" not in ks:
            return k
    return None


def _norm_date(s: str) -> str:
    """把『2026/6/10』『2026/06/10』或『20260610』正規化為『2026-06-10』;失敗回原字串。"""
    s = s.strip()
    try:
        if "/" in s:
            y, m, d = s.split("/")
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    except (ValueError, TypeError):
        pass
    return s


def _get_json(log=print):
    """GET OpenAPI(走 NAS 代理 + 自動降級直連)。回 JSON;200 但非 JSON 時印原文診斷後回 None。"""
    raw = None  # 保留最後一個 200 回應,供非 JSON 時印原文診斷
    try:
        import proxy_helper
        resp = proxy_helper.fetch_url(
            OPENAPI_URL, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
        if resp is not None and resp.status_code == 200:
            raw = resp
            try:
                return resp.json()
            except Exception:  # noqa: BLE001 — 非 JSON → 留待診斷
                pass
    except Exception as exc:  # noqa: BLE001 — proxy_helper 不可用 → 直連保底
        log(f"  [台指期留倉] 代理連線失敗:{type(exc).__name__}")
    try:
        import requests
        resp = requests.get(
            OPENAPI_URL,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            raw = raw or resp
            try:
                return resp.json()
            except Exception:  # noqa: BLE001
                pass
        else:
            log(f"  [台指期留倉] 直連非 200:{resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        log(f"  [台指期留倉] 直連失敗:{type(exc).__name__}")
    # 取得 200 但內容非 JSON → 印原文揭露實際格式(供精準修正,不盲試)
    if raw is not None:
        ct = raw.headers.get("content-type", "")
        body = (raw.text or "")[:300]
        log(f"  [台指期留倉][診斷] 200 但非 JSON;url={OPENAPI_URL}")
        log(f"  [台指期留倉][診斷] content-type={ct};前 300 字:{body!r}")
    return None


def fetch_futures_chip(log=print) -> dict | None:
    """抓最新交易日三大法人台指期留倉淨額(OpenAPI 回傳最新一日)。抓不到回 None。"""
    data = _get_json(log)
    if not isinstance(data, list) or not data:
        log("  [台指期留倉] OpenAPI 無資料(連線/代理問題或回傳非陣列),略過")
        return None

    found: dict = {}
    date_str = ""
    sample_keys = None
    for rec in data:
        if not isinstance(rec, dict):
            continue
        if sample_keys is None:
            sample_keys = list(rec.keys())
        prod = _first(rec, _PROD_KEYS)
        if ("臺股期貨" not in prod and "台股期貨" not in prod) or "小型" in prod or "微型" in prod:
            continue
        nk = _net_oi_key(rec)
        if not nk:
            continue
        who = _first(rec, _WHO_KEYS)
        net = _to_int(rec.get(nk))
        date_str = date_str or _first(rec, _DATE_KEYS)
        if "外資" in who:
            found["foreign_net_oi"] = net
        elif "投信" in who:
            found["trust_net_oi"] = net
        elif "自營" in who:
            found["dealer_net_oi"] = net

    if "foreign_net_oi" not in found:
        # 診斷:印首筆記錄的欄位名,供精準對欄(不盲試)
        log(f"  [台指期留倉][診斷] 未找到外資臺股期貨;首筆欄位={sample_keys}")
        return None

    foreign = found["foreign_net_oi"]
    trust = found.get("trust_net_oi", 0)
    dealer = found.get("dealer_net_oi", 0)
    try:
        thr = int(os.environ.get("FUT_STANCE_LOTS") or DEFAULT_STANCE_LOTS)
    except (TypeError, ValueError):
        thr = DEFAULT_STANCE_LOTS
    stance = "偏多" if foreign >= thr else ("偏空" if foreign <= -thr else "中性")
    log(f"  [台指期留倉] {_norm_date(date_str)} 外資淨"
        f"{('多' if foreign >= 0 else '空')}{abs(foreign):,}口 → {stance}")
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC (TAIFEX OpenAPI)"),
        "date": _norm_date(date_str), "product": PRODUCT, "unit": "口",
        "foreign_net_oi": foreign, "trust_net_oi": trust,
        "dealer_net_oi": dealer, "total_net_oi": foreign + trust + dealer,
        "stance": stance,
    }


if __name__ == "__main__":
    out = fetch_futures_chip()
    if not out:
        print("台指期留倉抓取失敗", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0)
