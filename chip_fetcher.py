"""chip_fetcher.py — 抓證交所「三大法人買賣超」近 N 個交易日(事後驗證用)。

來源:台灣證券交易所 BFI82U(三大法人買賣金額總額,日)。已實測:GitHub Actions
     美國 IP 直連即可(TWSE 不擋境外),故沿用 proxy_helper.fetch_url(有 proxy 走
     proxy、無則直連)。沙箱無網路時抓不到屬正常。

【真實性】數字一律取自證交所原始 JSON(單位:元),不經 AI 估算。

輸出:fetch_chip_flow(days=N) -> {
  "as_of": "YYYY-MM-DD HH:MM UTC (TWSE BFI82U)",
  "unit": "元",
  "days": [  # 由新到舊
    {"date","foreign","trust","dealer","dealer_self","dealer_hedge",
     "foreign_main","foreign_dealer","total"}, ...
  ]
}
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone

import numutil

BFI82U_URL = (
    "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
    "?dayDate={d}&type=day&response=json"
)
HTTP_TIMEOUT = 20
DEFAULT_DAYS = 10
MAX_LOOKBACK = 30  # 最多往回找的日曆天數,避免連假/長停盤無限迴圈



def _fetch_day_json(date_str: str) -> dict | None:
    """走 proxy_helper.fetch_json 抓單日 BFI82U JSON(proxy→直連兩段降級)。"""
    import proxy_helper
    return proxy_helper.fetch_json(BFI82U_URL.format(d=date_str), timeout=HTTP_TIMEOUT)


def _parse_day(payload: dict, date_str: str) -> dict | None:
    """把 BFI82U 一日 JSON 解析成正規化結構;無資料(假日)回 None。

    data 各列:['單位名稱','買進金額','賣出金額','買賣差額'],依單位名稱歸位。
    外資合計 = 外資及陸資(不含外資自營商) + 外資自營商;自營 = 自行 + 避險。
    """
    if not payload or payload.get("stat") != "OK" or not payload.get("data"):
        return None
    bucket = {"foreign_main": 0, "foreign_dealer": 0, "trust": 0,
              "dealer_self": 0, "dealer_hedge": 0, "total": 0}
    for row in payload["data"]:
        if len(row) < 4:
            continue
        name, diff = row[0], numutil.parse_number(row[3], as_int=True, default=0)
        # 注意:「外資及陸資(不含外資自營商)」字串內含「外資自營商」,
        # 故必須先判斷「外資及陸資」,再判斷「外資自營商」,否則主力外資會被誤分類為 0。
        if "外資及陸資" in name:
            bucket["foreign_main"] = diff
        elif "外資自營商" in name:
            bucket["foreign_dealer"] = diff
        elif "外資" in name and "自營" not in name:
            bucket["foreign_main"] = diff
        elif "投信" in name:
            bucket["trust"] = diff
        elif "自營商" in name and "避險" in name:
            bucket["dealer_hedge"] = diff
        elif "自營商" in name:  # 自行買賣
            bucket["dealer_self"] = diff
        elif "合計" in name:
            bucket["total"] = diff
    return {
        "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
        "foreign": bucket["foreign_main"] + bucket["foreign_dealer"],
        "foreign_main": bucket["foreign_main"],
        "foreign_dealer": bucket["foreign_dealer"],
        "trust": bucket["trust"],
        "dealer": bucket["dealer_self"] + bucket["dealer_hedge"],
        "dealer_self": bucket["dealer_self"],
        "dealer_hedge": bucket["dealer_hedge"],
        "total": bucket["total"],
    }


def fetch_chip_flow(days: int = DEFAULT_DAYS, log=print) -> dict:
    """抓近 days 個交易日的三大法人買賣超(由新到舊),自動跳過週末/假日。"""
    collected: list[dict] = []
    d = date.today()
    looked = 0
    while len(collected) < days and looked < MAX_LOOKBACK:
        looked += 1
        if d.weekday() < 5:  # 只試工作日,省請求
            parsed = _parse_day(_fetch_day_json(d.strftime("%Y%m%d")),
                                 d.strftime("%Y%m%d"))
            if parsed:
                collected.append(parsed)
                log(f"  [{parsed['date']}] 外資 {parsed['foreign']/1e8:+.0f}億 "
                    f"投信 {parsed['trust']/1e8:+.0f}億 合計 {parsed['total']/1e8:+.0f}億")
        d -= timedelta(days=1)
    if not collected:
        raise RuntimeError("三大法人資料全數抓取失敗(檢查網路/PROXY_URL 或來源是否可達)")
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC (TWSE BFI82U)"),
        "unit": "元",
        "days": collected,
    }


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    try:
        data = fetch_chip_flow(n)
    except Exception as exc:  # noqa: BLE001
        print(f"三大法人抓取失敗:{exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(0)
