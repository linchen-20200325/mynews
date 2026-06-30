"""pages/us.py — 美股頁:美股觀察 + 個股健診。"""
from __future__ import annotations

import streamlit as st

import update_data
import tz_utils
from app_core import (
    US_STOCKS_PATH,
    US_STOCKS_ARCHIVE_DIR,
    SIX_MONTH_SOURCE_CAPTION,
    ensure_gemini_key,
    load_json,
    render_news_cards,
    pick_report,
    render_market_digest,
    _render_stock_card_group,
    _render_trends_sunset,
)
from pages.tw import tool_stock_query

def render_us_stock_live_panel() -> None:
    """美股觀察第一步:只抓美股財經新聞(整理另由 Gemini 按鈕觸發)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時產生(免等每日排程)")
        st.caption(
            "從美股財經新聞統計被提到最多次的美股標的,分利多/利空/觀望,"
            "並歸納未來趨勢與夕陽產業。流程:① 先抓財經新聞 → ② 看過後再按 Gemini 整理。"
        )
        if st.button("🔄 ① 立即抓取美股財經新聞", use_container_width=True):
            with st.spinner("抓取美股財經新聞中…"):
                try:
                    st.session_state["live_us_stock_news"] = update_data.fetch_us_stock_news()
                    st.session_state.pop("live_us_stocks", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["live_us_stock_news"] = []
                    st.error(f"抓取失敗:{exc}")


def generate_live_us_stocks() -> None:
    """美股觀察第二步:對『已抓到的財經新聞』請 Gemini 整理美股標的。"""
    news = st.session_state.get("live_us_stock_news", [])
    today = tz_utils.taiwan_today()
    st.session_state["live_us_stocks"] = update_data.get_us_stock_picks(news, today)
    st.session_state.pop("live_us_stock_news", None)


def render_us_stocks(data: dict) -> None:
    st.metric("資料日期", data.get("report_date", "—"))
    if data.get("summary"):
        st.info(data["summary"])
    st.caption("依新聞『被提及次數』排序;標的分利多/利空/觀望。⚠️ 僅為新聞整理,非投資建議。")
    st.caption(SIX_MONTH_SOURCE_CAPTION)

    stocks = data.get("stocks", [])
    if not stocks:
        st.info("本次未整理出美股標的。")
        return

    # 總表(依新聞提及次數 + 首見/最近見報)
    st.subheader("📋 美股標的總表(新聞提及 × 見報區間)")
    st.caption("被很多新聞提及 ＋ 偏利多 = 相對更受關注;首見/最近/則數由真實新聞統計。")
    st.dataframe(
        [
            {
                "標的": s.get("name", ""),
                "代號": s.get("ticker", ""),
                "產業": s.get("sector", ""),
                "則數": s.get("news_count", s.get("mention_count", 0)),
                "首見": s.get("first_seen", ""),
                "最近": s.get("last_seen", ""),
                "傾向": s.get("sentiment", ""),
                "原因": s.get("reason", ""),
            }
            for s in stocks
        ],
        use_container_width=True,
        hide_index=True,
    )

    _render_stock_card_group(stocks)
    _render_trends_sunset(data)
    st.caption("⚠️ 本頁由 AI 自動整理新聞而成,可能有誤,僅供參考,非投資建議。")

def sec_us_stocks() -> None:
    st.subheader("📈 美股觀察 — 值得關注的美股標的")
    with st.expander("⚡ 即時重新抓取美股觀察"):
        render_us_stock_live_panel()
        if "live_us_stock_news" in st.session_state and not st.session_state.get("live_us_stocks"):
            news = st.session_state["live_us_stock_news"]
            if news:
                st.success(f"已抓到 {len(news)} 則財經新聞:")
                if st.button("🧠 ② 用 Gemini 整理美股標的", key="uss_step2",
                             disabled=not ensure_gemini_key()):
                    with st.spinner("Gemini 整理美股標的中…"):
                        try:
                            generate_live_us_stocks(); st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"整理美股標的失敗:{exc}")
                render_news_cards(news)
    data = st.session_state.get("live_us_stocks") or pick_report(US_STOCKS_PATH, US_STOCKS_ARCHIVE_DIR)
    if data is None:
        st.info("尚無美股觀察存檔。可用上方『即時產生』取得。")
        return
    render_us_stocks(data)

def page_us() -> None:
    st.header("🇺🇸 美股")
    payload = {"美股觀察": load_json(US_STOCKS_PATH)}
    render_market_digest("美股", {k: v for k, v in payload.items() if v})
    st.divider(); sec_us_stocks()
    st.divider()
    st.markdown("### 🛠 互動工具")
    with st.expander("🩺 個股健診 — 美股也能查(輸入 Nvidia / NVDA …)"):
        tool_stock_query()
