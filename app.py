"""Streamlit 前端 — 全球政經戰略報告 + 趨勢雷達。

側邊欄可切換報告類型(戰略報告 / 趨勢雷達),並瀏覽歷史存檔。
本地執行: streamlit run app.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

import update_data  # 重用爬蟲 + Gemini 管線,讓網頁可即時抓新聞/產報告

REPORT_PATH = Path("latest_report.json")
ARCHIVE_DIR = Path("data/reports")
TRENDS_PATH = Path("latest_trends.json")
TRENDS_ARCHIVE_DIR = Path("data/trends")

ANALYSIS_SECTIONS = [
    ("geo_military", "🛰️ 一、地緣政治與軍事戰略"),
    ("supply_chain", "🛢️ 二、原物料與供應鏈傳導"),
    ("macro_economy", "💵 三、總體經濟與貨幣定價"),
    ("blind_spots_and_kpi", "🌏 四、全球大局觀與領先指標"),
]

SIGNAL_LABELS = [
    ("funding", "💰 資金流向"),
    ("hiring", "🧑‍💻 徵才動能"),
    ("policy", "📜 政策動向"),
    ("technology", "🔬 技術動能"),
]


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def list_archive(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted((p.name for p in directory.glob("*.json")), reverse=True)


def load_trend_history(archive_dir: Path) -> "pd.DataFrame | None":
    """彙整所有歷史趨勢存檔成『日期 × 產業』的熱度表,供折線圖使用。"""
    rows = []
    for p in sorted(archive_dir.glob("*.json")):
        data = load_json(p)
        if not data:
            continue
        date = data.get("report_date") or p.stem
        for t in data.get("trends", []):
            industry = t.get("industry")
            heat = t.get("heat_score")
            if industry and isinstance(heat, (int, float)):
                rows.append({"date": date, "industry": industry, "heat": heat})
    if not rows:
        return None
    df = pd.DataFrame(rows)
    pivot = df.pivot_table(index="date", columns="industry", values="heat", aggfunc="mean")
    return pivot.sort_index()


def pick_report(latest_path: Path, archive_dir: Path):
    """側邊欄報告選擇器,回傳選定的 dict。"""
    archive = list_archive(archive_dir)
    choice = st.sidebar.selectbox("選擇日期", ["最新 (latest)"] + archive)
    if archive:
        st.sidebar.caption(f"歷史存檔:{len(archive)} 份")
    if choice == "最新 (latest)":
        return load_json(latest_path)
    return load_json(archive_dir / choice)


# ---------------------------------------------------------------------------
# 新聞來源說明 + 即時抓取(免等每日排程)
# ---------------------------------------------------------------------------

NEWS_SOURCE_CAPTION = (
    "新聞來源(繁中):**Google News RSS（台灣)**（依關鍵字聚合中央社、聯合報、"
    "自由時報、中時、BBC 中文、DW 中文等可信媒體)＋ 中央社（國際/兩岸/財經)、"
    "BBC 中文、德國之聲 DW 的官方 RSS feed。只取開放 feed 的標題/來源/連結/摘要,"
    "不爬付費牆全文。"
)


def get_topic() -> str:
    return os.environ.get("REPORT_TOPIC") or update_data.DEFAULT_TOPIC


def ensure_gemini_key() -> bool:
    """從環境變數或 Streamlit Secrets 取得 GEMINI_API_KEY。"""
    if os.environ.get("GEMINI_API_KEY"):
        return True
    try:
        key = st.secrets["GEMINI_API_KEY"]
    except Exception:  # noqa: BLE001 — 沒設定 secrets 時直接視為無金鑰
        key = None
    if key:
        os.environ["GEMINI_API_KEY"] = str(key)
    return bool(os.environ.get("GEMINI_API_KEY"))


def render_news_cards(news: list[dict]) -> None:
    for item in news:
        title = item.get("title", "(無標題)")
        source = item.get("source", "")
        url = item.get("url", "")
        header = f"**{title}**" + (f" — _{source}_" if source else "")
        with st.container(border=True):
            st.markdown(header)
            if item.get("published"):
                st.caption(f"🕒 {item['published']}")
            st.write(item.get("summary", ""))
            if url:
                st.markdown(f"[原文連結]({url})")


def render_live_panel() -> None:
    """第一步:只負責『抓新聞』,結果存進 session_state(Gemini 分析另由按鈕觸發)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時抓取(免等每日排程)")
        st.caption(NEWS_SOURCE_CAPTION)
        st.caption("流程:① 先抓新聞 → ② 看過後,再按 Gemini 按鈕做分析+白話文。")

        if st.button("🔄 ① 立即抓取最新新聞", use_container_width=True):
            with st.spinner("抓取真實外電中…"):
                try:
                    st.session_state["live_news"] = update_data.fetch_macro_news(get_topic())
                    st.session_state.pop("live_report", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["live_news"] = []
                    st.error(f"抓取失敗:{exc}")


def generate_live_report() -> None:
    """第二步:對『已抓到的新聞』請 Gemini 做四維度分析 + 白話文。"""
    news = st.session_state.get("live_news", [])
    topic = get_topic()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    analysis = update_data.get_macro_analysis(news, topic, today)
    st.session_state["live_report"] = {
        "report_date": today,
        "topic": topic,
        "raw_news": news,
        "strategic_analysis": analysis["strategic_analysis"],
        "laymans_dictionary": analysis["laymans_dictionary"],
        "dictionary_source": "gemini",
    }
    st.session_state.pop("live_news", None)


def render_trend_live_panel() -> None:
    """趨勢雷達第一步:只抓產業新聞(排名打分另由 Gemini 按鈕觸發)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時產生(免等每日排程)")
        st.caption(NEWS_SOURCE_CAPTION)
        st.caption("流程:① 先抓產業新聞 → ② 看過後,再按 Gemini 按鈕排名打分。")

        if st.button("🔄 ① 立即抓取產業新聞", use_container_width=True):
            with st.spinner("抓取產業新聞中…"):
                try:
                    st.session_state["live_trend_news"] = update_data.fetch_trend_news()
                    st.session_state.pop("live_trends", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["live_trend_news"] = []
                    st.error(f"抓取失敗:{exc}")


def generate_live_trends() -> None:
    """趨勢雷達第二步:對『已抓到的產業新聞』請 Gemini 排名打分。"""
    news = st.session_state.get("live_trend_news", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st.session_state["live_trends"] = update_data.get_trend_radar(news, today)
    st.session_state.pop("live_trend_news", None)


# ---------------------------------------------------------------------------
# 戰略報告
# ---------------------------------------------------------------------------

def render_report(report: dict) -> None:
    col1, col2, col3 = st.columns(3)
    col1.metric("報告日期", report.get("report_date", "—"))
    col2.metric("分析主題", report.get("topic", "—"))
    col3.metric("白話文來源", report.get("dictionary_source", "—"))
    st.divider()

    st.header("📰 第一階段:原始情報彙整")
    news = report.get("raw_news", [])
    if not news:
        st.info("本次未取得相關新聞。")
    render_news_cards(news)

    st.header("🧭 第二階段:四維度專業戰略分析")
    analysis = report.get("strategic_analysis", {})
    for key, label in ANALYSIS_SECTIONS:
        with st.expander(label, expanded=True):
            st.write(analysis.get(key, "(無內容)"))

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


# ---------------------------------------------------------------------------
# 趨勢雷達
# ---------------------------------------------------------------------------

def render_trends(data: dict) -> None:
    st.metric("資料日期", data.get("report_date", "—"))
    st.caption("依「資金 / 徵才 / 政策 / 技術」四種訊號綜合評估,熱度 0–100。")
    st.divider()

    trends = data.get("trends", [])
    if not trends:
        st.info("本次未產生趨勢資料。")
        return

    for t in trends:
        rank = t.get("rank", "")
        industry = t.get("industry", "(未命名)")
        heat = t.get("heat_score", 0)
        with st.container(border=True):
            st.subheader(f"#{rank} {industry}　🔥 熱度 {heat}")
            try:
                st.progress(min(max(int(heat), 0), 100) / 100)
            except (TypeError, ValueError):
                pass
            if t.get("summary"):
                st.write(t["summary"])

            signals = t.get("signals", {})
            cols = st.columns(2)
            for i, (key, label) in enumerate(SIGNAL_LABELS):
                with cols[i % 2]:
                    st.markdown(f"**{label}**")
                    st.caption(signals.get(key, "—"))

            indicators = t.get("leading_indicators", [])
            if indicators:
                st.markdown("**📌 該緊盯的領先指標**")
                for ind in indicators:
                    st.markdown(f"- {ind}")

            evidence = t.get("evidence_news", [])
            if evidence:
                with st.expander("📰 佐證新聞"):
                    for n in evidence:
                        title = n.get("title", "")
                        source = n.get("source", "")
                        url = n.get("url", "")
                        line = f"- {title}" + (f" — _{source}_" if source else "")
                        if url:
                            line += f" [連結]({url})"
                        st.markdown(line)

    st.caption("⚠️ 趨勢評估由 AI 自動生成,非投資建議。新聞屬同步/落後訊號,請搭配資金與徵才等領先指標判讀。")


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="全球政經戰略看板", page_icon="🌐", layout="wide")
    st.title("🌐 全球政經戰略每日看板")

    st.sidebar.header("📂 報告類型")
    report_type = st.sidebar.radio("選擇", ["戰略報告", "趨勢雷達"])
    st.sidebar.divider()
    st.sidebar.header("📅 報告選擇")

    if report_type == "戰略報告":
        render_live_panel()

        # 1) 本次即時產生的完整報告(含 Gemini 分析)優先顯示
        if st.session_state.get("live_report"):
            live = st.session_state["live_report"]
            st.success("⚡ 以下為剛剛即時產生的報告(尚未存檔)。")
            st.download_button(
                "⬇️ 下載這份報告 JSON",
                data=json.dumps(live, ensure_ascii=False, indent=2),
                file_name=f"report_{live.get('report_date', 'latest')}.json",
                mime="application/json",
            )
            st.divider()
            render_report(live)
            return

        # 2) 已抓到新聞、尚未分析:顯示新聞,並提供第二步的 Gemini 按鈕
        if "live_news" in st.session_state:
            news = st.session_state["live_news"]
            st.divider()
            st.header("📰 即時抓取的新聞")
            if news:
                st.success(f"已抓到 {len(news)} 則真實外電,確認後再請 Gemini 分析:")
                has_key = ensure_gemini_key()
                if st.button(
                    "🧠 ② 用 Gemini 產生戰略分析 + 白話文",
                    use_container_width=True,
                    disabled=not has_key,
                    help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
                ):
                    with st.spinner("Gemini 分析中(約 10–30 秒)…"):
                        try:
                            generate_live_report()
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"產生報告失敗:{exc}")
                if not has_key:
                    st.caption(
                        "ℹ️ 尚未偵測到 GEMINI_API_KEY。看新聞不需金鑰;要做分析+白話文,"
                        "請到 Streamlit Cloud → App settings → Secrets 加上 "
                        "`GEMINI_API_KEY = \"...\"`。"
                    )
                st.download_button(
                    "⬇️ 下載新聞 JSON",
                    data=json.dumps(news, ensure_ascii=False, indent=2),
                    file_name="news.json",
                    mime="application/json",
                )
                render_news_cards(news)
            else:
                st.info("這次沒抓到新聞,稍後再試或調整關鍵字。")
            return

        # 3) 否則顯示每日排程存檔的報告
        report = pick_report(REPORT_PATH, ARCHIVE_DIR)
        if report is None:
            st.warning("尚無每日排程報告。可用上方「⚡ 即時抓取」按鈕馬上取得新聞或產生報告。")
            return
        render_report(report)
    else:
        st.header("🔥 趨勢雷達 — 現在最紅的產業")
        render_trend_live_panel()

        # 1) 本次即時產生的趨勢雷達優先顯示
        if st.session_state.get("live_trends"):
            live = st.session_state["live_trends"]
            st.success("⚡ 以下為剛剛即時產生的趨勢雷達(尚未存檔)。")
            st.download_button(
                "⬇️ 下載趨勢 JSON",
                data=json.dumps(live, ensure_ascii=False, indent=2),
                file_name=f"trends_{live.get('report_date', 'latest')}.json",
                mime="application/json",
            )
            st.divider()
            render_trends(live)
            return

        # 2) 已抓到產業新聞、尚未排名:顯示新聞,並提供第二步的 Gemini 按鈕
        if "live_trend_news" in st.session_state:
            news = st.session_state["live_trend_news"]
            st.divider()
            st.subheader("📰 即時抓取的產業新聞")
            if news:
                st.success(f"已抓到 {len(news)} 則產業新聞,確認後再請 Gemini 排名打分:")
                has_key = ensure_gemini_key()
                if st.button(
                    "🧠 ② 用 Gemini 產生趨勢雷達",
                    use_container_width=True,
                    disabled=not has_key,
                    help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
                ):
                    with st.spinner("Gemini 排名打分中(約 10–30 秒)…"):
                        try:
                            generate_live_trends()
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"產生趨勢雷達失敗:{exc}")
                if not has_key:
                    st.caption(
                        "ℹ️ 尚未偵測到 GEMINI_API_KEY。看新聞不需金鑰;要排名打分,"
                        "請到 Streamlit Cloud → App settings → Secrets 加上 "
                        "`GEMINI_API_KEY = \"...\"`。"
                    )
                st.download_button(
                    "⬇️ 下載產業新聞 JSON",
                    data=json.dumps(news, ensure_ascii=False, indent=2),
                    file_name="trend_news.json",
                    mime="application/json",
                )
                render_news_cards(news)
            else:
                st.info("這次沒抓到產業新聞,稍後再試或調整 TREND_QUERIES 關鍵字。")
            return

        # 3) 否則顯示每日排程存檔 + 歷史折線圖
        history = load_trend_history(TRENDS_ARCHIVE_DIR)
        if history is not None and len(history.index) >= 2:
            st.subheader("📈 產業熱度趨勢(歷史)")
            st.caption("看出『網路 → AI』式的長期轉移:哪條線持續往上,就是動能最強的產業。")
            st.line_chart(history)
            st.divider()
        elif history is not None:
            st.info("歷史折線圖需累積至少兩天的資料,明天就會開始出現。")

        data = pick_report(TRENDS_PATH, TRENDS_ARCHIVE_DIR)
        if data is None:
            st.warning("尚無每日趨勢雷達存檔。可用上方「⚡ 即時產生」按鈕馬上取得。")
            return
        render_trends(data)


if __name__ == "__main__":
    main()
