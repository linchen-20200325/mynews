"""futures_chip_fetcher.py — 抓期交所「三大法人台指期留倉淨額」(外資期貨部位偏多/偏空)。

來源:台灣期貨交易所「三大法人－區分各期貨契約」每日 CSV:
  https://www.taifex.com.tw/cht/3/futContractsDateExcel
用途:看外資/投信/自營在「臺股期貨(大台 TXF)」的**未平倉(留倉)口數淨額** —
     正=淨多單(偏多)、負=淨空單(偏空)。

【性質提醒】留倉是「庫存(現在仍持有的部位)」而非當日「流量」,所以即使數字是
          前一交易日盤後結算,仍代表外資抱著走進今日早盤的多空方向 →
          與現貨三大法人「買賣超」(chip_fetcher)互補:現貨看流量、期貨看部位。

【真實性】口數淨額一律取自期交所原始 CSV,不經 AI 估算。

【連線】期交所常擋境外 IP,故走 NAS 代理(PROXY_URL)+ 自動降級直連;抓不到回 None。

輸出:fetch_futures_chip() -> {
  "as_of": "YYYY-MM-DD HH:MM UTC (TAIFEX futContractsDate)",
  "date": "YYYY-MM-DD", "product": "臺股期貨", "unit": "口",
  "foreign_net_oi": <int>, "trust_net_oi": <int>, "dealer_net_oi": <int>,
  "total_net_oi": <int>, "stance": "偏多|偏空|中性",
}  抓不到回 None。
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import date, datetime, timedelta, timezone

CSV_URL = "https://www.taifex.com.tw/cht/3/futContractsDateExcel"
HTTP_TIMEOUT = 25
MAX_LOOKBACK = 14
# 偏多/偏空門檻(口):外資淨留倉超過此絕對值才表態,避免雜訊。可用 FUT_STANCE_LOTS 覆寫。
DEFAULT_STANCE_LOTS = 3000
PRODUCT = "臺股期貨"  # 大台 TXF;需排除「小型臺指期貨」「微型臺指期貨」


def _to_int(s) -> int:
    try:
        return int(str(s).replace(",", "").replace(" ", "").strip())
    except (TypeError, ValueError):
        return 0


def _post_csv(date_str: str, log=print) -> str | None:
    """POST 期交所抓單日 CSV(Big5);先走 NAS 代理,失敗降級直連。非 200 回 None。"""
    import requests
    payload = {
        "queryType": "1", "goDay": "", "doQuery": "1", "dateaddcnt": "",
        "queryDate": f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}", "commodityId": "TXF",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.taifex.com.tw/cht/3/futContractsDate",
        "Accept": "text/csv,*/*",
    }
    try:
        import proxy_helper
        proxies = proxy_helper.get_proxy_config()
    except Exception:  # noqa: BLE001
        proxies = None

    attempts = [("代理", proxies, False)] if proxies else []
    attempts.append(("直連", None, True))
    for mode, prox, verify in attempts:
        try:
            resp = requests.post(CSV_URL, data=payload, headers=headers,
                                 proxies=prox, timeout=HTTP_TIMEOUT, verify=verify)
            if resp.status_code == 200 and resp.content:
                for enc in ("cp950", "big5", "utf-8-sig", "utf-8"):
                    try:
                        return resp.content.decode(enc)
                    except UnicodeDecodeError:
                        continue
                return resp.content.decode("cp950", errors="replace")
            log(f"  [台指期留倉] {mode} 非 200:{resp.status_code}")
        except Exception as exc:  # noqa: BLE001 — 換下一個連線方式
            log(f"  [台指期留倉] {mode} 連線失敗:{type(exc).__name__}")
    return None


def _extract(rows: list, require_big: bool) -> tuple[dict, list | None]:
    """掃資料列取三大法人台指期留倉(不靠表頭)。

    期交所這份 CSV 表頭是「兩列」(上層交易/未平倉分組、下層口數欄位),故不找表頭,
    直接認資料列:含身份別(外資/投信/自營商)即為資料列;『多空未平倉口數淨額』固定是
    該列**倒數第二欄**(最後一欄為對應契約金額淨額)。
    require_big=True 時僅取含「臺股期貨」名稱的列(排除小型/微型);
    若過濾模式下商品名稱欄空白,改用 require_big=False 退而求其次。
    """
    out: dict = {}
    sample = None
    for r in rows:
        cells = [c.strip() for c in r]
        if any("小型" in c or "微型" in c for c in cells):
            continue
        if require_big and not any("臺股期貨" in c or "台股期貨" in c for c in cells):
            continue
        who = next((c for c in cells if c in ("外資", "投信", "自營商", "外資及陸資")), "")
        if not who:
            continue
        trimmed = list(cells)
        while trimmed and trimmed[-1] == "":
            trimmed.pop()
        if len(trimmed) < 2:
            continue
        net = _to_int(trimmed[-2])  # 倒數第二欄 = 多空未平倉口數淨額
        if sample is None:
            sample = trimmed
        key = ("foreign_net_oi" if "外資" in who
               else "trust_net_oi" if "投信" in who else "dealer_net_oi")
        out[key] = net
    return out, sample


def _parse_csv(text: str, date_str: str, log=print) -> dict | None:
    """解析 CSV:取『臺股期貨』各身份別的『多空未平倉口數淨額』。無資料回 None。"""
    rows = list(csv.reader(io.StringIO(text)))
    found, sample = _extract(rows, require_big=True)
    if "foreign_net_oi" not in found:  # 過濾模式商品名稱可能空白 → 退而求其次
        found, sample = _extract(rows, require_big=False)
    if "foreign_net_oi" not in found:
        log("  [台指期留倉] 未取得外資臺股期貨留倉,略過")
        return None
    if sample is not None:
        log(f"  [台指期留倉] 樣本列({len(sample)}欄):{sample[-6:]}")

    foreign = found["foreign_net_oi"]
    trust = found.get("trust_net_oi", 0)
    dealer = found.get("dealer_net_oi", 0)
    import os
    try:
        thr = int(os.environ.get("FUT_STANCE_LOTS") or DEFAULT_STANCE_LOTS)
    except (TypeError, ValueError):
        thr = DEFAULT_STANCE_LOTS
    stance = "偏多" if foreign >= thr else ("偏空" if foreign <= -thr else "中性")
    log(f"  [台指期留倉] {date_str} 外資淨{('多' if foreign >= 0 else '空')}{abs(foreign):,}口 → {stance}")
    return {
        "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
        "product": PRODUCT, "unit": "口",
        "foreign_net_oi": foreign, "trust_net_oi": trust,
        "dealer_net_oi": dealer, "total_net_oi": foreign + trust + dealer,
        "stance": stance,
    }


def fetch_futures_chip(log=print) -> dict | None:
    """抓最近一個交易日的三大法人台指期留倉淨額;自動跳過週末/假日。抓不到回 None。"""
    d = date.today()
    for _ in range(MAX_LOOKBACK):
        if d.weekday() < 5:
            ds = d.strftime("%Y%m%d")
            text = _post_csv(ds, log=log)
            if text:
                parsed = _parse_csv(text, ds, log=log)
                if parsed:
                    parsed["as_of"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M UTC (TAIFEX futContractsDate)")
                    return parsed
        d -= timedelta(days=1)
    return None


if __name__ == "__main__":
    data = fetch_futures_chip()
    if not data:
        print("台指期留倉抓取失敗", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(0)
