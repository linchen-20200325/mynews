"""etf_fetcher.py — 從官方/發行商來源抓『真實』ETF 成分股,更新 etf_holdings.json。

設計原則:
  * 來源集中在設定檔 ``etf_sources.json``:每檔 ETF 對應一個可下載的成分股清單
    (CSV 或 JSON)網址與解析欄位。端點/格式有變,只要改設定、不必動程式。
  * 逐檔抓取,單一來源失敗不影響其他;抓不到的 ETF 一律【保留】etf_holdings.json
    既有資料,不會把舊資料清掉。
  * 純標準函式庫(urllib + csv + json)。

⚠️ 重要:本機/沙箱常無對外網路,無法驗證真實抓取;請在 GitHub Actions
   (.github/workflows/update_etf.yml)上實跑,用真實回應校正設定檔。

設定檔格式(etf_sources.json):
{
  "etfs": {
    "0050": {
      "name": "元大台灣50",
      "url": "https://發行商/成分股.csv",
      "format": "csv",                // "csv" 或 "json"
      "encoding": "utf-8",            // 選填,預設 utf-8
      "ticker_field": "證券代號",      // CSV 欄名 / JSON 物件鍵
      "name_field": "證券名稱",        // 選填
      "json_path": ["data"]           // 選填:JSON 時,成分股陣列所在的路徑
    }
  }
}
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCES_PATH = Path("etf_sources.json")
HOLDINGS_PATH = Path("etf_holdings.json")

HTTP_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; mynews-etf/1.0)"
TICKER_RE = re.compile(r"^[0-9]{4,6}[A-Z]?$")  # 台股代號:4~6 碼數字,可帶一個字母(如 00982A)


def _http_get(url: str, encoding: str = "utf-8") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode(encoding, "replace")


def _dig(data, path: list[str]):
    """依 path 逐層深入 JSON,取出成分股陣列。"""
    for key in path or []:
        if isinstance(data, dict):
            data = data.get(key)
        else:
            return None
    return data


def _norm_ticker(value: str) -> str:
    """正規化股票代號:取前段、去空白(容許『2330 台積電』『2330.TW』等)。"""
    token = str(value).strip().split()[0] if str(value).strip() else ""
    token = token.split(".")[0].upper()
    return token


def parse_source(cfg: dict, raw: str) -> list[dict]:
    """依設定解析下載內容,回傳 [{'ticker','name'}, ...]。"""
    fmt = (cfg.get("format") or "csv").lower()
    ticker_field = cfg.get("ticker_field")
    name_field = cfg.get("name_field")
    rows: list[dict] = []

    if fmt == "json":
        records = _dig(json.loads(raw), cfg.get("json_path", []))
        if not isinstance(records, list):
            return rows
        for rec in records:
            if not isinstance(rec, dict):
                continue
            ticker = _norm_ticker(rec.get(ticker_field, ""))
            name = str(rec.get(name_field, "")).strip() if name_field else ""
            if TICKER_RE.match(ticker):
                rows.append({"ticker": ticker, "name": name})
    else:  # csv
        reader = csv.DictReader(io.StringIO(raw))
        for rec in reader:
            ticker = _norm_ticker(rec.get(ticker_field, ""))
            name = str(rec.get(name_field, "")).strip() if name_field else ""
            if TICKER_RE.match(ticker):
                rows.append({"ticker": ticker, "name": name})

    # 去重(同 ETF 內)
    seen: set[str] = set()
    uniq: list[dict] = []
    for r in rows:
        if r["ticker"] not in seen:
            seen.add(r["ticker"])
            uniq.append(r)
    return uniq


def fetch_etf(code: str, cfg: dict) -> list[dict]:
    """抓取單一 ETF 的成分股;失敗回空清單(不拋例外)。"""
    url = (cfg.get("url") or "").strip()
    if not url:
        return []
    try:
        raw = _http_get(url, cfg.get("encoding", "utf-8"))
        return parse_source(cfg, raw)
    except Exception as exc:  # noqa: BLE001 — 單一來源失敗不影響其他
        print(f"  [{code}] 抓取/解析失敗:{exc}", file=sys.stderr)
        return []


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def update_holdings() -> int:
    sources = load_json(SOURCES_PATH).get("etfs", {})
    if not sources:
        print("etf_sources.json 沒有任何來源設定;無事可做。", file=sys.stderr)
        return 0

    holdings = load_json(HOLDINGS_PATH)
    holdings.setdefault("etfs", {})
    holdings.setdefault("stock_names", {})

    changed = False
    for code, cfg in sources.items():
        constituents = fetch_etf(code, cfg)
        if not constituents:
            print(f"  [{code}] 無新資料,保留既有。")
            continue
        tickers = [c["ticker"] for c in constituents]
        holdings["etfs"][code] = {
            "name": cfg.get("name", code),
            "holdings": tickers,
        }
        for c in constituents:
            if c["name"] and c["ticker"] not in holdings["stock_names"]:
                holdings["stock_names"][c["ticker"]] = c["name"]
        changed = True
        print(f"  [{code}] {cfg.get('name', '')} 更新成分股 {len(tickers)} 檔。")

    if not changed:
        print("沒有任何 ETF 成功更新(可能是來源連結尚未填或皆失敗)。")
        return 0

    holdings["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d (自動抓取)")
    holdings["note"] = "成分股由 etf_fetcher.py 依 etf_sources.json 自官方/發行商來源抓取。非投資建議。"
    HOLDINGS_PATH.write_text(
        json.dumps(holdings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已更新 {HOLDINGS_PATH}(共 {len(holdings['etfs'])} 檔 ETF)。")
    return 0


if __name__ == "__main__":
    sys.exit(update_holdings())
