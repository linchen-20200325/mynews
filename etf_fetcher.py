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

import paths  # 路徑 SSOT

SOURCES_PATH = paths.ETF_SOURCES
HOLDINGS_PATH = paths.ETF_HOLDINGS

HTTP_TIMEOUT = 30
REQUEST_GAP_SEC = 0.6  # 每檔之間的禮貌性間隔
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MONEYDJ_TEMPLATE = "https://www.moneydj.com/etf/x/Basic/Basic0007.xdjhtm?etfid={etfid}"
# 主動式 ETF(代號 A 結尾)的持股頁是 Basic0007B,與被動式 Basic0007 不同
MONEYDJ_ACTIVE_TEMPLATE = "https://www.moneydj.com/etf/x/Basic/Basic0007B.xdjhtm?etfid={etfid}"

TICKER_RE = re.compile(r"^[0-9]{4,6}[A-Z]?$")          # 台股代號:4~6 碼數字,可帶一個字母(如 00982A)
_TICKER_IN_TEXT = re.compile(r"(?<!\d)(\d{4,6}[A-Z]?)(?!\d)")
_CJK = re.compile(r"[一-鿿]")


# ---------------------------------------------------------------------------
# HTTP(透過代理)
# ---------------------------------------------------------------------------

def get_proxies(explicit: str | None = None) -> dict | None:
    """取得 NAS 中繼站代理設定。回傳 requests 用的 proxies dict 或 None。

    統一走 proxy_helper(支援 explicit > 環境變數 PROXY_URL > Streamlit secrets);
    proxy_helper 不可用時退回純環境變數,確保 GitHub Actions 也能運作。
    """
    try:
        import proxy_helper
        return proxy_helper.get_proxy_config(explicit)
    except Exception:  # noqa: BLE001 — 無 proxy_helper 時退回環境變數
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

    # 走 NAS 代理時跳過 SSL 驗證 — Squid CONNECT 隧道與 MoneyDJ 憑證不相容,
    # verify=True 會在 SSL 階段拋例外(「能連線卻每檔都抓不到」的真因);
    # 直連模式(無 proxy)則正常驗證。比照 proxy_helper / 基金 infra.proxy 行為。
    verify = not bool(proxies)
    if not verify:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # noqa: BLE001
            pass

    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        proxies=proxies,
        timeout=HTTP_TIMEOUT,
        verify=verify,
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


def _is_active_etf(code: str, etfid: str) -> bool:
    """主動式 ETF:代號 A 結尾(如 00982A)。"""
    base = str(etfid or code).split(".")[0].upper()
    return base.endswith("A")


def _etfid_variants(etfid: str) -> list[str]:
    """MoneyDJ etfid 大小寫變體(站方網址多為小寫,主動式字母後綴尤其常需小寫 a)。

    依序:原值 → 字母後綴轉小寫(保留 .TW)→ 全小寫。去重後回傳。
    """
    variants = [etfid]
    suf_low = re.sub(
        r"([0-9]+)([A-Za-z])(\.[A-Za-z]{2})$",
        lambda m: m.group(1) + m.group(2).lower() + m.group(3), etfid,
    )
    for v in (suf_low, etfid.lower()):
        if v not in variants:
            variants.append(v)
    return variants


def fetch_moneydj(etfid: str, template: str, proxies: dict | None, code: str = "") -> list[dict]:
    """抓單一 ETF 的 MoneyDJ 成分股。

    主動式 ETF(A 結尾)優先用 Basic0007B、一般 ETF 用傳入的 template(Basic0007),
    兩個頁面都試;另對 etfid 嘗試大小寫變體(站方多為小寫,主動式後綴常需小寫 a),
    回傳第一個有成分股的結果。所有組合只在前一個無資料時才續試,成功的 ETF 不增延遲。
    """
    primary = MONEYDJ_ACTIVE_TEMPLATE if _is_active_etf(code, etfid) else template
    alt = template if primary == MONEYDJ_ACTIVE_TEMPLATE else MONEYDJ_ACTIVE_TEMPLATE
    for eid in _etfid_variants(etfid):
        for tmpl in (primary, alt):
            try:
                rows = parse_moneydj(http_get(tmpl.format(etfid=eid), proxies))
            except Exception:  # noqa: BLE001 — 單一頁面/變體失敗就試下一個
                continue
            if rows:
                return rows
    return []


