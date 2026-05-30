"""etf_fetcher.py — 透過(NAS)代理抓 MoneyDJ 的 ETF 成分股,建立反查資料庫。

資料流:
  etf_sources.json(列出要收錄的 ETF 與其 MoneyDJ etfid)
        │  經 PROXY_URL 代理 → 抓 MoneyDJ 成分股頁 → 解析表格
        ▼
  etf_holdings.json(ETF→成分股 + 個股名稱),供 app 反查「個股被哪些 ETF 持有」

設計:
  * 連線走 requests + proxies(PROXY_URL,如 http://user:pass@host:3128)。
  * 逐檔抓取,單一 ETF 失敗不影響其他;抓不到的【保留】etf_holdings.json 既有資料。
  * 禮貌性間隔,避免對來源造成負擔;僅取『成分股代號/名稱』這類事實資料。

注意:成分股屬事實資料;請自行確認對來源網站的使用符合其服務條款,並維持合理抓取頻率。
PROXY_URL 只透過環境變數 / Streamlit Secrets 提供,切勿寫進程式或進版控。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

SOURCES_PATH = Path("etf_sources.json")
HOLDINGS_PATH = Path("etf_holdings.json")

HTTP_TIMEOUT = 30
REQUEST_GAP_SEC = 0.6  # 每檔之間的禮貌性間隔
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MONEYDJ_TEMPLATE = "https://www.moneydj.com/etf/x/Basic/Basic0007.xdjhtm?etfid={etfid}"

TICKER_RE = re.compile(r"^[0-9]{4,6}[A-Z]?$")          # 台股代號:4~6 碼數字,可帶一個字母(如 00982A)
_TICKER_IN_TEXT = re.compile(r"(?<!\d)(\d{4,6}[A-Z]?)(?!\d)")
_CJK = re.compile(r"[一-鿿]")


# ---------------------------------------------------------------------------
# HTTP(透過代理)
# ---------------------------------------------------------------------------

def get_proxies(explicit: str | None = None) -> dict | None:
    """從參數或環境變數取得代理設定。回傳 requests 用的 proxies dict 或 None。"""
    url = (explicit or os.environ.get("PROXY_URL")
           or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    url = (url or "").strip()
    return {"http": url, "https": url} if url else None


def _decode(content: bytes) -> str:
    for enc in ("utf-8", "big5", "cp950"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", "replace")


def http_get(url: str, proxies: dict | None) -> str:
    import requests  # 延遲匯入

    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        proxies=proxies,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return _decode(resp.content)


# ---------------------------------------------------------------------------
# MoneyDJ 成分股頁解析
# ---------------------------------------------------------------------------

class _TableRows(HTMLParser):
    """把 HTML 所有 <tr> 拆成 [cell 文字, ...] 的列。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def parse_moneydj(html_text: str) -> list[dict]:
    """從 MoneyDJ 成分股頁解析出 [{'ticker','name'}, ...]。"""
    parser = _TableRows()
    parser.feed(html_text)

    out: list[dict] = []
    seen: set[str] = set()
    for row in parser.rows:
        ticker = ""
        for cell in row:
            m = _TICKER_IN_TEXT.search(cell)
            if m and TICKER_RE.match(m.group(1)):
                ticker = m.group(1)
                break
        if not ticker or ticker in seen:
            continue
        # 名稱:取含中文字的儲存格,去掉代號與括號
        name = ""
        for cell in row:
            if _CJK.search(cell):
                name = re.sub(r"\(?\b\d{4,6}[A-Z]?\b\)?", "", cell).strip(" ()（）-")
                if name:
                    break
        seen.add(ticker)
        out.append({"ticker": ticker, "name": name})
    return out


def fetch_moneydj(etfid: str, template: str, proxies: dict | None) -> list[dict]:
    """抓單一 ETF 的 MoneyDJ 成分股。"""
    return parse_moneydj(http_get(template.format(etfid=etfid), proxies))


# ---------------------------------------------------------------------------
# 建庫
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def crawl(proxy: str | None = None, log=print) -> dict:
    """依 etf_sources.json 透過代理抓 MoneyDJ,回傳合併後的 holdings dict(不寫檔)。"""
    sources = load_json(SOURCES_PATH)
    proxies = get_proxies(proxy)
    if proxies is None:
        raise RuntimeError("未提供 PROXY_URL,無法透過代理抓取")

    mdj = sources.get("moneydj", {})
    template = mdj.get("url_template", MONEYDJ_TEMPLATE)
    etfs = mdj.get("etfs", {})
    if not etfs:
        raise RuntimeError("etf_sources.json 的 moneydj.etfs 沒有任何 ETF")

    # 以既有 etf_holdings.json 為底,成功抓到的覆蓋上去(抓不到者保留)
    holdings = load_json(HOLDINGS_PATH)
    holdings.setdefault("etfs", {})
    holdings.setdefault("stock_names", {})

    ok = 0
    for code, info in etfs.items():
        etfid = info.get("etfid", "")
        name = info.get("name", code)
        try:
            rows = fetch_moneydj(etfid, template, proxies)
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不影響其他
            log(f"  [{code}] 抓取失敗:{exc}")
            rows = []
        if rows:
            holdings["etfs"][code] = {"name": name, "holdings": [r["ticker"] for r in rows]}
            for r in rows:
                if r["name"] and r["ticker"] not in holdings["stock_names"]:
                    holdings["stock_names"][r["ticker"]] = r["name"]
            ok += 1
            log(f"  [{code}] {name}:{len(rows)} 檔成分股")
        else:
            log(f"  [{code}] {name}:無資料,保留既有")
        time.sleep(REQUEST_GAP_SEC)

    if ok == 0:
        raise RuntimeError("所有 ETF 皆抓取失敗(檢查 PROXY_URL / etfid / 來源是否可達)")

    holdings["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d (MoneyDJ via proxy)")
    holdings["note"] = (
        "成分股由 etf_fetcher.py 透過代理自 MoneyDJ 抓取;僅供參考、非投資建議。"
    )
    log(f"完成:成功更新 {ok}/{len(etfs)} 檔 ETF,共 {len(holdings['etfs'])} 檔在庫。")
    return holdings


def update_holdings() -> int:
    """命令列 / GitHub Actions 入口:抓取並寫回 etf_holdings.json。"""
    try:
        holdings = crawl()
    except Exception as exc:  # noqa: BLE001
        print(f"ETF 建庫失敗:{exc}", file=sys.stderr)
        return 1
    HOLDINGS_PATH.write_text(
        json.dumps(holdings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已寫入 {HOLDINGS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(update_holdings())
