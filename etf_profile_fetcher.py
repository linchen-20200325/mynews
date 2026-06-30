"""etf_profile_fetcher.py — 透過(NAS)代理抓 MoneyDJ ETF 基本資料,建立『ETF 圖鑑』。

資料流:
  etf_sources.json(沿用同一份 ETF 清單)
        │  經 PROXY_URL 代理 → 抓 MoneyDJ 基本資料頁(Basic0001)→ 解析欄位
        ▼
  etf_profiles.json:每檔 ETF 的型態/區域/配息/費用/策略/主題標籤,供圖鑑頁篩選

抓取的欄位(盡量,抓不到就留空):
  category(資產型態)、region(投資區域)、dividend_freq(配息頻率)、
  dividend_months(配息月份)、mgmt_fee(經理費%)、custody_fee(保管費%)、
  index_tracked(追蹤指數)、strategy(主動/被動)、themes(主題標籤)、issuer(發行商)

連線統一走 proxy_helper(PROXY_URL);沙箱無網路抓不到屬正常,請在 Streamlit/Actions 上跑。
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import config  # 環境變數讀取 SSOT
import paths   # 路徑 SSOT

SOURCES_PATH = paths.ETF_SOURCES
PROFILES_PATH = paths.ETF_PROFILES

HTTP_TIMEOUT = 30
REQUEST_GAP_SEC = 0.6
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# 注意:Basic0001 是「即時報價頁」,沒有基金種類/配息/費用。
# ETF 簡介/特色(投資地區、計價、成立日、經理費、保管費、配息、追蹤指數)在 Basic0004。
BASIC_TEMPLATE = "https://www.moneydj.com/etf/x/Basic/Basic0004.xdjhtm?etfid={etfid}"
# Basic0005 = 配息記錄頁(含除息日,近兩年),可反推『真實配息月份』
DIVIDEND_TEMPLATE = "https://www.moneydj.com/etf/x/Basic/Basic0005.xdjhtm?etfid={etfid}"
PAGE_TEMPLATES = {
    "0004": "https://www.moneydj.com/etf/x/Basic/Basic0004.xdjhtm?etfid={etfid}",
    "0005": "https://www.moneydj.com/etf/x/Basic/Basic0005.xdjhtm?etfid={etfid}",
    "0003": "https://www.moneydj.com/etf/x/Basic/Basic0003.xdjhtm?etfid={etfid}",
    "0001": "https://www.moneydj.com/etf/x/Basic/Basic0001.xdjhtm?etfid={etfid}",
}

# ---- 標準化選項(供篩選器固定選單)---------------------------------------

CATEGORIES = ["股票型", "債券型", "平衡/多重資產型", "貨幣市場型", "商品型", "不動產REITs型", "槓桿/反向型", "其他"]
REGIONS = ["台灣", "美國", "中國", "日本", "其他亞洲", "已開發", "新興市場", "全球", "其他"]
DIVIDEND_FREQS = ["不配息(累積)", "年配", "半年配", "季配", "雙月配", "月配", "不定期"]


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
        url, headers={"User-Agent": USER_AGENT}, proxies=proxies,
        timeout=HTTP_TIMEOUT, verify=verify,
    )
    resp.raise_for_status()
    for enc in ("utf-8", "big5", "cp950"):
        try:
            return resp.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return resp.content.decode("utf-8", "replace")


# ---- HTML 解析:把基本資料頁的「標籤:值」表格拆成 dict --------------------

class _KVParser(HTMLParser):
    """擷取所有表格儲存格文字,後續用相鄰配對成『欄位名→值』。"""

    def __init__(self) -> None:
        super().__init__()
        self.cells: list[str] = []
        self._buf: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self._buf = []

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._buf is not None:
            self.cells.append(re.sub(r"\s+", " ", "".join(self._buf)).strip())
            self._buf = None


_LABEL_RE = re.compile(r"[一-鿿]")  # 欄位名應含中文
_PRICEY_RE = re.compile(r"^[0-9.,%+\-X\s]+$")  # 純數字/價格/符號 → 不是欄位名


def _looks_like_label(text: str) -> bool:
    """判斷某儲存格是否像『欄位名』(短、含中文、非純數字/價格)。"""
    t = text.rstrip(":： ").strip()
    if not t or len(t) > 10:
        return False
    if _PRICEY_RE.match(t):
        return False
    return bool(_LABEL_RE.search(t))


def _kv_pairs(html_text: str) -> dict[str, str]:
    """把連續儲存格配對成 {欄位名: 值}。

    只在『前一格像欄位名、後一格不像欄位名』時才配對,避免報價頁那種
    每格互相串成 key 的雜訊(98776→買價與股數→...)。
    """
    p = _KVParser()
    p.feed(html_text)
    cells = [c for c in p.cells if c]
    kv: dict[str, str] = {}
    for i in range(len(cells) - 1):
        key, val = cells[i].rstrip(":： ").strip(), cells[i + 1].strip()
        # 只要『前一格像欄位名』就配對;擋掉報價頁那種 key 是數字/價格的雜訊
        if _looks_like_label(key) and val and key not in kv:
            kv[key] = val
    return kv


def _pct(text: str) -> float | None:
    """取第一個數字當百分比值(欄位本身已是 % 欄,如『經理費(%)→0.4』,不一定帶 % 符號)。"""
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", text or "")
    return float(m.group(1)) if m else None


def _scale_million(text: str) -> float | None:
    """ETF 規模 → 百萬台幣數字。例『575,078.00(百萬台幣)…』→ 575078.0。"""
    m = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _classify_category(text: str) -> str:
    t = text or ""
    if any(k in t for k in ("債", "公債", "債券")):
        return "債券型"
    if any(k in t for k in ("槓桿", "反向", "正2", "反1")):
        return "槓桿/反向型"
    if any(k in t for k in ("貨幣", "貨幣市場")):
        return "貨幣市場型"
    if any(k in t for k in ("不動產", "REIT")):
        return "不動產REITs型"
    if any(k in t for k in ("商品", "黃金", "原油", "白銀")):
        return "商品型"
    if any(k in t for k in ("平衡", "多重資產", "組合")):
        return "平衡/多重資產型"
    if any(k in t for k in ("股票", "指數股票", "成分股", "ETF", "股")):
        return "股票型"
    return "其他"


def _classify_region(text: str) -> str:
    t = text or ""
    pairs = [
        ("台灣", ("台灣", "臺灣", "上市", "上櫃")),
        ("美國", ("美國", "標普", "S&P", "那斯達克", "費城")),
        ("中國", ("中國", "中國大陸", "A股", "滬深", "陸股")),
        ("日本", ("日本", "日經", "東證")),
        ("新興市場", ("新興",)),
        ("已開發", ("已開發", "成熟")),
        ("全球", ("全球", "世界", "環球")),
        ("其他亞洲", ("越南", "印度", "韓國", "東協", "亞太", "亞洲")),
    ]
    for label, keys in pairs:
        if any(k in t for k in keys):
            return label
    return "其他"


def _dividend_freq(text: str) -> str:
    t = text or ""
    if any(k in t for k in ("不配", "累積", "不分配")):
        return "不配息(累積)"
    if "月" in t and any(k in t for k in ("雙月", "每雙月", "偶數月", "奇數月")):
        return "雙月配"
    if any(k in t for k in ("每月", "月配", "月月")):
        return "月配"
    if any(k in t for k in ("每季", "季配", "季")):
        return "季配"
    if any(k in t for k in ("半年",)):
        return "半年配"
    if any(k in t for k in ("每年", "年配", "年")):
        return "年配"
    return "不定期"


def _months(text: str) -> list[int]:
    """從配息月份描述抓出月份數字(支援『1,4,7,10月』『1、4、7、10 月』等串列)。"""
    t = text or ""
    months: set[int] = set()
    # 先抓「數字串 + 月」整段(如 1,4,7,10月),再從整段拆出所有數字
    for seg in re.findall(r"([0-9][0-9,，、\s]*)\s*月", t):
        for m in re.findall(r"[0-9]{1,2}", seg):
            if 1 <= int(m) <= 12:
                months.add(int(m))
    return sorted(months)


# 依配息頻率推測月份(台灣 ETF 慣例;季配/雙月配/半年配各有版本,僅供參考)
_FREQ_DEFAULT_MONTHS = {
    "月配": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],   # 確定:每月都配
    "季配": [1, 4, 7, 10],                              # 推測:最常見版本(另有 2/5/8/11、3/6/9/12)
    "雙月配": [1, 3, 5, 7, 9, 11],                      # 推測
    "半年配": [6, 12],                                  # 推測
    "年配": [10],                                       # 推測
}


def _infer_months(freq: str) -> tuple[list[int], bool]:
    """依頻率推測配息月份。回傳 (月份清單, 是否為推測值)。
    月配為確定(1~12);其餘為推測,前端會標註(推測)。"""
    months = _FREQ_DEFAULT_MONTHS.get(freq, [])
    is_estimate = freq != "月配"  # 只有月配是確定的
    return months, (is_estimate and bool(months))


def _strategy(text_all: str) -> str:
    return "主動式" if ("主動" in text_all and "被動" not in text_all) else "被動(追蹤指數)"


def _themes(name: str, text_all: str) -> list[str]:
    # 只用『ETF 名稱 + 追蹤指數』判斷主題,避免被整頁其他區塊(相關 ETF 清單等)汙染
    blob = f"{name} {text_all}"[:200]
    rules = {
        "高息低波": ("高息低波", "低波"),
        "高股息": ("高股息", "高息", "股利"),
        "市值型": ("市值", "台灣50", "台50", "中型100"),
        "ESG永續": ("ESG", "永續", "低碳"),
        "等權重": ("等權",),
        "Smart Beta/多因子": ("多因子", "factor", "Smart"),
        "半導體": ("半導體", "晶圓", "IC設計"),
        "AI": ("AI", "人工智慧"),
        "5G": ("5G",),
        "電動車/綠能": ("電動車", "綠能", "電池", "再生能源"),
        "科技": ("科技", "電子", "技術"),
        "金融": ("金融", "銀行"),
        "政府公債": ("公債", "政府債"),
        "投資等級債": ("投資等級", "投等"),
        "非投資等級債": ("非投資等級", "高收益", "高息債"),
        "新興市場債": ("新興市場債", "新興債"),
    }
    out = []
    for tag, keys in rules.items():
        if any(k in blob for k in keys):
            out.append(tag)
    return out


def parse_profile(html_text: str, code: str, name: str) -> dict:
    """把 MoneyDJ Basic0004(ETF 簡介頁)解析成一筆 profile。"""
    kv = _kv_pairs(html_text)

    def find(*keys: str) -> str:
        # 完全比對優先,再退而求其次做包含比對
        for key in keys:
            if key in kv:
                return kv[key]
        for k in kv:
            if any(key in k for key in keys):
                return kv[k]
        return ""

    cat_raw = find("投資標的", "基金種類", "ETF種類", "類型")        # 例:股票型 / 債券型
    region_raw = find("投資區域", "投資地區")                       # 例:台灣 / 美國
    div_raw = find("配息頻率", "收益分配", "配息")                  # 例:季配 / 月配 / 不配息
    mgmt_raw = find("經理費(%)", "經理費", "管理費")               # 例:0.4
    total_fee_raw = find("總管理費用(%)", "總管理費用", "總費用")   # 例:0.57 (含...)
    index_raw = find("追蹤指數", "標的指數", "基準指數")
    issuer_raw = find("發行公司", "發行", "投信")
    manager_raw = find("經理人")
    scale_raw = find("ETF規模", "基金規模", "規模")
    yield_raw = find("殖利率(%)", "殖利率")
    price_raw = find("ETF市價", "市價", "成交價")           # 例:50.2000（05/29）
    nav_raw = find("ETF淨值", "淨值")                       # 例:50.1500（05/29）
    strategy_text = find("投資策略", "投資風格")
    size_m = _scale_million(scale_raw)

    freq = _dividend_freq(div_raw or name)
    actual_months = _months(div_raw)  # 頁面若直接寫月份就用實際的
    if actual_months:
        months, est = actual_months, False
    else:
        months, est = _infer_months(freq)

    return {
        "code": code,
        "name": name,
        "issuer": issuer_raw,
        "manager": manager_raw,
        "category": _classify_category(cat_raw or name),
        "category_raw": cat_raw,
        "region": _classify_region(region_raw or index_raw or name),
        "dividend_freq": freq,
        "dividend_months": months,
        "months_estimated": est,
        "mgmt_fee": _pct(mgmt_raw),
        "total_fee": _pct(total_fee_raw),
        "yield_pct": _pct(yield_raw),
        "price": _pct(price_raw),
        "nav": _pct(nav_raw),
        "scale_million": size_m,
        "index_tracked": index_raw,
        "strategy": _strategy(f"{strategy_text} {cat_raw} {name}"),
        "strategy_text": strategy_text,
        "themes": _themes(name, f"{name} {index_raw} {cat_raw}"),
    }


def diagnose(etfid: str, proxy: str | None = None, page: str = "0004") -> dict:
    """診斷:抓指定基本資料頁(預設 Basic0004 簡介頁),回傳解析出的『欄位名→值』。"""
    proxies = get_proxies(proxy)
    if proxies is None:
        raise RuntimeError("未提供 PROXY_URL")
    tmpl = PAGE_TEMPLATES.get(page, BASIC_TEMPLATE)
    return _kv_pairs(_http_get(tmpl.format(etfid=etfid), proxies))


_DATE_RE = re.compile(r"(20[0-9]{2})[/\-年.](1[0-2]|0?[1-9])[/\-月.](3[01]|[12][0-9]|0?[1-9])")


def parse_dividend_months(html_text: str) -> list[int]:
    """從 Basic0005 配息記錄頁的『除息日』反推真實配息月份。

    逐列解析,每列只取『第一個日期』(除息日),避免把發放日月份也算進去
    (除息 1/4/7/10、發放 2/5/8/11 會混在一起)。近兩年記錄足以涵蓋完整週期。
    """
    class _Rows(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list[list[str]] = []
            self._row = None
            self._cell = None

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
                self._row.append("".join(self._cell).strip())
                self._cell = None
            elif tag == "tr" and self._row is not None:
                self.rows.append(self._row)
                self._row = None

    parser = _Rows()
    parser.feed(html_text)
    months: set[int] = set()
    for row in parser.rows:
        for cell in row:  # 每列取第一個出現的日期(=除息日)就停
            m = _DATE_RE.search(cell)
            if m:
                mo = int(m.group(2))
                if 1 <= mo <= 12:
                    months.add(mo)
                break
    return sorted(months)


def fetch_dividend_months(etfid: str, proxies: dict | None) -> list[int]:
    """抓 Basic0005,回傳真實配息月份;失敗回空清單。"""
    try:
        return parse_dividend_months(_http_get(DIVIDEND_TEMPLATE.format(etfid=etfid), proxies))
    except Exception:  # noqa: BLE001
        return []


def get_proxies(proxy: str | None = None) -> dict | None:
    try:
        import proxy_helper
        return proxy_helper.get_proxy_config(proxy)
    except Exception:  # noqa: BLE001
        url = (proxy or config.env_str("PROXY_URL") or "").strip()
        return {"http": url, "https": url} if url else None


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def crawl(proxy: str | None = None, log=print, sources: dict | None = None) -> dict:
    """依清單抓每檔 ETF 基本資料,回傳 {"as_of","profiles":{code:profile}}。

    sources 可由呼叫端傳入(Streamlit session 最新清單);未傳入才讀磁碟。
    """
    proxies = get_proxies(proxy)
    if proxies is None:
        raise RuntimeError("未提供 PROXY_URL,無法透過代理抓取")

    if sources is None:
        sources = load_json(SOURCES_PATH)
    etfs = sources.get("moneydj", {}).get("etfs", {})
    if not etfs:
        raise RuntimeError("etf_sources.json 沒有任何 ETF")

    existing = load_json(PROFILES_PATH).get("profiles", {})
    profiles = dict(existing)  # 抓不到者保留既有

    ok = 0
    for code, info in etfs.items():
        etfid = info.get("etfid", f"{code}.TW")
        name = info.get("name", code)
        try:
            html = _http_get(BASIC_TEMPLATE.format(etfid=etfid), proxies)
            prof = parse_profile(html, code, name)
            # 進一步抓配息記錄頁(Basic0005),用真實除息月份覆蓋頻率推測值
            real_months = fetch_dividend_months(etfid, proxies)
            if real_months:
                prof["dividend_months"] = real_months
                prof["months_estimated"] = False
            profiles[code] = prof
            ok += 1
            mon = "、".join(str(m) for m in prof["dividend_months"]) or "—"
            tag = "" if not prof["months_estimated"] else "(推測)"
            log(f"  [{code}] {name}:{prof['category']}/{prof['region']}/{prof['dividend_freq']} 配息月 {mon}{tag}")
        except Exception as exc:  # noqa: BLE001
            log(f"  [{code}] {name}:抓取失敗,保留既有({exc})")
        time.sleep(REQUEST_GAP_SEC)

    if ok == 0:
        raise RuntimeError("所有 ETF 基本資料皆抓取失敗(檢查 PROXY_URL / 來源)")

    log(f"完成:成功 {ok}/{len(etfs)} 檔,共 {len(profiles)} 筆在庫。")
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d (MoneyDJ via proxy)"),
        "profiles": profiles,
    }


def load_profiles(path: Path = PROFILES_PATH) -> dict:
    return load_json(path)


def update_profiles() -> int:
    try:
        data = crawl()
    except Exception as exc:  # noqa: BLE001
        print(f"ETF 圖鑑建庫失敗:{exc}", file=sys.stderr)
        return 1
    PROFILES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已寫入 {PROFILES_PATH}(共 {len(data['profiles'])} 筆)")
    return 0


if __name__ == "__main__":
    sys.exit(update_profiles())
