"""housing_fetcher.py — 房市真實資料抓取(新聞 + 實價登錄房價),不呼叫 AI。

兩類真實資料:
  1. 房市新聞:沿用 news_fetcher 的 RSS 引擎,改用房市關鍵字(預售屋/成屋/房價/
     打房/央行限貸/平均地權條例…),供 Gemini 判讀冷熱與政策。
  2. 各縣市每坪房價:內政部「不動產成交案件實際資訊資料供應系統」實價登錄
     季度批次 ZIP(官方開放資料),解析出各縣市『成屋』與『預售屋』每坪均價,
     並保留逐筆樣本當佐證。

     下載:https://plvr.land.moi.gov.tw/DownloadSeason?season=<季>&type=zip&fileName=lvr_landcsv.zip
     ZIP 內每縣市一組 CSV:{字母}_lvr_land_a.csv(不動產買賣/成屋)、
     {字母}_lvr_land_b.csv(預售屋買賣)、_c.csv(租賃,本模組不用)。
     單價欄為「單價元平方公尺」;每坪 = 單價元平方公尺 × 3.305785(平方公尺/坪)。

實價登錄站會封鎖境外 IP,連線統一走 proxy_helper(PROXY_URL)中繼;沙箱無網路屬正常。
所有房價為政府實價登錄事實資料,嚴禁以 AI 猜測。
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import news_fetcher

HOUSE_PRICES_PATH = Path("house_prices.json")

# 1 坪 = 3.305785 平方公尺;每坪單價(元) = 單價元平方公尺 × PING_PER_SQM。
PING_PER_SQM = 3.305785

DOWNLOAD_URL = "https://plvr.land.moi.gov.tw/DownloadSeason"
HTTP_TIMEOUT = 60
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 實價登錄縣市代碼(內政部地政車牌碼);僅列現行 22 直轄市/縣市,名稱用官方「臺」。
CITY_CODES: dict[str, str] = {
    "a": "臺北市", "b": "臺中市", "c": "基隆市", "d": "臺南市", "e": "高雄市",
    "f": "新北市", "g": "宜蘭縣", "h": "桃園市", "i": "嘉義市", "j": "新竹縣",
    "k": "苗栗縣", "m": "南投縣", "n": "彰化縣", "o": "新竹市", "p": "雲林縣",
    "q": "嘉義縣", "t": "屏東縣", "u": "花蓮縣", "v": "臺東縣", "w": "金門縣",
    "x": "澎湖縣", "z": "連江縣",
}

# 視為「住宅」的建物型態(排除店面/辦公/廠辦/車位等非自住標的)。
RESIDENTIAL_TYPES = ("住宅大樓", "公寓", "透天厝", "華廈", "套房")

# 每坪(萬元)合理區間,過濾離群值(打錯、含大量車位、特殊交易)。
MIN_PING_WAN = 1.0
MAX_PING_WAN = 400.0

# 房市新聞關鍵字(繁中、聚焦預售/成屋冷熱與打房政策)。
DEFAULT_HOUSING_QUERIES = [
    "房市 預售屋 成交",
    "中古屋 成屋 房價",
    "打房 政策 房市",
    "央行 房貸 信用管制",
    "平均地權條例 囤房稅 房市",
]


# ---------------------------------------------------------------------------
# 房市新聞
# ---------------------------------------------------------------------------

def fetch_housing_news(limit: int = 18, since_hours: int = 72) -> list[dict]:
    """抓房市相關真實新聞(預售/成屋冷熱、打房政策)。"""
    feeds = {"中央社 財經": news_fetcher.CREDIBLE_FEEDS.get("中央社 財經", "")}
    feeds = {k: v for k, v in feeds.items() if v}
    return news_fetcher.fetch_news(
        DEFAULT_HOUSING_QUERIES,
        lang="zh", region="TW", feeds=feeds,
        limit=limit, since_hours=since_hours,
    )


# ---------------------------------------------------------------------------
# 實價登錄房價
# ---------------------------------------------------------------------------

def recent_seasons(today: datetime | None = None, n: int = 6) -> list[str]:
    """產生近 n 個實價登錄季別代碼(由新到舊),例:['115S1','114S4',...]。

    格式為『民國年 + S + 季』。實價登錄發布有時間落差,逐季往前試到抓得到為止。
    """
    today = today or datetime.now(timezone.utc)
    roc_year = today.year - 1911
    quarter = (today.month - 1) // 3 + 1
    seasons: list[str] = []
    y, q = roc_year, quarter
    for _ in range(n):
        seasons.append(f"{y}S{q}")
        q -= 1
        if q == 0:
            q = 4
            y -= 1
    return seasons


def _http_get_bytes(url: str, params: dict, proxies: dict | None) -> bytes:
    import requests

    verify = not bool(proxies)
    if not verify:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # noqa: BLE001
            pass
    resp = requests.get(
        url, params=params, headers={"User-Agent": USER_AGENT},
        proxies=proxies, timeout=HTTP_TIMEOUT, verify=verify,
    )
    resp.raise_for_status()
    return resp.content


def _to_float(value) -> float | None:
    try:
        f = float(str(value).replace(",", "").strip())
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _is_residential(deal_target: str, building_type: str) -> bool:
    """只計『房地(土地+建物)』且建物型態屬住宅者;排除純車位/土地/店辦廠。"""
    if not deal_target.startswith("房地"):
        return False
    if "車位" in deal_target:  # 含車位會稀釋每坪單價,排除
        return False
    return any(t in building_type for t in RESIDENTIAL_TYPES)


def parse_price_csv(csv_bytes: bytes, county: str) -> list[dict]:
    """解析單一縣市的買賣 CSV,回傳住宅逐筆 [{district,type,ping_wan,total_wan,date,address}]。

    實價登錄 CSV 第 1 列為中文表頭、第 2 列為英文表頭;英文列因單價非數字會自動被略過。
    """
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for r in reader:
        unit_sqm = _to_float(r.get("單價元平方公尺"))
        if unit_sqm is None:
            continue  # 英文表頭列 / 無單價(純土地、車位)→ 略過
        deal = (r.get("交易標的") or "").strip()
        btype = (r.get("建物型態") or "").strip()
        if not _is_residential(deal, btype):
            continue
        ping_wan = unit_sqm * PING_PER_SQM / 10000.0  # 萬元/坪
        if not (MIN_PING_WAN <= ping_wan <= MAX_PING_WAN):
            continue
        total = _to_float(r.get("總價元"))
        rows.append({
            "district": (r.get("鄉鎮市區") or "").strip(),
            "type": btype,
            "ping_wan": round(ping_wan, 2),
            "total_wan": round(total / 10000.0, 1) if total else None,
            "date": (r.get("交易年月日") or "").strip(),
            "address": (r.get("土地位置建物門牌")
                        or r.get("土地區段位置建物區段門牌") or "").strip(),
        })
    return rows


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else round((s[mid - 1] + s[mid]) / 2, 2)


def _summarize(rows: list[dict], sample_n: int = 8) -> dict:
    """把逐筆住宅交易彙整成單一縣市/類別統計 + 逐筆佐證樣本。"""
    pings = [r["ping_wan"] for r in rows]
    if not pings:
        return {"count": 0, "avg_ping_wan": None, "median_ping_wan": None, "samples": []}
    # 樣本取最近交易(交易年月日字串遞減即近似時間序)
    samples = sorted(rows, key=lambda r: r.get("date", ""), reverse=True)[:sample_n]
    return {
        "count": len(pings),
        "avg_ping_wan": round(sum(pings) / len(pings), 2),
        "median_ping_wan": _median(pings),
        "samples": samples,
    }


def fetch_house_prices(proxy: str | None = None, log=print) -> dict:
    """下載最新可得季別實價登錄,彙整各縣市『成屋/預售屋』每坪均價 + 逐筆佐證。

    回傳 {"as_of","season","unit","counties":{縣市:{resale,presale}}}。
    逐季往前試到下載成功為止;單一縣市/檔案失敗不影響其他。
    """
    try:
        import proxy_helper
        proxies = proxy_helper.get_proxy_config(proxy)
    except Exception:  # noqa: BLE001
        import os
        url = (proxy or os.environ.get("PROXY_URL") or "").strip()
        proxies = {"http": url, "https": url} if url else None

    last_exc: Exception | None = None
    for season in recent_seasons():
        try:
            log(f"  嘗試下載實價登錄季別 {season} …")
            content = _http_get_bytes(
                DOWNLOAD_URL,
                {"season": season, "type": "zip", "fileName": "lvr_landcsv.zip"},
                proxies,
            )
            zf = zipfile.ZipFile(io.BytesIO(content))
        except Exception as exc:  # noqa: BLE001 — 該季尚未發布/被擋 → 試前一季
            last_exc = exc
            log(f"    {season} 取得失敗:{exc}")
            continue

        names = set(zf.namelist())
        counties: dict[str, dict] = {}
        for letter, county in CITY_CODES.items():
            entry = {}
            for kind, suffix in (("resale", "a"), ("presale", "b")):
                fname = f"{letter}_lvr_land_{suffix}.csv"
                if fname not in names:
                    continue
                try:
                    rows = parse_price_csv(zf.read(fname), county)
                    entry[kind] = _summarize(rows)
                except Exception as exc:  # noqa: BLE001 — 單檔解析失敗不影響其他
                    log(f"    {county} {fname} 解析失敗:{exc}")
            if entry:
                counties[county] = entry
        if counties:
            n_resale = sum(1 for c in counties.values() if c.get("resale", {}).get("count"))
            log(f"  季別 {season} 完成:{len(counties)} 縣市(成屋有量 {n_resale})")
            return {
                "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d (實價登錄 via proxy)"),
                "season": season,
                "unit": "萬元/坪",
                "counties": counties,
            }
        last_exc = RuntimeError("ZIP 內未解析到任何縣市住宅資料")

    raise RuntimeError(f"實價登錄房價抓取失敗(檢查 PROXY_URL / 來源)。最後錯誤:{last_exc}")


def load_house_prices(path: Path = HOUSE_PRICES_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def update_house_prices() -> int:
    """CLI / 每月排程入口:抓房價寫入 house_prices.json。"""
    try:
        data = fetch_house_prices()
    except Exception as exc:  # noqa: BLE001
        print(f"房價更新失敗:{exc}", file=sys.stderr)
        return 1
    HOUSE_PRICES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已寫入 {HOUSE_PRICES_PATH}(季別 {data['season']},{len(data['counties'])} 縣市)")
    return 0


if __name__ == "__main__":
    sys.exit(update_house_prices())
