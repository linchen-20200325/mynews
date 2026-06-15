"""etf_data.py — ETF 資料的單一真相源(Single Source of Truth)。

設計目的(回應重構需求):
  * 全專案「載入 ETF 成分股 / 基本資料」與「反查、檔數計算」只走這一個入口,
    杜絕 app.py 各處重複 load_holdings() 讀檔解析、重複 reverse_index() 重算。
  * 全部包上 @st.cache_data(ttl=3600):相同資料一小時內只讀檔/計算一次,
    切換頁面(或未來 Tab)時直接命中快取,底層數值保證一致。

資料/UI 分離:本模組「只負責資料」,不畫任何 Streamlit 元件;
  app.py 的 render_* 只管畫面,需要數據就向這裡要。

注意:使用者按「立即抓取」得到的即時資料放在 st.session_state(live),
  仍由呼叫端以 `live or get_holdings()` 優先採用;本層只快取「檔案後援」這條路。
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import etf_holdings
import etf_profile_fetcher

# 與 app.py 既有常數一致:成分股設定檔路徑(專案根目錄)
HOLDINGS_PATH = Path("etf_holdings.json")

_CACHE_TTL = 3600  # 秒:資料變動不頻繁,一小時重抓一次即可


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def get_holdings() -> dict | None:
    """ETF→成分股 設定檔(快取)。不存在/格式錯回 None,與原 load_holdings 行為一致。"""
    return etf_holdings.load_holdings(HOLDINGS_PATH)


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def get_reverse_index() -> list[dict]:
    """個股反查清單 [{ticker,name,etf_count,etfs}](快取);無資料回空陣列。"""
    data = etf_holdings.load_holdings(HOLDINGS_PATH)
    return etf_holdings.reverse_index(data) if data else []


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def get_etf_count_map() -> dict[str, int]:
    """{ticker: 被幾檔 ETF 持有}(快取),供台股觀察等頁交叉參照——與反查同一來源。"""
    return {r["ticker"]: r["etf_count"] for r in get_reverse_index()}


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def get_profiles() -> dict:
    """ETF 圖鑑基本資料(型態/配息/費用…)(快取)。"""
    return etf_profile_fetcher.load_profiles()


def clear_cache() -> None:
    """資料更新(例如剛存檔/重抓)後手動清快取,讓下次讀到最新檔案。"""
    for fn in (get_holdings, get_reverse_index, get_etf_count_map, get_profiles):
        fn.clear()
