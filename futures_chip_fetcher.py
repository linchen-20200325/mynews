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


def _parse_csv(text: str, date_str: str, log=print) -> dict | None:
    """解析 CSV:取『臺股期貨』各身份別的『多空未平倉口數淨額』。無資料回 None。"""
    rows = list(csv.reader(io.StringIO(text)))
    # 找表頭(含『身份別/身分別』與『未平倉』字樣的那一列)。
    header = None
    h_idx = 0
    for i, r in enumerate(rows):
        joined = "".join(r)
        if ("身份別" in joined or "身分別" in joined) and "未平倉" in joined:
            header, h_idx = r, i
            break
    if not header:
        log("  [台指期留倉] 找不到表頭,略過")
        return None

    def col(*names) -> int:
        for j, f in enumerate(header):
            if any(n in str(f) for n in names):
                return j
        return -1

    i_prod = col("商品名稱", "商品")
    i_who = col("身份別", "身分別")
    i_net = col("多空未平倉口數淨額", "未平倉口數淨額")
    if min(i_prod, i_who, i_net) < 0:
        log(f"  [台指期留倉] 缺必要欄位(prod={i_prod} who={i_who} net={i_net}),略過")
        return None

    out = {"foreign_net_oi": None, "trust_net_oi": None, "dealer_net_oi": None}
    for r in rows[h_idx + 1:]:
        if max(i_prod, i_who, i_net) >= len(r):
            continue
        prod = r[i_prod].strip()
        # 只取大台「臺股期貨」,排除小型/微型臺指。
        if "臺股期貨" not in prod and "台股期貨" not in prod:
            continue
        if "小型" in prod or "微型" in prod:
            continue
        who = r[i_who].strip()
        net = _to_int(r[i_net])
        if "外資" in who:
            out["foreign_net_oi"] = net
        elif "投信" in who:
            out["trust_net_oi"] = net
        elif "自營" in who:
            out["dealer_net_oi"] = net

    if out["foreign_net_oi"] is None:
        log("  [台指期留倉] 未取得外資臺股期貨留倉,略過")
        return None

    foreign = out["foreign_net_oi"]
    trust = out["trust_net_oi"] or 0
    dealer = out["dealer_net_oi"] or 0
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
