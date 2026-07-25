"""pages/us.py — 美股頁:美股觀察 + 個股健診。"""
from __future__ import annotations

import streamlit as st

import freshness
import update_data
import tz_utils
import ui_helpers
from app_core import (
    STALE_REPORT_DAYS,
    US_STOCKS_PATH,
    US_STOCKS_ARCHIVE_DIR,
    INTL_ALERT_PATH,
    SIX_MONTH_SOURCE_CAPTION,
    ensure_gemini_key,
    fetch_live_news_cached,
    load_json,
    render_news_cards,
    pick_report,
    render_market_digest,
    render_stock_bubble,
    render_index_quotes,
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
                    st.session_state["live_us_stock_news"] = fetch_live_news_cached("us_stock")
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
    note = freshness.stale_note(data.get("report_date"), STALE_REPORT_DAYS, "美股觀察")
    if note:
        st.warning(note)
    if data.get("summary"):
        st.info(data["summary"])
    st.caption("依新聞『被提及次數』排序;標的分利多/利空/觀望。⚠️ 僅為新聞整理,非投資建議。")
    st.caption(SIX_MONTH_SOURCE_CAPTION)

    stocks = data.get("stocks", [])
    if not stocks:
        st.info("本次未整理出美股標的。")
        return

    # 總覽用泡泡圖(取代寬表 → 去掉表+卡重印同一批標的);逐檔理由/佐證收進「個股詳情」expander。
    st.subheader("📊 美股標的總覽(新聞提及 × 傾向)")
    render_stock_bubble(stocks)
    with st.expander("📇 個股詳情(利多 / 利空 / 觀望卡片 + 佐證新聞)"):
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

def sec_us_indices() -> None:
    """美股大盤指數:讀既有 latest_intl_alert.json 的真實 Yahoo 報價(標普/那斯達克/道瓊/費半+美股期貨+美元/利率)。"""
    st.subheader("📊 美股大盤與美元/利率(真實報價)")
    data = load_json(INTL_ALERT_PATH)
    quotes = (data or {}).get("quotes") or {}
    # 只留美股相關組別(美股指數/美股期貨/債匯);台股期貨=台指期夜盤,屬台股頁國際盤,不放美股頁避免誤導。
    us_quotes = {k: v for k, v in quotes.items() if v.get("group") != "台股期貨"}
    if not us_quotes:
        st.info("尚無指數報價存檔。可到 📊 台股頁「🌏 國際盤預警」即時抓取,或等每日排程產生。")
        return
    st.caption(f"資料時間:{(data or {}).get('as_of', '—')}　|　來源:Yahoo Finance　|　"
               "漲跌幅=最新 vs 前收(免金鑰、非 AI 估算)。")
    render_index_quotes(us_quotes)
    st.caption("⚠️ 真實市場報價,非投資建議。這些美股指數對台股開盤屬『隔夜領先』訊號——"
               "台股盤前的完整解讀(含台指期夜盤)見 📊 台股頁「🌏 國際盤預警」。")


def page_us() -> None:
    st.header("🇺🇸 美股")
    ui_helpers.render_intro_banner(
        page_key="us",
        title="美股頁",
        steps=[
            "看 📈 **美股觀察**：AI 依新聞統計的熱門美股，欄位「傾向」= 利多/利空/觀望代表新聞情緒方向。",
            "注意「則數」欄：被提及次數越多代表近期市場關注度越高，但不等於未來一定上漲。",
            "善用 🩺 **個股健診工具**（下方互動區）：輸入任何美股代號可看完整分析，包含護城河與估值。",
        ],
    )
    payload = {"美股觀察": load_json(US_STOCKS_PATH)}
    render_market_digest("美股", {k: v for k, v in payload.items() if v})
    st.divider(); sec_us_indices()
    st.divider(); sec_us_stocks()
    st.divider()
    st.markdown("### 🛠 互動工具")
    with st.expander("🩺 個股健診 — 美股也能查(輸入 Nvidia / NVDA …)"):
        tool_stock_query()
