"""app.py — Streamlit 入口:側邊欄路由 → 各頁模組。

本地執行: streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

import line_notify
from app_core import render_proxy_status
from pages.tw import page_tw
from pages.us import page_us
from pages.global_ import page_global
from pages.housing import page_housing
from pages.ai_brain import page_ai_brain
from pages.etf import page_etf
from pages.diagnostics import page_diagnostics


def main() -> None:
    st.set_page_config(page_title="全球政經戰略看板", page_icon="🌐", layout="wide")
    # 隱藏 Streamlit 自動偵測 pages/ 產生的多頁導覽列（已有自訂 radio 導覽）
    st.markdown(
        "<style>[data-testid='stSidebarNav']{display:none}</style>",
        unsafe_allow_html=True,
    )
    st.title("🌐 全球政經戰略每日看板")
    st.caption(f"{line_notify.MORNING_TAGLINE} — 資料為 AI/工具自動生成,僅供參考,非投資建議")

    st.sidebar.header("📂 領域")
    view = st.sidebar.radio(
        "選擇", ["📊 台股", "🇺🇸 美股", "🌍 全球", "🏠 台灣房市", "🧩 ETF 工作台",
               "🧠 AI 決策大腦", "🩺 資料診斷"])
    st.sidebar.caption("點一個領域,該領域所有面板一次展開,最上方有 AI 今日總結。")
    st.sidebar.divider()
    with st.sidebar:
        render_proxy_status()
        st.checkbox(
            "💾 抓取後自動存到 GitHub", value=False, key="auto_save_github",
            help="勾選後,各面板『即時抓取』完成即自動 commit 對應 JSON 回 repo(需設 GITHUB_TOKEN)。"
                 "預設關閉:抓取與存檔解耦,免去每次抓取多等 2~8 秒的同步 commit;"
                 "內容未變更時會自動略過 commit。",
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
    elif view == "🩺 資料診斷":
        page_diagnostics()
    else:
        page_etf()


if __name__ == "__main__":
    main()
