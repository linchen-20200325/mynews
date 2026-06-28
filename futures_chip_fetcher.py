"""futures_chip_fetcher.py — 抓期交所「三大法人台指期留倉淨額」(外資期貨部位偏多/偏空)。

來源:台灣期貨交易所**官方 OpenAPI(JSON)**,端點(依 Swagger 文件實際路徑):
  https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate
  (「三大法人-區分各期貨契約-依日期」,回傳最新一個交易日全表 JSON。)
用途:看外資/投信/自營在「臺股期貨(大台,ContractCode=TX)」的**未平倉(留倉)
     口數淨額** OpenInterest(Net) — 正=淨多單(偏多)、負=淨空單(偏空)。

【欄位】英文欄位:Date、ContractCode(商品代碼)、Item(身份別)、
       OpenInterest(Net)(多空未平倉口數淨額,即所需)、其餘為交易量/契約金額。

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

import numutil

OPENAPI_URL = ("https://openapi.taifex.com.tw/v1/"
               "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
HTTP_TIMEOUT = 25
# 偏多/偏空門檻(口):外資淨留倉超過此絕對值才表態,避免雜訊。可用 FUT_STANCE_LOTS 覆寫。
DEFAULT_STANCE_LOTS = 3000
PRODUCT = "臺股期貨"

# OpenAPI 英文欄位名(實測:ContractCode 內容為中文商品名稱,如「臺股期貨」)
CODE_KEY = "ContractCode"     # 商品名稱(中文)
ITEM_KEY = "Item"             # 身份別(自營商/投信/外資)
NET_OI_KEY = "OpenInterest(Net)"  # 多空未平倉口數淨額(要口數,非 ContractValue 金額)
DATE_KEY = "Date"


def _institution(item: str) -> str | None:
    """把身份別字串歸類成 foreign/trust/dealer(容許中英文);非三大法人回 None。"""
    s = str(item)
    if "外資" in s or "Foreign" in s:
        return "foreign_net_oi"
    if "投信" in s or "Trust" in s:
        return "trust_net_oi"
    if "自營" in s or "Dealer" in s:
        return "dealer_net_oi"
    return None


def _norm_date(s: str) -> str:
    """把『2026/6/10』『2026/06/10』或『20260610』正規化為『2026-06-10』;失敗回原字串。"""
    s = str(s).strip()
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
    if raw is not None:  # 200 但非 JSON → 印原文揭露實際格式
        log(f"  [台指期留倉][診斷] 200 但非 JSON;content-type="
            f"{raw.headers.get('content-type', '')};前 300 字:{(raw.text or '')[:300]!r}")
    return None


def fetch_futures_chip(log=print) -> dict | None:
    """抓最新交易日三大法人台指期(大台)留倉淨額(OpenAPI 回傳最新一日)。抓不到回 None。"""
    data = _get_json(log)
    if not isinstance(data, list) or not data:
        log("  [台指期留倉] OpenAPI 無資料(連線/代理問題或回傳非陣列),略過")
        return None

    found: dict = {}
    date_str = ""
    codes_seen: set[str] = set()
    for rec in data:
        if not isinstance(rec, dict):
            continue
        code = str(rec.get(CODE_KEY, "")).strip()
        codes_seen.add(code)
        # ContractCode 實際存中文商品名稱;取大台「臺股期貨」,排除小型臺指/微型臺指
        # (其名為「臺指期貨」不含「臺股期貨」)及其他商品。
        if ("臺股期貨" not in code and "台股期貨" not in code) or "小型" in code or "微型" in code:
            continue
        key = _institution(rec.get(ITEM_KEY, ""))
        if not key:
            continue
        found[key] = numutil.parse_number(rec.get(NET_OI_KEY), as_int=True, default=0)
        date_str = date_str or str(rec.get(DATE_KEY, ""))

    if "foreign_net_oi" not in found:
        # 診斷:印出現過的商品代碼,供確認 TX 寫法(不盲試)
        log(f"  [台指期留倉][診斷] 未找到臺股期貨外資留倉;出現的商品名稱="
            f"{sorted(c for c in codes_seen if c)[:40]}")
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
