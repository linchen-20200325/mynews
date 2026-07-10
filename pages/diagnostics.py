"""pages/diagnostics.py — 🩺 資料診斷:全站資料健康的單一觀測面板(SSOT)。

集中三塊原本散落各頁的可觀測性:
  1. 資料檔新鮮度總覽:paths.LATEST_* 每檔的存在/歸屬日/檔案更新時間/過期判定
     (門檻沿用 freshness + 各領域具名常數,不另立標準)。
  2. 資料源連線健檢:NAS 中繼站(MoneyDJ)/ Yahoo / 證交所 / Google News RSS
     輕量 probe(平行執行,按鈕觸發才打網路),外加金鑰/Token 設定狀態。
  3. mock 資料清單:哪些指標仍是模擬值、是否已排除於訊號計算 — 讓 mock/真實
     界線透明,避免使用者誤信模擬值。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import freshness
import github_store
import index_fetcher
import news_fetcher
import paths
import proxy_helper
import tz_utils
import update_data  # CHIP/HOUSE 新鮮度門檻的具名常數(SSOT 定義於 update_data)
from app_core import (
    PRICE_STALE_DAYS,
    STALE_REPORT_DAYS,
    _secret,
    ensure_gemini_key,
    load_json,
)

# 法規月報約每月一更,新鮮度門檻放寬(其餘門檻沿用各領域 SSOT 常數)
_HOUSING_REG_STALE_DAYS = 45

# 證交所可達性探測(只驗連線,不抓業務資料)
_TWSE_PROBE_URL = "https://www.twse.com.tw/"


# ---------------------------------------------------------------------------
# 1. 資料檔新鮮度總覽
# ---------------------------------------------------------------------------

def _data_files() -> list[tuple[str, object, "int | None"]]:
    """(顯示名, 路徑, 過期門檻天數 None=不判定)。路徑一律取自 paths(SSOT)。"""
    return [
        ("戰略報告", paths.LATEST_REPORT, STALE_REPORT_DAYS),
        ("趨勢雷達", paths.LATEST_TRENDS, STALE_REPORT_DAYS),
        ("台股觀察", paths.LATEST_STOCKS, STALE_REPORT_DAYS),
        ("美股觀察", paths.LATEST_US_STOCKS, STALE_REPORT_DAYS),
        ("國際盤預警", paths.LATEST_INTL_ALERT, STALE_REPORT_DAYS),
        ("三大法人籌碼", paths.LATEST_CHIP, update_data.CHIP_STALE_DAYS),
        ("融資餘額", paths.LATEST_MARGIN, update_data.CHIP_STALE_DAYS),
        ("台指期留倉", paths.LATEST_FUT_CHIP, update_data.CHIP_STALE_DAYS),
        ("人物追蹤", paths.LATEST_FOCUS, STALE_REPORT_DAYS),
        ("房市觀察", paths.LATEST_HOUSING, STALE_REPORT_DAYS),
        ("房產法規月報", paths.LATEST_HOUSING_REG, _HOUSING_REG_STALE_DAYS),
        ("中線翻轉偵測", paths.LATEST_REVERSAL, STALE_REPORT_DAYS),
        ("中央決策大腦", paths.LATEST_DECISION, STALE_REPORT_DAYS),
        ("台股收盤價", paths.STOCK_PRICES, PRICE_STALE_DAYS),
        ("實價登錄房價", paths.HOUSE_PRICES, update_data.HOUSE_STALE_DAYS),
        ("ETF 成分股", paths.ETF_HOLDINGS, None),
        ("ETF 圖鑑", paths.ETF_PROFILES, None),
    ]


def _doc_date(doc) -> str:
    """從資料檔常見欄位取歸屬日字串(report_date/as_of/date…);取不到回空字串。"""
    if not isinstance(doc, dict):
        return ""
    for key in ("report_date", "as_of", "date", "updated_at", "fetched_at"):
        value = doc.get(key)
        if value:
            return str(value)
    return ""


def sec_file_freshness() -> None:
    st.subheader("📁 資料檔新鮮度總覽")
    st.caption(
        "每檔 JSON 的歸屬日與檔案更新時間一覽;「今天為什麼沒資料」先看這裡。"
        "過期門檻沿用各領域常數(報告/觀察 "
        f"{STALE_REPORT_DAYS} 天、籌碼 {update_data.CHIP_STALE_DAYS} 天、"
        f"股價 {PRICE_STALE_DAYS} 天、房價 {update_data.HOUSE_STALE_DAYS} 天)。"
    )
    rows = []
    for label, path, stale_days in _data_files():
        row = {"資料檔": label, "檔名": str(path), "狀態": "", "歸屬日": "",
               "檔案更新(台灣)": "", "大小": ""}
        try:
            stat = path.stat()
        except OSError:
            row["狀態"] = "❌ 不存在"
            rows.append(row)
            continue
        mtime_tw = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc) + tz_utils.TW_OFFSET
        row["檔案更新(台灣)"] = mtime_tw.strftime("%Y-%m-%d %H:%M")
        row["大小"] = f"{stat.st_size / 1024:,.0f} KB"

        doc = load_json(path)
        if doc is None:
            row["狀態"] = "⚠️ 無法解析(非 JSON 物件?)"
            rows.append(row)
            continue
        as_of = _doc_date(doc)
        row["歸屬日"] = as_of
        if stale_days is None or not as_of:
            row["狀態"] = "✅ 存在"
        elif freshness.stale_note(as_of, stale_days, label):
            row["狀態"] = f"⚠️ 過期(落後 {freshness.staleness_days(as_of)} 天)"
        else:
            row["狀態"] = "✅ 新鮮"
        rows.append(row)

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                 height=min(38 * (len(rows) + 1) + 4, 660))


# ---------------------------------------------------------------------------
# 2. 資料源連線健檢(輕量 probe;按鈕觸發才打網路,平行執行)
# ---------------------------------------------------------------------------

def _probe_proxy() -> tuple[bool, str, int]:
    res = proxy_helper.check_proxy(timeout=8)
    return bool(res.get("ok")), str(res.get("detail", "")), int(res.get("elapsed_ms", 0))


def _probe_yahoo() -> tuple[bool, str, int]:
    start = time.time()
    data = proxy_helper.fetch_json(
        index_fetcher.YF_CHART_URL.format(symbol="^GSPC"),
        timeout=8, retries=1, prefer_direct=True,
    )
    elapsed = int((time.time() - start) * 1000)
    ok = isinstance(data, dict) and bool(data.get("chart"))
    detail = ("✅ chart API 可達(直連優先,NAS 備援)" if ok
              else "❌ 無回應或結構非預期(限流/網路);國際盤報價與季節圖將受影響")
    return ok, detail, elapsed


def _probe_twse() -> tuple[bool, str, int]:
    start = time.time()
    resp = proxy_helper.fetch_url(_TWSE_PROBE_URL, timeout=8, retries=1)
    elapsed = int((time.time() - start) * 1000)
    ok = resp is not None
    detail = ("✅ 證交所可達" if ok
              else "❌ 證交所無回應(維護/連線受阻);法人籌碼、融資與個股技術面將受影響")
    return ok, detail, elapsed


def _probe_rss() -> tuple[bool, str, int]:
    start = time.time()
    items = news_fetcher.fetch_feed(
        news_fetcher.google_news_topic_url("BUSINESS", "zh", "TW"),
        "Google News", None,
    )
    elapsed = int((time.time() - start) * 1000)
    ok = len(items) > 0
    detail = (f"✅ Google News RSS 可達(財經頭條 {len(items)} 則)" if ok
              else "❌ Google News RSS 無資料;各頁「立即抓取新聞」將受影響")
    return ok, detail, elapsed


_NETWORK_PROBES: list[tuple[str, object]] = [
    ("NAS 中繼站(MoneyDJ)", _probe_proxy),
    ("Yahoo Finance(國際報價/季節圖)", _probe_yahoo),
    ("證交所 TWSE(籌碼/融資/技術面)", _probe_twse),
    ("Google News RSS(各頁新聞)", _probe_rss),
]


def _run_network_probes() -> list[dict]:
    """平行執行 4 個輕量 probe(執行緒內不碰 st.*),彙整後由主執行緒渲染。"""
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(_NETWORK_PROBES)) as pool:
        futures = [(name, pool.submit(fn)) for name, fn in _NETWORK_PROBES]
        for name, fut in futures:
            try:
                ok, detail, elapsed = fut.result()
            except Exception as exc:  # noqa: BLE001 — 單一 probe 失敗不影響其他
                ok, detail, elapsed = False, f"探測程序異常:{exc}", 0
            results.append({"name": name, "ok": ok, "detail": detail, "ms": elapsed})
    return results


def sec_source_probe() -> None:
    st.subheader("🧪 資料源連線健檢")

    # 設定狀態(零網路成本,直接顯示)
    cfg = proxy_helper.get_proxy_config()
    c1, c2, c3 = st.columns(3)
    c1.metric("NAS 中繼站設定", "✅ 已設定" if cfg else "⚠️ 未設定")
    c2.metric("Gemini 金鑰", "✅ 已設定" if ensure_gemini_key() else "⚠️ 未設定")
    c3.metric("GITHUB_TOKEN", "✅ 已設定" if github_store.is_configured(_secret) else "⚠️ 未設定")
    st.caption(
        "以上為設定存在與否(不打 API、不耗配額)。"
        "下方按鈕才對 4 個資料源實際發出輕量請求(平行,約 2~8 秒)。"
    )

    if st.button("🧪 一鍵健檢 4 個資料源", use_container_width=True, key="btn_diag_probe"):
        with st.spinner("平行探測 NAS / Yahoo / 證交所 / Google News 中…"):
            st.session_state["diag_probe_results"] = _run_network_probes()
            st.session_state["diag_probe_at"] = tz_utils.taiwan_now().strftime("%Y-%m-%d %H:%M:%S")

    results = st.session_state.get("diag_probe_results")
    if not results:
        return
    st.caption(f"上次健檢:{st.session_state.get('diag_probe_at', '—')}(台灣時間)")
    for r in results:
        render = st.success if r["ok"] else st.error
        render(f"**{r['name']}**　·　{r['detail']}　·　{r['ms']:,} ms")


# ---------------------------------------------------------------------------
# 3. mock 資料清單(模擬值透明化)
# ---------------------------------------------------------------------------

_MOCK_SOURCES = [
    {
        "指標": "中線翻轉偵測 — 大盤籌碼歷史序列",
        "位置": "reversal_signals._load_real_market_chip / _mock_market_chip",
        "說明": "僅最新一筆外資期貨口數為真實(latest_futures_chip.json);"
               "前 9 筆歷史與融資維持率為模擬值。已排除於共振計算,不影響買賣訊號。",
    },
    {
        "指標": "中線翻轉偵測 — 個股集保持股分級",
        "位置": "reversal_signals._mock_stock_chip",
        "說明": "集保 API 未接通,全為模擬值。已排除於共振計算,不影響買賣訊號。",
    },
    {
        "指標": "房市 — 就業人口 × 空屋率地圖",
        "位置": "taiwan_map_data._mock_df",
        "說明": "勞動部/內政部真實 CSV 未接入,全為模擬值(頁面已標示);"
               "接入後替換 _mock_df 即上線。",
    },
]


def sec_mock_inventory() -> None:
    st.subheader("🧪 mock(模擬值)清單")
    st.caption(
        "以下指標仍使用模擬資料 — 模擬值一律不得觸發訊號,只作介面演示;"
        "接通真實 API 後由對應模組自動納入。錯誤或假造的結論成本遠高於沒有結論。"
    )
    for m in _MOCK_SOURCES:
        with st.container(border=True):
            st.markdown(f"**{m['指標']}**　·　`{m['位置']}`")
            st.caption(m["說明"])


# ---------------------------------------------------------------------------
# 頁面入口
# ---------------------------------------------------------------------------

def page_diagnostics() -> None:
    st.header("🩺 資料診斷")
    st.caption(
        "全站資料健康的單一觀測面板:各 JSON 新鮮度、9 大資料源連通性、mock 界線。"
        "本頁只讀取與探測,不改動任何資料。"
    )
    sec_file_freshness()
    st.divider()
    sec_source_probe()
    st.divider()
    sec_mock_inventory()
