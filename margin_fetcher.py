"""margin_fetcher.py — 抓證交所「融資餘額」最新一日(散戶槓桿/斷頭訊號)。

來源:台灣證券交易所 MI_MARGN(信用交易統計,MS 彙總)。直連即可(同 BFI82U)。
用途:融資餘額單日大減 = 去槓桿/斷頭賣壓,供「多重賣壓共振」偵測其中一力。

【真實性】數字取自證交所原始 JSON(融資金額,仟元→換算為元),不經 AI 估算。

輸出:fetch_margin() -> {
  "as_of": "YYYY-MM-DD HH:MM UTC (TWSE MI_MARGN)",
  "date": "YYYY-MM-DD",
  "margin_today": <int 元>, "margin_prev": <int 元>,
  "margin_chg": <int 元>, "margin_chg_pct": <float %>,
}  抓不到回 None。
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone

import numutil  # 漲跌幅公式 + 方向對帳的單一真相源(SSOT)

MI_MARGN_URL = (
    "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
    "?date={d}&selectType=MS&response=json"
)
HTTP_TIMEOUT = 20
MAX_LOOKBACK = 14


def _fetch_json(date_str: str) -> dict | None:
    import proxy_helper
    return proxy_helper.fetch_json(MI_MARGN_URL.format(d=date_str), timeout=HTTP_TIMEOUT)


def _parse(payload: dict, date_str: str) -> dict | None:
    """從 MI_MARGN 找『融資金額(仟元)』列的前日餘額/今日餘額,換算為元。無資料回 None。"""
    if not payload or payload.get("stat") != "OK":
        return None
    # 新版回傳 tables;舊版可能是頂層 fields/data。統一蒐集 (fields, rows)。
    blocks = []
    for tb in payload.get("tables", []) or []:
        blocks.append((tb.get("fields", []), tb.get("data", []) or []))
    if payload.get("data"):
        blocks.append((payload.get("fields", []), payload["data"]))

    for fields, rows in blocks:
        # 找前日/今日餘額欄位索引(找不到就用慣例位置)
        def col(*names, default):
            for i, f in enumerate(fields):
                if any(n in str(f) for n in names):
                    return i
            return default
        i_prev = col("前日餘額", default=4)
        i_today = col("今日餘額", default=5)
        for row in rows:
            if not row:
                continue
            label = str(row[0])
            # 鎖定融資「金額」列(仟元),避開融資張數與融券
            if "融資" in label and ("仟元" in label or "金額" in label):
                if max(i_prev, i_today) >= len(row):
                    continue
                prev_k = numutil.parse_number(row[i_prev], as_int=True, default=0)
                today_k = numutil.parse_number(row[i_today], as_int=True, default=0)
                if prev_k <= 0:  # 無有效前值 → 不計%(不以 0 充數),續找下一列
                    continue
                today, prev = today_k * 1000, prev_k * 1000  # 仟元 → 元
                chg = today - prev
                pct = numutil.pct_change(today, prev)  # 含 prev>0 與方向對帳不變量
                return {
                    "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
                    "margin_today": today, "margin_prev": prev,
                    "margin_chg": chg, "margin_chg_pct": pct,
                }
    return None


def fetch_margin(log=print) -> dict | None:
    """抓最近一個交易日的融資餘額;自動跳過週末/假日。抓不到回 None。"""
    d = date.today()
    for _ in range(MAX_LOOKBACK):
        if d.weekday() < 5:
            parsed = _parse(_fetch_json(d.strftime("%Y%m%d")), d.strftime("%Y%m%d"))
            if parsed:
                log(f"  融資餘額 {parsed['date']}:{parsed['margin_today']/1e8:.0f}億"
                    f"({parsed['margin_chg_pct']:+.2f}%)")
                parsed["as_of"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC (TWSE MI_MARGN)")
                return parsed
        d -= timedelta(days=1)
    return None


if __name__ == "__main__":
    data = fetch_margin()
    if not data:
        print("融資餘額抓取失敗", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(0)
