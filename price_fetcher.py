"""price_fetcher.py — 透過(NAS)代理抓台股收盤價,存成 stock_prices.json。

來源(官方、開放、JSON):
  * 上市:臺灣證券交易所 STOCK_DAY_ALL(每日全部上市個股收盤價)
      https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL
  * 上櫃:櫃買中心每日收盤行情(opendata)
      https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes

連線統一走 proxy_helper(PROXY_URL),沙箱無網路時抓不到屬正常;請在 Streamlit/Actions 上跑。
輸出 stock_prices.json: {"as_of": "...", "prices": {"2330": 1085.0, ...}}
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PRICES_PATH = Path("stock_prices.json")

TWSE_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

HTTP_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _http_get(url: str, proxies: dict | None) -> str:
    import requests

    verify = not bool(proxies)
    if not verify:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # noqa: BLE001
            pass
    resp = requests.get(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        proxies=proxies, timeout=HTTP_TIMEOUT, verify=verify,
    )
    resp.raise_for_status()
    return resp.text


def _to_float(value) -> float | None:
    try:
        f = float(str(value).replace(",", "").strip())
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def parse_twse(text: str) -> dict[str, float]:
    """STOCK_DAY_ALL:回傳 {代號: 收盤價}。容許『欄位字典』或『data 陣列』兩種格式。"""
    prices: dict[str, float] = {}
    data = json.loads(text)

    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("data") or data.get("aaData") or []

    for row in rows:
        code = price = None
        if isinstance(row, dict):
            code = (row.get("Code") or row.get("證券代號") or "").strip()
            price = row.get("ClosingPrice") or row.get("收盤價")
        elif isinstance(row, (list, tuple)) and len(row) >= 8:
            # STOCK_DAY_ALL 欄位:代號,名稱,成交股數,成交金額,開,高,低,收
            code, price = str(row[0]).strip(), row[7]
        f = _to_float(price)
        if code and f is not None:
            prices[code] = f
    return prices


def parse_tpex(text: str) -> dict[str, float]:
    """櫃買 opendata:每筆含 SecuritiesCompanyCode 與 Close。"""
    prices: dict[str, float] = {}
    for row in json.loads(text):
        if not isinstance(row, dict):
            continue
        code = (row.get("SecuritiesCompanyCode") or row.get("Code") or "").strip()
        f = _to_float(row.get("Close") or row.get("收盤"))
        if code and f is not None:
            prices[code] = f
    return prices


def fetch_prices(proxy: str | None = None, log=print) -> dict:
    """抓上市+上櫃收盤價,合併回傳 {"as_of","prices"}。單一來源失敗不影響另一個。"""
    try:
        import proxy_helper
        proxies = proxy_helper.get_proxy_config(proxy)
    except Exception:  # noqa: BLE001
        import os
        url = (proxy or os.environ.get("PROXY_URL") or "").strip()
        proxies = {"http": url, "https": url} if url else None

    prices: dict[str, float] = {}
    for label, url, parser in (
        ("上市 TWSE", TWSE_URL, parse_twse),
        ("上櫃 TPEx", TPEX_URL, parse_tpex),
    ):
        try:
            got = parser(_http_get(url, proxies))
            prices.update(got)
            log(f"  [{label}] {len(got)} 檔收盤價")
        except Exception as exc:  # noqa: BLE001
            log(f"  [{label}] 失敗:{exc}")

    if not prices:
        raise RuntimeError("上市與上櫃皆抓取失敗(檢查 PROXY_URL / 來源是否可達)")

    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d (TWSE/TPEx via proxy)"),
        "prices": prices,
    }


def load_prices(path: Path = PRICES_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def update_prices() -> int:
    try:
        data = fetch_prices()
    except Exception as exc:  # noqa: BLE001
        print(f"股價更新失敗:{exc}", file=sys.stderr)
        return 1
    PRICES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已寫入 {PRICES_PATH}(共 {len(data['prices'])} 檔)")
    return 0


if __name__ == "__main__":
    sys.exit(update_prices())
