"""earnings_fetcher.py — 抓台股「月營收」最新公告(真實財報更新訊號)。

來源:
  上市公司:臺灣證券交易所 OpenAPI ``t187ap05_L``(官方 JSON,一次全抓)。
  上櫃公司:公開資訊觀測站(MOPS)``ajax_t05st10_q`` POST(HTML 表,需 proxy)。
為何選月營收:台股最高頻、散戶最關注的財報數字 —— 上市櫃公司每月 10 日前須公告上月營收,
比季報(EPS)頻繁,最適合當「財報有更新就通知」的觸發。

【真實性】數字一律取自官方 API/MOPS 原始資料,YoY/MoM 由官方欄位直接帶出,不經 AI 估算;
AI 只做白話解讀。被擋/抓不到 → 回空 dict(§5:不拖垮主流程,當天就略過財報、只推消息面)。

對外 API:
    fetch_monthly_revenue(tickers, log=print) -> dict[str, dict]
      上市先 TWSE 一次全抓,找不到的代號自動 fallback 至 MOPS 上櫃查詢,兩者透明合併。
      回傳 {ticker: {"ticker","name","period"(YYYY-MM),"period_roc"(原始資料年月),
                     "month_rev"(元),"yoy_pct"(去年同月增減%),"mom_pct"(上月增減%),
                     "as_of"[,"market":"otc"]}};僅含成功抓到的代號。
    fetch_quarterly_eps(tickers, log=print) -> dict[str, dict]
      後續擴充(MOPS 無正式 API 需表單解析),目前回空 dict 並靜默略過。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

# 上市公司每月營業收入彙總表(全市場一次抓回,再依 watchlist 過濾)。
TWSE_MONTHLY_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
# 上櫃公司月營收(MOPS 表單 POST,需 proxy 過境)。
MOPS_OTC_URL = "https://mops.twse.com.tw/mops/web/ajax_t05st10_q"
# 上市/上櫃 IFRS 綜合損益表(含「基本每股盈餘」行)。
MOPS_EPS_URL = "https://mops.twse.com.tw/mops/web/ajax_t163sb04"
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


def _fetch_listed_rows() -> list[dict]:
    """抓上市 OpenAPI 整包(proxy→直連兩段降級);失敗回空清單。"""
    import proxy_helper
    data = proxy_helper.fetch_json(TWSE_MONTHLY_REVENUE_URL, timeout=HTTP_TIMEOUT)
    return data if isinstance(data, list) else []


def _latest_roc_period() -> tuple[int, int]:
    """回傳最可能已公告的月份(10日後→上月;10日前→上上月)。回 (ROC年, 月)。"""
    from tz_utils import taiwan_now
    today = taiwan_now()
    months_back = 1 if today.day >= 10 else 2
    month = today.month - months_back
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    return year - 1911, month


def _fetch_otc_bulk(wanted: set[str], log=print) -> dict[str, dict]:
    """向 MOPS POST 一次抓取所有上櫃公司月營收,過濾出 wanted 代號。

    需 NAS proxy(MOPS 境外 IP 可能被限速);失敗回空 dict 不拖垮上市資料。
    """
    import io
    import pandas as pd
    import proxy_helper

    roc_year, month = _latest_roc_period()
    iso_period = f"{roc_year + 1911:04d}-{month:02d}"
    period_roc = f"{roc_year}{month:02d}"
    as_of = datetime.now(timezone.utc).strftime(
        f"%Y-%m-%d %H:%M UTC (MOPS OTC {roc_year}/{month:02d})"
    )
    form_data = {
        "step": "1", "firstin": "1", "off": "1",
        "keyword4": "", "code1": "", "TYPEK2": "", "checkbtn": "",
        "queryName": "co_id", "inpuType": "co_id",
        "TYPEK": "otc", "isnew": "false",
        "co_id": "",  # 空 = 全部上櫃
        "year": str(roc_year),
        "month": f"{month:02d}",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://mops.twse.com.tw/mops/web/t05st10_q",
    }
    proxies = proxy_helper.get_proxy_config() or {}
    verify = not bool(proxies)

    try:
        sess = proxy_helper.make_retry_session()
        resp = sess.post(
            MOPS_OTC_URL, data=form_data, headers=headers,
            proxies=proxies, verify=verify, timeout=HTTP_TIMEOUT,
        )
        resp.encoding = "utf-8"
        if resp.status_code != 200 or not resp.text:
            log(f"  月營收(上櫃) MOPS 無回應(status={resp.status_code}),略過。")
            return {}
        html = resp.text
    except Exception as exc:
        log(f"  月營收(上櫃) MOPS 連線失敗:{exc}")
        return {}

    try:
        tables = pd.read_html(io.StringIO(html), thousands=",")
    except Exception as exc:
        log(f"  月營收(上櫃) HTML 解析失敗:{exc}")
        return {}

    df = next((t for t in tables if any("當月營收" in str(c) for c in t.columns)), None)
    if df is None or df.empty:
        log(f"  月營收(上櫃) 找不到營收表(期別 {iso_period}),略過。")
        return {}

    def _col(df, *keywords):
        for c in df.columns:
            if all(k in str(c) for k in keywords):
                return c
        return None

    code_col = _col(df, "代號")
    name_col = _col(df, "名稱")
    rev_col  = _col(df, "當月營收")
    yoy_col  = _col(df, "去年同月")
    mom_col  = _col(df, "上月比較") or _col(df, "上月增減")

    if not code_col or not rev_col:
        log(f"  月營收(上櫃) 欄位識別失敗({list(df.columns)[:6]}),略過。")
        return {}

    df = df[df[code_col].astype(str).str.strip().isin(wanted)].copy()
    out: dict[str, dict] = {}
    for row in df.to_dict("records"):
        code = str(row[code_col]).strip()
        rev_k = _to_int(str(row[rev_col]))
        if rev_k <= 0:
            continue
        out[code] = {
            "ticker": code,
            "name": str(row[name_col]).strip() if name_col else code,
            "period": iso_period,
            "period_roc": period_roc,
            "month_rev": rev_k * 1000,
            "yoy_pct": _to_float(str(row[yoy_col])) if yoy_col else None,
            "mom_pct": _to_float(str(row[mom_col])) if mom_col else None,
            "as_of": as_of,
            "market": "otc",
        }
    if out:
        log(f"  月營收(上櫃):抓到 {len(out)} 檔。")
    return out


def fetch_monthly_revenue(tickers: list[str], log=print) -> dict[str, dict]:
    """抓 watchlist 各代號最新月營收;上市走 TWSE OpenAPI,上櫃 fallback 至 MOPS。

    兩者透明合併回傳 {ticker: {...}},抓不到的代號不在回傳內。
    """
    wanted = {str(t).strip() for t in tickers if str(t).strip()}
    if not wanted:
        return {}

    # ── 1. 上市:TWSE OpenAPI 一次全抓 ──
    rows = _fetch_listed_rows()
    if not rows:
        log("  月營收(上市):OpenAPI 無回應(被擋或休市),略過上市部分。")

    as_of_listed = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC (TWSE OpenAPI t187ap05_L)")
    out: dict[str, dict] = {}
    for row in rows:
        code = _get(row, _F_CODE)
        if code not in wanted:
            continue
        rev_k = _to_int(_get(row, _F_REV))  # 仟元
        if rev_k <= 0:
            continue
        out[code] = {
            "ticker": code,
            "name": _get(row, _F_NAME),
            "period": _roc_period_to_iso(_get(row, _F_PERIOD)),
            "period_roc": _get(row, _F_PERIOD),
            "month_rev": rev_k * 1000,  # 仟元 → 元
            "yoy_pct": _to_float(_get(row, _F_YOY)),
            "mom_pct": _to_float(_get(row, _F_MOM)),
            "as_of": as_of_listed,
        }
    if out:
        log(f"  月營收(上市):抓到 {len(out)} 檔。")

    # ── 2. 上櫃:TWSE 找不到的代號 → MOPS bulk POST ──
    missing = wanted - out.keys()
    if missing:
        otc = _fetch_otc_bulk(missing, log)
        out.update(otc)

    if not out:
        log("  月營收:全部代號均未抓到,本次略過財報、只推消息面。")
    return out


def _latest_quarter() -> tuple[int, int]:
    """最可能已公告的季別(ROC年, 季1-4)。
    截止日:Q1→5/15、Q2→8/14、Q3→11/14、Q4→隔年3/31。
    """
    from tz_utils import taiwan_now
    today = taiwan_now()
    y, m, d = today.year, today.month, today.day
    if m > 11 or (m == 11 and d >= 14):
        return y - 1911, 3
    if m > 8 or (m == 8 and d >= 14):
        return y - 1911, 2
    if m > 5 or (m == 5 and d >= 15):
        return y - 1911, 1
    return y - 1912, 4  # 前一年 Q4


def _parse_eps_from_html(html: str) -> "tuple[float | None, float | None]":
    """從 MOPS 損益表 HTML 萃取 (本期EPS, 前期EPS)。"""
    import io
    import pandas as pd

    try:
        tables = pd.read_html(io.StringIO(html), thousands=",")
    except Exception:
        return None, None

    for df in tables:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [
                "|".join(str(c) for c in col if "Unnamed" not in str(c)).strip("|")
                for col in df.columns
            ]
        for col_idx, col in enumerate(df.columns):
            mask = df[col].astype(str).str.contains("基本每股盈餘", na=False)
            if not mask.any():
                continue
            row = df[mask].iloc[0]
            nums = [
                _to_float(str(row[c]))
                for c in list(df.columns)[col_idx + 1:]
                if _to_float(str(row[c])) is not None
            ]
            nums = [v for v in nums if abs(v) < 500]  # EPS 合理範圍(-500~500 元)
            if nums:
                return nums[0], (nums[1] if len(nums) > 1 else None)
    return None, None


def _fetch_eps_for(ticker: str, roc_year: int, season: int,
                   sess, proxies: dict, verify: bool) -> "dict | None":
    """向 MOPS 抓單一代號 EPS;先試上市(sii)再試上櫃(otc)。失敗回 None。"""
    import time

    as_of = datetime.now(timezone.utc).strftime(
        f"%Y-%m-%d %H:%M UTC (MOPS {roc_year}/Q{season})"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://mops.twse.com.tw/mops/web/t163sb04",
    }
    period_str = f"{roc_year + 1911}-Q{season}"

    for typek in ("sii", "otc"):
        form_data = {
            "step": "1", "firstin": "1",
            "TYPEK": typek,
            "year": str(roc_year),
            "season": str(season),
            "co_id": ticker,
        }
        try:
            resp = sess.post(
                MOPS_EPS_URL, data=form_data, headers=headers,
                proxies=proxies, verify=verify, timeout=HTTP_TIMEOUT,
            )
            resp.encoding = "utf-8"
            if resp.status_code == 200 and resp.text:
                eps, prior_eps = _parse_eps_from_html(resp.text)
                if eps is not None:
                    return {
                        "ticker": ticker,
                        "period": period_str,
                        "season": season,
                        "roc_year": roc_year,
                        "eps": eps,
                        "prior_eps": prior_eps,
                        "market": typek,
                        "as_of": as_of,
                    }
        except Exception:
            pass
        time.sleep(0.5)  # MOPS 限速緩衝

    return None


def fetch_quarterly_eps(tickers: list[str], log=print) -> dict[str, dict]:
    """抓 watchlist 各代號最新季報 EPS(上市 sii / 上櫃 otc 自動辨識)。

    每檔向 MOPS ajax_t163sb04 POST(需 proxy);找不到者靜默略過。
    公告截止:Q1→5/15、Q2→8/14、Q3→11/14、Q4→隔年3/31。
    """
    wanted = [str(t).strip() for t in tickers if str(t).strip()]
    if not wanted:
        return {}

    import proxy_helper
    roc_year, season = _latest_quarter()
    proxies = proxy_helper.get_proxy_config() or {}
    verify = not bool(proxies)
    sess = proxy_helper.make_retry_session()

    out: dict[str, dict] = {}
    for ticker in wanted:
        result = _fetch_eps_for(ticker, roc_year, season, sess, proxies, verify)
        if result:
            out[ticker] = result

    if out:
        log(f"  季報 EPS:抓到 {len(out)} 檔({roc_year + 1911}-Q{season})。")
    else:
        log(f"  季報 EPS:全部代號均未抓到({roc_year + 1911}-Q{season}),略過。")
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
