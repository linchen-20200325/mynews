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
import math
import statistics
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import config  # 環境變數讀取 SSOT

import news_fetcher
import paths  # 路徑 SSOT

HOUSE_PRICES_PATH = paths.HOUSE_PRICES
HOUSE_PRICE_HISTORY_PATH = paths.HOUSE_PRICE_HISTORY

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

# 交通可達性:有高鐵站、或台鐵自強號會停靠的縣市(供「交通便利縣市」額外比較)。
# 高鐵設站縣市(11 站所在地)。
HSR_COUNTIES = {
    "臺北市", "新北市", "桃園市", "新竹縣", "苗栗縣", "臺中市",
    "彰化縣", "雲林縣", "嘉義縣", "臺南市", "高雄市",
}
# 台鐵自強號(含普悠瑪/太魯閣)主要停靠的縣市;南投與離島無台鐵。
TRA_TZECHIANG_COUNTIES = {
    "基隆市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣", "苗栗縣",
    "臺中市", "彰化縣", "雲林縣", "嘉義市", "臺南市", "高雄市",
    "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣",
}


def transport_tag(county: str) -> str:
    """回傳縣市的軌道交通標籤:高鐵+自強號 / 高鐵 / 自強號 / 無軌道。"""
    h = county in HSR_COUNTIES
    t = county in TRA_TZECHIANG_COUNTIES
    if h and t:
        return "高鐵+自強號"
    if h:
        return "高鐵"
    if t:
        return "自強號"
    return "無軌道"


def has_rail_transport(county: str) -> bool:
    """是否有高鐵站或自強號停靠(交通便利縣市)。"""
    return county in HSR_COUNTIES or county in TRA_TZECHIANG_COUNTIES

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


def parse_price_csv(csv_bytes: bytes, county: str, log=None) -> list[dict]:
    """解析單一縣市的買賣 CSV,回傳住宅逐筆 [{district,type,ping_wan,total_wan,date,address}]。

    實價登錄 CSV 第 1 列為中文表頭、第 2 列為英文表頭;英文列因單價非數字會自動被略過。
    """
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    outliers = 0  # 住宅但每坪超出合理區間(打錯/車位污染)— 計數而非沉默丟棄
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
            outliers += 1
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
    if outliers and log:
        log(f"  [{county}] {outliers} 筆每坪超出 {MIN_PING_WAN}-{MAX_PING_WAN} 萬合理區間,剔除")
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
    avg = round(sum(pings) / len(pings), 2)
    med = _median(pings)
    # §4.2 不變量:已過 [MIN,MAX] 濾網,均值/中位數必落在區間內(否則邏輯有 bug)。
    # §4.3 第二法對帳:自製 avg/_median 與 stdlib 比對,偏離即 raise(Fail-Loud)。
    if not (MIN_PING_WAN <= avg <= MAX_PING_WAN) or not (MIN_PING_WAN <= med <= MAX_PING_WAN):
        raise AssertionError(f"房價統計越界:avg={avg}, median={med}(應在 "
                             f"{MIN_PING_WAN}-{MAX_PING_WAN} 萬)")
    if not math.isclose(avg, statistics.fmean(pings), abs_tol=0.01) \
            or not math.isclose(med, statistics.median(pings), abs_tol=0.01):
        raise AssertionError(f"房價統計對帳不符:avg={avg} vs fmean、median={med} vs stdlib")
    # 樣本取最近交易(交易年月日字串遞減即近似時間序)
    samples = sorted(rows, key=lambda r: r.get("date", ""), reverse=True)[:sample_n]
    return {
        "count": len(pings),
        "avg_ping_wan": avg,
        "median_ping_wan": med,
        "samples": samples,
    }


def _resolve_proxies(proxy: str | None):
    try:
        import proxy_helper
        return proxy_helper.get_proxy_config(proxy)
    except Exception:  # noqa: BLE001
        url = (proxy or config.env_str("PROXY_URL") or "").strip()
        return {"http": url, "https": url} if url else None


