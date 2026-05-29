"""Streamlit 前端 — 顯示每日全球政經戰略報告。

- 主畫面以四維度 + 白話字典呈現選定的報告。
- 側邊欄可切換「最新報告」與 data/reports/ 內的歷史存檔。
本地執行: streamlit run app.py
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

REPORT_PATH = Path("latest_report.json")
ARCHIVE_DIR = Path("data/reports")

ANALYSIS_SECTIONS = [
    ("geo_military", "🛰️ 一、地緣政治與軍事戰略"),
    ("supply_chain", "🛢️ 二、原物料與供應鏈傳導"),
    ("macro_economy", "💵 三、總體經濟與貨幣定價"),
    ("blind_spots_and_kpi", "🌏 四、全球大局觀與領先指標"),
]


def load_report(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def list_archive() -> list[str]:
    if not ARCHIVE_DIR.exists():
        return []
    return sorted((p.name for p in ARCHIVE_DIR.glob("*.json")), reverse=True)


def render_report(report: dict) -> None:
    col1, col2, col3 = st.columns(3)
    col1.metric("報告日期", report.get("report_date", "—"))
    col2.metric("分析主題", report.get("topic", "—"))
    col3.metric("白話文來源", report.get("dictionary_source", "—"))
    st.divider()

    # 第一階段:原始情報
    st.header("📰 第一階段:原始情報彙整")
    news = report.get("raw_news", [])
    if not news:
        st.info("本次未取得相關新聞。")
    for item in news:
        title = item.get("title", "(無標題)")
        source = item.get("source", "")
        url = item.get("url", "")
        header = f"**{title}**" + (f" — _{source}_" if source else "")
        with st.container(border=True):
            st.markdown(header)
            st.write(item.get("summary", ""))
            if url:
                st.markdown(f"[原文連結]({url})")

    # 第二階段:四維度分析
    st.header("🧭 第二階段:四維度專業戰略分析")
    analysis = report.get("strategic_analysis", {})
    for key, label in ANALYSIS_SECTIONS:
        with st.expander(label, expanded=True):
            st.write(analysis.get(key, "(無內容)"))

    # 第三階段:白話字典
    st.header("📖 第三階段:白話文翻譯字典")
    dictionary = report.get("laymans_dictionary", [])
    if dictionary:
        st.table(
            [{"專業術語": d.get("term", ""), "白話文意思": d.get("explanation", "")}
             for d in dictionary]
        )
    else:
        st.info("本次無術語需要翻譯。")

    st.caption("⚠️ 本報告由 AI 自動生成,非投資建議。局勢以最新確認消息為準。")


def main() -> None:
    st.set_page_config(page_title="全球政經戰略看板", page_icon="🌐", layout="wide")
    st.title("🌐 全球政經戰略每日看板")

    # 側邊欄:報告選擇
    st.sidebar.header("📅 報告選擇")
    archive = list_archive()
    options = ["最新 (latest)"] + archive
    choice = st.sidebar.selectbox("選擇報告", options)

    if choice == "最新 (latest)":
        report = load_report(REPORT_PATH)
    else:
        report = load_report(ARCHIVE_DIR / choice)

    if archive:
        st.sidebar.caption(f"歷史存檔:{len(archive)} 份")

    if report is None:
        st.warning("尚無報告資料(找不到或無法解析報告 JSON)。請先執行 update_data.py。")
        return

    render_report(report)


if __name__ == "__main__":
    main()
