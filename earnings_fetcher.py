"""earnings_fetcher.py — 抓台股「月營收」最新公告(真實財報更新訊號)。

來源:臺灣證券交易所 OpenAPI ``t187ap05_L``(上市公司每月營業收入彙總表),官方 JSON。
為何選月營收:台股最高頻、散戶最關注的財報數字 —— 上市櫃公司每月 10 日前須公告上月營收,
比季報(EPS)頻繁,最適合當「財報有更新就通知」的觸發。季報 EPS 屬後續擴充(見 STATE.md)。

【真實性】數字一律取自證交所 OpenAPI 原始 JSON,YoY/MoM 由官方欄位直接帶出,不經 AI 估算;
AI 只做白話解讀。被擋/抓不到 → 回空 dict(§5:不拖垮主流程,當天就略過財報、只推消息面)。

對外 API:
    fetch_monthly_revenue(tickers, log=print) -> dict[str, dict]
      回傳 {ticker: {"ticker","name","period"(YYYY-MM),"period_roc"(原始資料年月),
                     "month_rev"(元),"yoy_pct"(去年同月增減%),"mom_pct"(上月增減%),
                     "as_of"}};僅含成功抓到的代號(清單外/當期未抓到者不在內)。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

# 上市公司每月營業收入彙總表(全市場一次抓回,再依 watchlist 過濾)。
# 註:上櫃(TPEx)另有來源,屬後續擴充;目前先涵蓋上市,抓不到的代號當天靜默略過。
TWSE_MONTHLY_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
HTTP_TIMEOUT = 25

# OpenAPI 欄位名可能微調,逐一容錯比對(包含子字串即可)。
_F_CODE = ("公司代號", "公司 代號")
_F_NAME = ("公司名稱",)
_F_PERIOD = ("資料年月",)
_F_REV = ("當月營收",)
_F_YOY = ("去年同月增減", "去年同月")
_F_MOM = ("上月比較增減", "上月增減")


def _get(row: dict, names: tuple[str, ...]) -> str:
    """從一列(dict)取第一個鍵名含 names 任一子字串的值;取不到回空字串。"""
    for key, val in row.items():
        if any(n in str(key) for n in names):
            return str(val).strip()
    return ""


def _to_float(s: str):
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _to_int(s: str) -> int:
    try:
        return int(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def _roc_period_to_iso(roc: str) -> str:
    """民國資料年月(如 '11505')→ 西元 'YYYY-MM';解析失敗回原字串。"""
    digits = "".join(ch for ch in str(roc) if ch.isdigit())
    if len(digits) >= 5:
        try:
            year = int(digits[:-2]) + 1911
            month = int(digits[-2:])
            if 1 <= month <= 12:
                return f"{year:04d}-{month:02d}"
        except ValueError:
            pass
    return str(roc)


def _fetch_rows() -> list[dict]:
    """抓 OpenAPI 整包(走 NAS 代理 + 自動降級直連);失敗回空清單。"""
    try:
        import proxy_helper
        resp = proxy_helper.fetch_url(
            TWSE_MONTHLY_REVENUE_URL,
            headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT,
        )
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
    except Exception:  # noqa: BLE001 — 代理/解析失敗 → 試直連
        pass
    try:
        import requests
        resp = requests.get(
            TWSE_MONTHLY_REVENUE_URL,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200 and isinstance(resp.json(), list):
            return resp.json()
    except Exception:  # noqa: BLE001 — 直連也失敗 → 回空,當天略過財報
        pass
    return []


def fetch_monthly_revenue(tickers: list[str], log=print) -> dict[str, dict]:
    """抓 watchlist 內各代號的最新月營收;回 {ticker: {...}}。抓不到的代號不在回傳內。"""
    wanted = {str(t).strip() for t in tickers if str(t).strip()}
    if not wanted:
        return {}
    rows = _fetch_rows()
    if not rows:
        log("  月營收:OpenAPI 無回應(被擋或休市),本次略過財報、只推消息面。")
        return {}

    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC (TWSE OpenAPI t187ap05_L)")
    out: dict[str, dict] = {}
    for row in rows:
        code = _get(row, _F_CODE)
        if code not in wanted:
            continue
        rev_k = _to_int(_get(row, _F_REV))  # 當月營收(仟元)
        if rev_k <= 0:
            continue  # 無有效營收 → 不以 0 充數,略過此檔
        out[code] = {
            "ticker": code,
            "name": _get(row, _F_NAME),
            "period": _roc_period_to_iso(_get(row, _F_PERIOD)),
            "period_roc": _get(row, _F_PERIOD),
            "month_rev": rev_k * 1000,  # 仟元 → 元
            "yoy_pct": _to_float(_get(row, _F_YOY)),
            "mom_pct": _to_float(_get(row, _F_MOM)),
            "as_of": as_of,
        }
    if out:
        log(f"  月營收:抓到 {len(out)} 檔最新公告。")
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="抓指定台股代號的最新月營收(預設 2330)")
    ap.add_argument("tickers", nargs="*", default=["2330"])
    args = ap.parse_args()
    data = fetch_monthly_revenue(args.tickers or ["2330"])
    if not data:
        print("月營收抓取失敗或清單外", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(0)