def _download_season_zip(season: str, proxies, log) -> "zipfile.ZipFile | None":
    """下載單一季別實價登錄 ZIP;失敗(未發布/被擋)回 None。"""
    try:
        content = _http_get_bytes(
            DOWNLOAD_URL,
            {"season": season, "type": "zip", "fileName": "lvr_landcsv.zip"},
            proxies,
        )
        return zipfile.ZipFile(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        log(f"    {season} 取得失敗:{exc}")
        return None


def _parse_zip_counties(zf: "zipfile.ZipFile", log) -> dict[str, dict[str, list]]:
    """把一個季別 ZIP 解析成 {縣市: {'resale': rows, 'presale': rows}}(逐筆住宅交易)。"""
    names = set(zf.namelist())
    out: dict[str, dict[str, list]] = {}
    for letter, county in CITY_CODES.items():
        entry: dict[str, list] = {}
        for kind, suffix in (("resale", "a"), ("presale", "b")):
            fname = f"{letter}_lvr_land_{suffix}.csv"
            if fname not in names:
                continue
            try:
                entry[kind] = parse_price_csv(zf.read(fname), county, log)
            except Exception as exc:  # noqa: BLE001 — 單檔解析失敗不影響其他
                log(f"    {county} {fname} 解析失敗:{exc}")
        if entry:
            out[county] = entry
    return out


def fetch_house_prices(proxy: str | None = None, log=print) -> dict:
    """下載最新可得季別實價登錄,彙整各縣市『成屋/預售屋』每坪均價 + 逐筆佐證。

    回傳 {"as_of","season","unit","counties":{縣市:{resale,presale}}}。
    逐季往前試到下載成功為止;單一縣市/檔案失敗不影響其他。
    """
    proxies = _resolve_proxies(proxy)
    last_exc: Exception | None = None
    for season in recent_seasons():
        log(f"  嘗試下載實價登錄季別 {season} …")
        zf = _download_season_zip(season, proxies, log)
        if zf is None:
            last_exc = RuntimeError(f"{season} 下載失敗")
            continue
        parsed = _parse_zip_counties(zf, log)
        counties = {
            county: {kind: _summarize(rows) for kind, rows in kinds.items()}
            for county, kinds in parsed.items()
        }
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


def _seasons_for_years(years_back: int, today: datetime | None = None) -> list[str]:
    """產生涵蓋近 years_back 個西元年的所有季別(由新到舊)。"""
    today = today or datetime.now(timezone.utc)
    roc_year = today.year - 1911
    quarter = (today.month - 1) // 3 + 1
    min_roc = roc_year - (years_back - 1)
    seasons: list[str] = []
    y, q = roc_year, quarter
    while y >= min_roc:
        seasons.append(f"{y}S{q}")
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return seasons


def _new_acc():
    """建立 acc[county][kind][year] = [總和, 筆數] 的巢狀累計結構。"""
    from collections import defaultdict
    return defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0, 0])))


def _load_acc(raw: dict | None):
    """把序列化的 _acc 還原成可累加的巢狀結構。"""
    acc = _new_acc()
    for county, kinds in (raw or {}).items():
        for kind, years in kinds.items():
            for y, pair in years.items():
                acc[county][kind][int(y)] = [float(pair[0]), int(pair[1])]
    return acc


def _accumulate(acc, parsed: dict, year: int) -> None:
    """把某季別解析結果累加進 acc 的對應西元年。"""
    for county, kinds in parsed.items():
        for kind, rows in kinds.items():
            bucket = acc[county][kind][year]
            for r in rows:
                bucket[0] += r["ping_wan"]
                bucket[1] += 1


def _prune_acc(acc, keep_years: int, today: datetime | None = None) -> None:
    """只保留近 keep_years 個西元年,避免歷年累計無限成長。"""
    today = today or datetime.now(timezone.utc)
    min_year = today.year - (keep_years - 1)
    for kinds in acc.values():
        for year_map in kinds.values():
            for y in [yr for yr in year_map if yr < min_year]:
                del year_map[y]


def _acc_to_public(acc):
    """由 acc 算出公開用 counties({年:均價} 純數字)與涵蓋年份集合。"""
    counties: dict[str, dict] = {}
    years: set[int] = set()
    for county, kinds in acc.items():
        entry: dict[str, dict] = {}
        for kind, year_map in kinds.items():
            yearly = {
                str(yr): round(s / n, 2)
                for yr, (s, n) in sorted(year_map.items()) if n
            }
            if yearly:
                entry[kind] = yearly
                years |= {int(y) for y in yearly}
        if entry:
            counties[county] = entry
    return counties, years


def _acc_to_serializable(acc) -> dict:
    """把 acc 轉成可寫入 JSON 的內部累計欄位(_acc)。"""
    return {
        county: {
            kind: {str(yr): [round(s, 4), n] for yr, (s, n) in year_map.items()}
            for kind, year_map in kinds.items()
        }
        for county, kinds in acc.items()
    }