MONEYDJ_INFO_TEMPLATE = "https://www.moneydj.com/etf/x/Basic/Basic0001.xdjhtm?etfid={etfid}"
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_etf_name(html_text: str) -> str:
    """從 MoneyDJ 頁面 <title> 解析 ETF 名稱(去掉代號、站名等雜訊)。"""
    m = _TITLE_RE.search(html_text)
    if not m:
        return ""
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    # 站名/分隔:取第一段(常見「名稱(代號)-ETF基本資料-MoneyDJ理財網」)
    title = re.split(r"[-_|｜–—]", title)[0].strip()
    # 去掉括號內代號與「ETF」字樣
    title = re.sub(r"[(（]\s*[0-9]{4,6}[A-Z]?(?:\.TW)?\s*[)）]", "", title)
    title = re.sub(r"\b[0-9]{4,6}[A-Z]?(?:\.TW)?\b", "", title)
    title = title.replace("ETF基本資料", "").replace("基本資料", "").strip(" -－()（）")
    return title


def fetch_etf_name(etfid: str, proxies: dict | None) -> str:
    """透過代理抓某 ETF 的中文名稱;失敗回空字串。"""
    try:
        return extract_etf_name(http_get(MONEYDJ_INFO_TEMPLATE.format(etfid=etfid), proxies))
    except Exception:  # noqa: BLE001
        return ""


# MoneyDJ 台股 ETF 列表頁(全市場);用代號連結反推出所有上市/上櫃 ETF
MONEYDJ_LIST_URL = "https://www.moneydj.com/etf/eb/et081001.djhtm"
# 連結中的 etfid 形如 ?etfid=0050.TW / 00982a.tw,擷取代號
_LIST_ETFID_RE = re.compile(r"etfid=([0-9]{4,6}[A-Za-z]?)\.[Tt][Ww]", re.IGNORECASE)


