"""pages/global_.py — 全球頁:戰略報告 + 趨勢雷達 + 人物追蹤。"""
from __future__ import annotations

import streamlit as st

import freshness
import update_data
import tz_utils
import ui_helpers
from app_core import (
    NEWS_SOURCE_CAPTION,
    SIGNAL_LABELS,
    ANALYSIS_SECTIONS,
    SIX_MONTH_SOURCE_CAPTION,
    STALE_REPORT_DAYS,
    REPORT_PATH,
    REPORTS_MULTI_PATH,
    ARCHIVE_DIR,
    TRENDS_PATH,
    TRENDS_ARCHIVE_DIR,
    FOCUS_PATH,
    FOCUS_ARCHIVE_DIR,
    ensure_gemini_key,
    render_key_hint,
    render_news_cards,
    pick_report,
    load_json,
    load_trend_history,
    render_market_digest,
    _render_evidence_news,
    mention_caption,
    SENTIMENT_STYLE,
    get_topic,
)

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
    today = tz_utils.taiwan_today()
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
    today = tz_utils.taiwan_today()
    st.session_state["live_trends"] = update_data.get_trend_radar(news, today)
    st.session_state.pop("live_trend_news", None)

def render_focus_panel() -> None:
    """第一步:中文輸入 → Gemini 翻英 → 抓全球英文新聞。"""
    with st.container(border=True):
        st.markdown("#### 🔎 輸入關注對象(中文)")
        st.caption(
            "例如:川普、黃仁勳、輝達、AI 晶片。系統會自動翻成英文抓全球新聞,"
            "再請 Gemini 整理他說了什麼、衍伸哪些產業,以及可能牽動哪些台股/美股。"
        )
        term = st.text_input(
            "關注對象", key="focus_term_input", placeholder="川普 / 黃仁勳 / 輝達 / AI 晶片 …"
        )
        has_key = ensure_gemini_key()
        if st.button(
            "🌍 ① 翻譯並抓全球新聞",
            use_container_width=True,
            disabled=not has_key,
            help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
        ):
            if not term.strip():
                st.warning("請先輸入要追蹤的中文關鍵字。")
            else:
                with st.spinner("翻譯關鍵字並抓取全球新聞中…"):
                    try:
                        tr = update_data.translate_focus_query(term.strip())
                        news = update_data.fetch_focus_news(
                            tr.get("query_en", ""), tr.get("aliases"),
                            tr.get("query_zh", term.strip()), tr.get("zh_aliases"),
                        )
                        st.session_state["live_focus_translation"] = tr
                        st.session_state["live_focus_news"] = news
                        st.session_state["live_focus_term"] = term.strip()
                        st.session_state.pop("live_focus", None)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"抓取失敗:{exc}")
        if not has_key:
            render_key_hint()


def generate_live_focus() -> None:
    """第二步:對『已抓到的全球新聞』請 Gemini 做關聯分析。"""
    tr = st.session_state.get("live_focus_translation", {})
    news = st.session_state.get("live_focus_news", [])
    term = st.session_state.get("live_focus_term", tr.get("query_zh", ""))
    today = tz_utils.taiwan_today()
    st.session_state["live_focus"] = update_data.get_focus_analysis(
        term, tr.get("query_en", ""), news, today
    )
    st.session_state.pop("live_focus_news", None)