def _build_history(acc, seasons_included: list[str]) -> dict:
    """由 acc + 已納入季別組出 house_price_history.json 結構。"""
    counties, years = _acc_to_public(acc)
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d (實價登錄 via proxy)"),
        "unit": "萬元/坪",
        "years": [str(y) for y in sorted(years)],
        "seasons_included": sorted(seasons_included),
        "counties": counties,
        "_acc": _acc_to_serializable(acc),
    }


def fetch_house_price_history(
    proxy: str | None = None, log=print, years_back: int = 5
) -> dict:
    """整段重抓:逐季抓近 years_back 年實價登錄,彙整各縣市『各西元年』每坪均價。

    回傳含公開 counties({年:均價})與內部 _acc(供日後增量累加)/ seasons_included。
    記憶體只保留各(縣市,類別,年)的累計總和/筆數,逐季處理完即釋放 ZIP。
    """
    proxies = _resolve_proxies(proxy)
    acc = _new_acc()
    included: list[str] = []

    for season in _seasons_for_years(years_back):
        year = int(season[:3]) + 1911
        log(f"  抓取季別 {season}(西元 {year})…")
        zf = _download_season_zip(season, proxies, log)
        if zf is None:
            continue
        parsed = _parse_zip_counties(zf, log)
        if parsed:
            _accumulate(acc, parsed, year)
            included.append(season)

    if not included:
        raise RuntimeError("歷年房價抓取失敗(檢查 PROXY_URL / 來源)。")
    return _build_history(acc, included)


def merge_house_price_history(
    existing: dict, proxy: str | None = None, log=print,
    window_years: int = 2, keep_years: int = 6,
) -> dict:
    """增量更新:只補近 window_years 內、尚未納入的季別,累加進既有 _acc 後重算。

    既有資料缺 _acc(舊格式)時,退回整段重抓 keep_years 年。
    """
    if not existing or not existing.get("_acc"):
        return fetch_house_price_history(proxy=proxy, log=log, years_back=keep_years)

    proxies = _resolve_proxies(proxy)
    acc = _load_acc(existing.get("_acc"))
    included = set(existing.get("seasons_included") or [])

    added = 0
    for season in _seasons_for_years(window_years):
        if season in included:
            continue
        year = int(season[:3]) + 1911
        log(f"  增量抓取季別 {season}(西元 {year})…")
        zf = _download_season_zip(season, proxies, log)
        if zf is None:
            continue
        parsed = _parse_zip_counties(zf, log)
        if parsed:
            _accumulate(acc, parsed, year)
            included.add(season)
            added += 1

    if added == 0:
        log("  無新季別可補,維持既有歷年房價。")
    _prune_acc(acc, keep_years)
    return _build_history(acc, list(included))


def load_house_price_history(path: Path = HOUSE_PRICE_HISTORY_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def update_house_prices() -> int:
    """CLI / 每月排程入口:抓最新一季房價寫入 house_prices.json。"""
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


def update_house_price_history(years_back: int = 5, incremental: bool = True) -> int:
    """CLI / 每月排程入口:更新歷年房價寫入 house_price_history.json。

    incremental=True(預設):只補最新尚未納入的季別,累加進既有資料(省時省流量);
    既有檔缺內部累計時自動退回整段重抓。incremental=False:整段重抓 years_back 年。
    """
    try:
        if incremental:
            data = merge_house_price_history(
                load_house_price_history(), keep_years=years_back)
        else:
            data = fetch_house_price_history(years_back=years_back)
    except Exception as exc:  # noqa: BLE001
        print(f"歷年房價更新失敗:{exc}", file=sys.stderr)
        return 1
    HOUSE_PRICE_HISTORY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已寫入 {HOUSE_PRICE_HISTORY_PATH}(年份 {data['years']},{len(data['counties'])} 縣市)")
    return 0


if __name__ == "__main__":
    # 用法:python housing_fetcher.py                 → 抓最新一季房價
    #       python housing_fetcher.py history          → 歷年增量更新(只補新季)
    #       python housing_fetcher.py history full [年數] → 整段重抓(預設 5 年)
    if len(sys.argv) > 1 and sys.argv[1] == "history":
        if len(sys.argv) > 2 and sys.argv[2] == "full":
            n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
            sys.exit(update_house_price_history(n, incremental=False))
        sys.exit(update_house_price_history(incremental=True))
    sys.exit(update_house_prices())