def parse_etf_list(html_text: str) -> list[str]:
    """從 MoneyDJ ETF 列表頁解析出所有台股 ETF 代號(去重、保序、大寫)。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _LIST_ETFID_RE.finditer(html_text):
        code = m.group(1).upper()
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def fetch_all_etf_codes(proxy: str | None = None, url: str = MONEYDJ_LIST_URL) -> list[str]:
    """透過代理抓 MoneyDJ ETF 列表頁,回傳全台股 ETF 代號清單。"""
    proxies = get_proxies(proxy)
    if proxies is None:
        raise RuntimeError("未提供 PROXY_URL,無法透過代理抓取")
    return parse_etf_list(http_get(url, proxies))


def import_all_etfs(proxy: str | None = None, sources: dict | None = None,
                    log=print) -> tuple[dict, int, int]:
    """抓全市場 ETF 代號並併入清單(只補沒有的;名稱留待後續自動抓)。

    回傳 (更新後 sources, 新增數, 全市場總數)。
    """
    if sources is None:
        sources = load_sources()
    codes = fetch_all_etf_codes(proxy)
    etfs = sources["moneydj"]["etfs"]
    added = 0
    for code in codes:
        if code not in etfs:
            etfs[code] = {"name": code, "etfid": f"{code}.TW"}
            added += 1
    log(f"全市場 ETF {len(codes)} 檔;新增 {added} 檔(原 {len(etfs) - added} 檔)。")
    return sources, added, len(codes)


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


def crawl(proxy: str | None = None, log=print, sources: dict | None = None) -> dict:
    """依清單透過代理抓 MoneyDJ,回傳合併後的 holdings dict(不寫檔)。

    sources 可由呼叫端傳入(如 Streamlit session 內的最新清單);
    未傳入時才從磁碟 etf_sources.json 讀取。
    """
    if sources is None:
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
    failed: list[dict] = []
    for code, info in etfs.items():
        etfid = info.get("etfid", "")
        name = info.get("name", code)
        try:
            rows = fetch_moneydj(etfid, template, proxies, code=code)
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不影響其他
            log(f"  [{code}] 抓取失敗:{exc}")
            failed.append({"code": code, "name": name, "etfid": etfid, "reason": str(exc)})
            rows = []
            time.sleep(REQUEST_GAP_SEC)
            continue
        if rows:
            holdings["etfs"][code] = {"name": name, "holdings": [r["ticker"] for r in rows]}
            for r in rows:
                if r["name"] and r["ticker"] not in holdings["stock_names"]:
                    holdings["stock_names"][r["ticker"]] = r["name"]
            ok += 1
            log(f"  [{code}] {name}:{len(rows)} 檔成分股")
        else:
            failed.append({"code": code, "name": name, "etfid": etfid, "reason": "無成分股資料"})
            log(f"  [{code}] {name}:無資料,保留既有")
        time.sleep(REQUEST_GAP_SEC)

    if ok == 0:
        raise RuntimeError("所有 ETF 皆抓取失敗(檢查 PROXY_URL / etfid / 來源是否可達)")

    holdings["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%d (MoneyDJ via proxy)")
    holdings["note"] = (
        "成分股由 etf_fetcher.py 透過代理自 MoneyDJ 抓取;僅供參考、非投資建議。"
    )
    holdings["_crawl_stats"] = {"total": len(etfs), "ok": ok, "failed": failed}
    log(f"完成:成功更新 {ok}/{len(etfs)} 檔 ETF,共 {len(holdings['etfs'])} 檔在庫。")
    return holdings


def update_holdings() -> int:
    """命令列 / GitHub Actions 入口:抓取並寫回 etf_holdings.json。"""
    try:
        holdings = crawl()
    except Exception as exc:  # noqa: BLE001
        print(f"ETF 建庫失敗:{exc}", file=sys.stderr)
        return 1
    holdings.pop("_crawl_stats", None)  # 統計資訊不寫入持久化檔案
    HOLDINGS_PATH.write_text(
        json.dumps(holdings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已寫入 {HOLDINGS_PATH}")
    return 0


# ---------------------------------------------------------------------------
# 來源清單管理(供網頁新增 ETF)
# ---------------------------------------------------------------------------

def load_sources() -> dict:
    """讀取 etf_sources.json;確保有 moneydj.etfs 結構。"""
    data = load_json(SOURCES_PATH)
    data.setdefault("moneydj", {})
    data["moneydj"].setdefault("url_template", MONEYDJ_TEMPLATE)
    data["moneydj"].setdefault("etfs", {})
    return data


def normalize_code(code: str) -> str:
    """正規化 ETF 代號:去空白、轉大寫(A 結尾主動式)。"""
    return str(code).strip().upper()


def add_etf(
    code: str, name: str = "", sources: dict | None = None, proxies: dict | None = None
) -> tuple[bool, str, dict]:
    """在來源清單新增一檔 ETF(含重複檢查;name 留空時透過代理自動抓)。

    回傳 (是否新增成功, 訊息, 更新後的 sources dict)。
    不直接寫檔,由呼叫端決定要存檔或下載(雲端唯讀環境用下載)。
    """
    if sources is None:
        sources = load_sources()
    code = normalize_code(code)
    name = str(name).strip()

    if not re.match(r"^[0-9]{4,6}[A-Z]?$", code):
        return False, f"代號格式怪怪的:「{code}」(應為 4~6 碼數字,可帶一個字母,如 00982A)", sources

    etfs = sources["moneydj"]["etfs"]
    if code in etfs:
        exist = etfs[code].get("name", "")
        return False, f"清單已經有 {code}（{exist}),未重複加入。", sources

    etfid = f"{code}.TW"
    if not name and proxies is not None:
        name = fetch_etf_name(etfid, proxies)

    etfs[code] = {"name": name or code, "etfid": etfid}
    label = name or "(名稱待抓取)"
    return True, f"已加入 {code} {label}。", sources


def parse_codes(text: str) -> list[str]:
    """從一段文字解析出多個 ETF 代號(以逗號/空白/換行/頓號分隔)。"""
    raw = re.split(r"[,\s、;]+", str(text or ""))
    out: list[str] = []
    for tok in raw:
        c = normalize_code(tok)
        if c and c not in out:
            out.append(c)
    return out


def add_etfs_bulk(
    text: str, sources: dict | None = None, proxies: dict | None = None
) -> tuple[dict, list[str]]:
    """批次新增多檔 ETF(只輸入代號,名稱自動抓)。回傳 (更新後 sources, 每檔訊息清單)。"""
    if sources is None:
        sources = load_sources()
    msgs: list[str] = []
    for code in parse_codes(text):
        ok, msg, sources = add_etf(code, "", sources, proxies)
        msgs.append(("✅ " if ok else "⚠️ ") + msg)
    if not msgs:
        msgs.append("沒有解析到任何代號。")
    return sources, msgs


def save_sources(sources: dict) -> None:
    """寫回 etf_sources.json(本機/可寫環境用)。"""
    SOURCES_PATH.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def remove_etf(code: str, sources: dict | None = None) -> tuple[bool, str, dict]:
    """從來源清單移除一檔 ETF。回傳 (是否移除, 訊息, 更新後 sources)。"""
    if sources is None:
        sources = load_sources()
    code = normalize_code(code)
    etfs = sources["moneydj"]["etfs"]
    if code in etfs:
        name = etfs[code].get("name", "")
        del etfs[code]
        return True, f"已移除 {code} {name}。", sources
    return False, f"清單中沒有 {code},無法移除。", sources


if __name__ == "__main__":
    sys.exit(update_holdings())
