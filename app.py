"""app.py — Streamlit 入口:側邊欄路由 → 各頁模組。

本地執行: streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from app_core import render_proxy_status
from pages.tw import page_tw
from pages.us import page_us
from pages.global_ import page_global
from pages.housing import page_housing
from pages.ai_brain import page_ai_brain
from pages.etf import page_etf


def main() -> None:
    st.set_page_config(page_title="全球政經戰略看板", page_icon="🌐", layout="wide")
    st.title("🌐 全球政經戰略每日看板")

    st.sidebar.header("📂 領域")
    view = st.sidebar.radio(
        "選擇", ["📊 台股", "🇺🇸 美股", "🌍 全球", "🏠 台灣房市", "🧩 ETF 工作台", "🧠 AI 決策大腦"])
    st.sidebar.caption("點一個領域,該領域所有面板一次展開,最上方有 AI 今日總結。")
    st.sidebar.divider()
    with st.sidebar:
        render_proxy_status()
        st.checkbox(
            "💾 抓取後自動存到 GitHub", value=True, key="auto_save_github",
            help="勾選後,各面板『即時抓取』完成即自動 commit 對應 JSON 回 repo。需設 GITHUB_TOKEN。",
        )

    if view == "📊 台股":
        page_tw()
    elif view == "🇺🇸 美股":
        page_us()
    elif view == "🌍 全球":
        page_global()
    elif view == "🏠 台灣房市":
        page_housing()
    elif view == "🧠 AI 決策大腦":
        page_ai_brain()
    else:
        page_etf()


if __name__ == "__main__":
    main()
