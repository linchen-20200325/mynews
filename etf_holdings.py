"""ETF 持股反查 — 讀取 etf_holdings.json,反算每檔個股「被幾檔 ETF 持有」。

純資料反查:不呼叫任何 AI、不爬網。資料來源是可維護的設定檔
``etf_holdings.json``(成分股會變動,請定期核實更新)。
"""

from __future__ import annotations

import re
from pathlib import Path

import paths  # 路徑 SSOT

DEFAULT_PATH = paths.ETF_HOLDINGS


def load_holdings(path: Path = DEFAULT_PATH) -> dict | None:
    """讀取設定檔;不存在或格式錯誤回 None。"""
    return paths.read_json(path)


def _iter_etfs(data: dict):
    """逐一產出 (etf_code, etf_name, holdings_list)。"""
    for code, info in (data.get("etfs") or {}).items():
        if isinstance(info, dict):
            yield code, info.get("name", code), info.get("holdings", []) or []
        elif isinstance(info, list):  # 容許直接給成分股陣列
            yield code, code, info


# 台灣 ETF 代號以 0 開頭：4-6 位數字加選用大寫字母（含 0050/0056/00878/00981A/006205）
_ETF_CODE_RE = re.compile(r"^0\d{3,5}[A-Za-z]?$")


def reverse_index(data: dict) -> list[dict]:
    """反查:回傳 [{ticker, name, etf_count, etfs:[{code,name}]}],依檔數由高到低。"""
    names = data.get("stock_names", {}) or {}
    etf_codes = set((data.get("etfs") or {}).keys())
    holders: dict[str, list[dict]] = {}
    for code, etf_name, holdings in _iter_etfs(data):
        for ticker in holdings:
            ticker_norm = str(ticker).zfill(4) if str(ticker).isdigit() else str(ticker)
            if _ETF_CODE_RE.match(ticker_norm) or ticker_norm in etf_codes:
                continue
            holders.setdefault(ticker_norm, []).append({"code": code, "name": etf_name})

    rows = [
        {
            "ticker": ticker,
            "name": names.get(ticker, ""),
            "etf_count": len(lst),
            "etfs": sorted(lst, key=lambda e: e["code"]),
        }
        for ticker, lst in holders.items()
        if not (_ETF_CODE_RE.match(ticker) or ticker in etf_codes)
    ]
    rows.sort(key=lambda r: (r["etf_count"], r["ticker"]), reverse=True)
    return rows


# 交易所後綴（顯示雜訊）：yfinance 風格代號可能帶 .TW/.TWO，顯示前去除。
_EXCHANGE_SUFFIX_RE = re.compile(r"\.(TW|TWO)$", re.IGNORECASE)


def name_for(ticker: str, data: dict | None = None) -> str:
    """查代號的正式名稱（SSOT）：ETF 代號查 ``etfs[].name``、個股查 ``stock_names``；查無回空字串。

    杜絕下游（如盯盤推播）讓 AI 猜 ETF/個股名（硬規則#3）：名稱只認本地維護資料，
    例如主動式 ETF ``00980A`` → 「主動野村臺灣優選」，不再讓 Gemini 幻覺成別檔的名字。
    ``data`` 可傳入已載入的 holdings 省重讀；未傳則自行 ``load_holdings()``。
    """
    t = (ticker or "").strip().upper()
    if not t:
        return ""
    if data is None:
        data = load_holdings()
    if not isinstance(data, dict):
        return ""
    etf = (data.get("etfs") or {}).get(t)
    if isinstance(etf, dict) and etf.get("name"):
        name = str(etf["name"]).strip()
    else:
        name = str((data.get("stock_names") or {}).get(t, "")).strip()
    return _EXCHANGE_SUFFIX_RE.sub("", name).strip()