def render_focus(data: dict) -> None:
    col1, col2 = st.columns(2)
    col1.metric("關注對象", data.get("query_zh", "—"))
    col2.metric("英文檢索", data.get("query_en", "—"))
    if data.get("summary"):
        st.info(data["summary"])
    st.caption("由 AI 依全球新聞整理,可能有誤,僅供參考,非投資建議。")
    if data.get("first_seen"):
        st.caption(
            f"🗓️ 相關新聞 {data['first_seen']} ～ {data.get('last_seen', '')}"
            f"，共 {data.get('news_count', 0)} 則(回溯約 6 個月,實際範圍受 RSS 限制)。"
        )

    statements = data.get("key_statements", [])
    if statements:
        st.subheader("🗣️ 他說了什麼 / 關鍵動向")
        for s in statements:
            st.markdown(f"- {s}")

    industries = data.get("affected_industries", [])
    if industries:
        st.subheader("🏭 衍伸 / 受影響產業")
        st.markdown("　".join(f"`{i}`" for i in industries))

    stocks = data.get("stocks", [])
    if stocks:
        st.subheader("📈 可能牽動的個股(台股 / 美股)")
        st.dataframe(
            [
                {
                    "市場": s.get("market", ""),
                    "標的": s.get("name", ""),
                    "代號": s.get("ticker", ""),
                    "產業": s.get("sector", ""),
                    "則數": s.get("news_count", 0),
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
        for market in ("台股", "美股"):
            group = [s for s in stocks if s.get("market") == market]
            if not group:
                continue
            st.markdown(f"##### 🏷️ {market}（{len(group)} 檔）")
            for s in group:
                name = s.get("name", "")
                ticker = s.get("ticker", "")
                head = f"**{name}**" + (f"（{ticker}）" if ticker else "")
                sector = s.get("sector", "")
                senti = s.get("sentiment", "")
                emoji, _ = SENTIMENT_STYLE.get(senti, ("", "info"))
                with st.container(border=True):
                    st.markdown(
                        head
                        + (f"　·　{sector}" if sector else "")
                        + (f"　·　{emoji} {senti}" if senti else "")
                    )
                    cap = mention_caption(s)
                    if cap:
                        st.caption(cap)
                    if s.get("reason"):
                        st.write(s["reason"])
                    _render_evidence_news(s.get("evidence_news", []))
    else:
        st.info("本次新聞未對應到明確的台股/美股個股。")

    _render_evidence_news(data.get("evidence_news", []), "📰 全部佐證新聞")

    st.caption("⚠️ 本頁由 AI 自動整理新聞而成,可能有誤,僅供參考,非投資建議。")

def render_report(report: dict) -> None:
    # §2.4 新鮮度:報告歸屬日落後今日過久 → 顯式警告,不讓過期資料偽裝成最新。
    note = freshness.stale_note(report.get("report_date"), STALE_REPORT_DAYS, "戰略報告")
    if note:
        st.warning(note)
    col1, col2, col3 = st.columns(3)
    col1.metric("報告日期", report.get("report_date", "—"))
    col2.metric("分析主題", report.get("topic", "—"))
    col3.metric("白話文來源", report.get("dictionary_source", "—"))
    st.divider()

    st.header("📰 第一階段:原始情報彙整")
    span = report.get("news_span", {})
    if span.get("first_seen"):
        st.caption(
            f"🗓️ 取材新聞 {span['first_seen']} ～ {span.get('last_seen', '')}"
            f"，共 {span.get('news_count', 0)} 則(回溯約 6 個月,實際範圍受 RSS 限制)。"
        )
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
    st.caption("依「資金 / 徵才 / 政策 / 技術」四種訊號綜合評估,熱度 0–100。新聞來源同時含台灣與美股。")
    st.caption(SIX_MONTH_SOURCE_CAPTION)
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
            cap = mention_caption(t)
            if cap:
                st.caption(cap)

            us_stocks = t.get("us_stocks", [])
            tw_stocks = t.get("tw_stocks", [])
            if us_stocks or tw_stocks:
                scol1, scol2 = st.columns(2)
                with scol1:
                    st.markdown("**🇺🇸 美股代表**")
                    if us_stocks:
                        for s in us_stocks:
                            nm = s.get("name", "") if isinstance(s, dict) else str(s)
                            tk = s.get("ticker", "") if isinstance(s, dict) else ""
                            st.markdown(f"- {nm}" + (f"（{tk}）" if tk else ""))
                    else:
                        st.caption("(本次未列出)")
                with scol2:
                    st.markdown("**🇹🇼 台股代表**")
                    if tw_stocks:
                        for s in tw_stocks:
                            nm = s.get("name", "") if isinstance(s, dict) else str(s)
                            tk = s.get("ticker", "") if isinstance(s, dict) else ""
                            st.markdown(f"- {nm}" + (f"（{tk}）" if tk else ""))
                    else:
                        st.caption("(本次未列出)")

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

            _render_evidence_news(t.get("evidence_news", []))

    st.caption("⚠️ 趨勢評估由 AI 自動生成,非投資建議。新聞屬同步/落後訊號,請搭配資金與徵才等領先指標判讀。")

def sec_report() -> None:
    st.subheader("🌐 戰略報告")
    with st.expander("⚡ 即時重新抓取 / 產生戰略報告"):
        render_live_panel()
        if "live_news" in st.session_state and not st.session_state.get("live_report"):
            news = st.session_state["live_news"]
            if news:
                st.success(f"已抓到 {len(news)} 則真實外電,確認後再請 Gemini 分析:")
                if st.button("🧠 ② 用 Gemini 產生戰略分析 + 白話文", key="rpt_step2",
                             disabled=not ensure_gemini_key()):
                    with st.spinner("Gemini 分析中…"):
                        try:
                            generate_live_report(); st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"產生報告失敗:{exc}")
                render_news_cards(news)
            else:
                st.info("這次沒抓到新聞,稍後再試或調整關鍵字。")
    live = st.session_state.get("live_report")
    if live:
        st.success("⚡ 以下為剛剛即時產生的報告(尚未存檔)。")
        render_report(live)
        return
    multi = load_json(REPORTS_MULTI_PATH)
    multi_reports = (multi or {}).get("reports") or []
    if len(multi_reports) > 1:
        st.caption(f"📚 本日含 {len(multi_reports)} 個主題報告,可切換:")
        idx = st.selectbox(
            "選擇主題", range(len(multi_reports)),
            format_func=lambda i: multi_reports[i].get("topic", f"主題 {i + 1}"),
            key="macro_topic_pick",
        )
        render_report(multi_reports[idx]); return
    report = pick_report(REPORT_PATH, ARCHIVE_DIR)
    if report is None:
        st.info("尚無每日戰略報告。可用上方『即時抓取』取得。")
        return
    render_report(report)


def sec_trends() -> None:
    st.subheader("🔥 趨勢雷達 — 現在最紅的產業")
    with st.expander("⚡ 即時重新產生趨勢雷達"):
        render_trend_live_panel()
        if "live_trend_news" in st.session_state and not st.session_state.get("live_trends"):
            news = st.session_state["live_trend_news"]
            if news:
                st.success(f"已抓到 {len(news)} 則產業新聞:")
                if st.button("🧠 ② 用 Gemini 產生趨勢雷達", key="trd_step2",
                             disabled=not ensure_gemini_key()):
                    with st.spinner("Gemini 排名打分中…"):
                        try:
                            generate_live_trends(); st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"產生趨勢雷達失敗:{exc}")
                render_news_cards(news)
    live = st.session_state.get("live_trends")
    if live:
        st.success("⚡ 以下為剛剛即時產生的趨勢雷達。")
        render_trends(live); return
    history = load_trend_history(TRENDS_ARCHIVE_DIR)
    if history is not None and len(history.index) >= 2:
        st.caption("📈 產業熱度趨勢(歷史):哪條線持續往上,就是動能最強的產業。")
        st.line_chart(history)
    data = pick_report(TRENDS_PATH, TRENDS_ARCHIVE_DIR)
    if data is None:
        st.info("尚無趨勢雷達存檔。可用上方『即時產生』取得。")
        return
    render_trends(data)

def tool_focus() -> None:
    render_focus_panel()
    if st.session_state.get("live_focus"):
        render_focus(st.session_state["live_focus"]); return
    if "live_focus_news" in st.session_state:
        tr = st.session_state.get("live_focus_translation", {})
        news = st.session_state["live_focus_news"]
        en = tr.get("query_en", "")
        st.info(f"🔤 中文「{tr.get('query_zh', '')}」→ 英文檢索:**{en}**")
        if news:
            st.success(f"已抓到 {len(news)} 則全球新聞,確認後再請 Gemini 整理:")
            if st.button("🧠 ② 用 Gemini 整理(他說了什麼 + 衍伸產業 + 台美股)", key="focus_step2",
                         disabled=not ensure_gemini_key()):
                with st.spinner("Gemini 分析中…"):
                    try:
                        generate_live_focus(); st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"分析失敗:{exc}")
            render_news_cards(news)
        else:
            st.info("這次沒抓到相關新聞,換個關鍵字或稍後再試。")
        return
    doc = pick_report(FOCUS_PATH, FOCUS_ARCHIVE_DIR)
    focuses = (doc or {}).get("focuses") or []
    if not focuses:
        st.info("尚無每日全球人物追蹤存檔。可在上方輸入中文關鍵字即時產生,或設 FOCUS_TOPICS。")
        return
    if len(focuses) > 1:
        st.caption(f"📚 本日含 {len(focuses)} 個追蹤對象,可切換:")
        idx = st.selectbox(
            "選擇追蹤對象", range(len(focuses)),
            format_func=lambda i: focuses[i].get("query_zh", f"對象 {i + 1}"),
            key="focus_topic_pick",
        )
        render_focus(focuses[idx])
    else:
        render_focus(focuses[0])

def page_global() -> None:
    st.header("🌍 全球")
    ui_helpers.render_intro_banner(
        page_key="global",
        title="全球頁",
        steps=[
            "看 🛰️ **戰略報告**：AI 分析地緣、供應鏈、總經四個維度，每個維度末尾有「盲點/領先指標」值得特別注意。",
            "看 📡 **趨勢雷達**：評分最高的 3–5 個產業主題，對應的台股 ETF 欄是重點。",
            "用 🌍 **全球人物追蹤**：輸入中文名（如「川普」「葛林斯班」），系統自動抓英文新聞並分析對台美股的影響。",
        ],
    )
    payload = {
        "戰略報告": load_json(REPORT_PATH),
        "趨勢雷達": load_json(TRENDS_PATH),
        "全球人物追蹤": load_json(FOCUS_PATH),
    }
    render_market_digest("全球", {k: v for k, v in payload.items() if v})
    st.divider(); sec_report()
    st.divider(); sec_trends()
    st.divider()
    st.subheader("🌍 全球人物追蹤 — 中文輸入,自動翻英抓全球新聞,看牽動哪些台美股")
    tool_focus()
