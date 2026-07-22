"""pages/tw.py — 台股頁:國際盤預警 + 法人籌碼 + 台股觀察 + 互動工具。"""
from __future__ import annotations

import time

import streamlit as st
import pandas as pd

import freshness
import update_data
import tz_utils
import etf_data
import season_chart
import reversal_signals
import ui_helpers
from app_core import (
    STALE_REPORT_DAYS,
    INTL_ALERT_PATH,
    INTL_ALERT_ARCHIVE_DIR,
    CHIP_PATH,
    CHIP_ARCHIVE_DIR,
    MARGIN_PATH,
    FUT_CHIP_PATH,
    STOCKS_PATH,
    STOCKS_ARCHIVE_DIR,
    REVERSAL_PATH,
    SIX_MONTH_SOURCE_CAPTION,
    ensure_gemini_key,
    fetch_live_news_cached,
    fetch_index_quotes_cached,
    render_key_hint,
    render_news_cards,
    pick_report,
    load_json,
    render_market_digest,
    _render_evidence_news,
    _render_stock_card_group,
    _render_trends_sunset,
    mention_caption,
)

def render_stock_live_panel() -> None:
    """台股觀察第一步:只抓台灣財經新聞(整理另由 Gemini 按鈕觸發)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時產生(免等每日排程)")
        st.caption(
            "從台灣財經新聞統計被提到最多次的台股標的,分利多/利空/觀望,"
            "並歸納未來趨勢與夕陽產業。流程:① 先抓財經新聞 → ② 看過後再按 Gemini 整理。"
        )
        if st.button("🔄 ① 立即抓取台灣財經新聞", use_container_width=True):
            with st.spinner("抓取台灣財經新聞中…"):
                try:
                    st.session_state["live_stock_news"] = fetch_live_news_cached("stock")
                    st.session_state.pop("live_stocks", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["live_stock_news"] = []
                    st.error(f"抓取失敗:{exc}")


def generate_live_stocks() -> None:
    """台股觀察第二步:對『已抓到的財經新聞』請 Gemini 整理台股標的。"""
    news = st.session_state.get("live_stock_news", [])
    today = tz_utils.taiwan_today()
    st.session_state["live_stocks"] = update_data.get_stock_picks(news, today)
    st.session_state.pop("live_stock_news", None)

def render_stocks(data: dict) -> None:
    st.metric("資料日期", data.get("report_date", "—"))
    note = freshness.stale_note(data.get("report_date"), STALE_REPORT_DAYS, "台股觀察")
    if note:
        st.warning(note)
    if data.get("summary"):
        st.info(data["summary"])
    st.caption("依新聞『被提及次數』排序;標的分利多/利空/觀望。⚠️ 僅為新聞整理,非投資建議。")
    st.caption(SIX_MONTH_SOURCE_CAPTION)

    stocks = data.get("stocks", [])
    if not stocks:
        st.info("本次未整理出台股標的。")
        return

    # 交叉參照:每檔個股被幾檔 ETF 持有(共用 etf_data 快取,與反查頁同一來源)
    etf_counts = etf_data.get_etf_count_map()

    # 總表(新聞提及次數 + ETF 持有檔數 + 首見/最近見報,多個訊號一起看)
    st.subheader("📋 台股標的總表(新聞提及 × ETF 持有 × 見報區間)")
    st.caption("被很多 ETF 持有 ＋ 新聞偏利多 = 相對更受關注。ETF 檔數來自 etf_holdings.json;首見/最近/則數由真實新聞統計。")
    st.dataframe(
        [
            {
                "標的": s.get("name", ""),
                "代號": s.get("ticker", ""),
                "產業": s.get("sector", ""),
                "則數": s.get("news_count", s.get("mention_count", 0)),
                "首見": s.get("first_seen", ""),
                "最近": s.get("last_seen", ""),
                "ETF持有": etf_counts.get(str(s.get("ticker", "")), 0),
                "傾向": s.get("sentiment", "") if s.get("news_count", s.get("mention_count", 0)) > 0 else "",
                "原因": s.get("reason", ""),
            }
            for s in stocks
        ],
        use_container_width=True,
        hide_index=True,
    )

    _render_stock_card_group(stocks, etf_counts)
    _render_trends_sunset(data)
    st.caption("⚠️ 本頁由 AI 自動整理新聞而成,可能有誤,僅供參考,非投資建議。")

def render_intl_alert_live_panel() -> None:
    """國際盤預警第一步:抓真實指數/期貨報價(免金鑰)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時產生(免等每日排程)")
        st.caption(
            "抓美股指數(隔夜領先)、美股期貨與台指期夜盤(盤前即時)的真實漲跌幅,"
            "偵測突然大跌;再由 Gemini 依新聞解讀利空原因與對台股影響。"
            "流程:① 先抓報價(免金鑰)→(看過後)② 按 Gemini 解讀。"
        )
        if st.button("🔄 ① 立即抓國際盤報價(免金鑰)", use_container_width=True):
            with st.spinner("抓國際盤報價中…"):
                try:
                    st.session_state["live_intl_quotes"] = fetch_index_quotes_cached()
                    st.session_state.pop("live_intl_alert", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state.pop("live_intl_quotes", None)
                    st.error(f"抓取失敗:{exc}")


def generate_live_intl_alert() -> None:
    """國際盤預警第二步:用『已抓報價』+ 新聞,請 Gemini 解讀利空與台股影響。"""
    quotes = st.session_state.get("live_intl_quotes")
    today = tz_utils.taiwan_today()
    st.session_state["live_intl_alert"] = update_data.build_intl_alert(today, quotes=quotes)
    st.session_state.pop("live_intl_quotes", None)


def render_index_quotes(qmap: dict) -> None:
    """以 st.metric(自動紅綠)分組呈現指數/期貨漲跌幅。"""
    if not qmap:
        st.info("本次未取得任何指數報價。")
        return
    groups: dict[str, list] = {}
    for sym, q in qmap.items():
        groups.setdefault(q.get("group", "其他"), []).append((sym, q))
    for group, items in groups.items():
        st.markdown(f"**{group}**")
        cols = st.columns(len(items))
        for col, (sym, q) in zip(cols, items):
            col.metric(
                label=q.get("name", sym),
                value=q.get("last", "—"),
                delta=f"{q.get('change_pct', 0):+.2f}%",
            )
            if group == "債匯":
                up = q.get("change_pct", 0) > 0
                if sym == "TWD=X":
                    # 新台幣:USD/TWD 走升=台幣貶值=外資賣股匯出提款的真實訊號
                    cap = "📈 台幣貶=外資匯出" if up else "台幣升=外資匯入"
                else:
                    # 殖利率/美元:上升=資金收緊(利空),非「大跌」
                    cap = "📈 走升=資金收緊" if up else "走弱=資金寬鬆"
            else:
                cap = q.get("lead_type", "")
                if q.get("is_drop"):
                    cap += " · ⚠️大跌"
            if cap:
                col.caption(cap)


ALERT_BADGE = {"警戒": ("🔴", "error"), "觀察": ("🟠", "warning"), "平靜": ("🟢", "success")}


def render_intl_alert(data: dict) -> None:
    level = data.get("alert_level", "—")
    emoji, kind = ALERT_BADGE.get(level, ("", "info"))
    banner = getattr(st, kind, st.info)
    summary = data.get("summary", "")
    banner(f"{emoji} 警示級別:{level}" + (f" — {summary}" if summary else ""))
    st.caption(
        f"報價時間:{data.get('as_of', '—')} · 大跌門檻 {data.get('threshold', '')}% · "
        "數字為真實市場報價(Yahoo Finance),非 AI 估算"
    )

    st.subheader("📊 國際盤即時報價(時間差定位)")
    st.caption("漲跌幅=最新 vs 前收;美股指數→隔夜領先台股,美股期貨/台指期夜盤→盤前即時。")
    if data.get("quotes_ok") is False:
        st.warning("⚠️ 本次未取得任何即時報價(來源/代理暫時不可用),大跌偵測不可用,以下僅新聞面研判。")
    render_index_quotes(data.get("quotes", {}))

    drops = data.get("drops", [])
    if drops:
        st.subheader(f"⚠️ 大跌預警({len(drops)} 項)")
        for d in drops:
            st.error(
                f"**{d.get('name', '')}** {d.get('change_pct', 0):+.2f}%"
                f"　·　{d.get('lead_type', '')}"
            )
    else:
        st.success("✅ 目前追蹤標的均未觸及大跌門檻。")

    interp = data.get("interpretation", [])
    if interp:
        st.subheader("🧭 利空原因解讀(依新聞)")
        for it in interp:
            with st.container(border=True):
                if it.get("market"):
                    st.markdown(f"**{it['market']}**")
                if it.get("cause"):
                    st.write(it["cause"])
                _render_evidence_news(it.get("evidence_news", []))
    elif not data.get("ai_ok", True):
        st.info("ℹ️ 利空原因解讀因 AI 配額/網路暫缺(原因待補);上方真實報價與大跌偵測不受影響。")

    us = data.get("us_view", {})
    if us:
        st.subheader("🇺🇸 對美股的看法")
        direction = us.get("direction", "中性")
        badge = {"偏多": "🟢", "偏空": "🔴", "中性": "⚪"}.get(direction, "")
        st.markdown(f"**研判方向:{badge} {direction}**")
        if us.get("reason"):
            st.write(us["reason"])
        focus = us.get("focus", [])
        if focus:
            st.markdown("**觀察焦點:** " + "、".join(str(s) for s in focus))

    imp = data.get("tw_impact", {})
    if imp:
        st.subheader("🇹🇼 對台股可能影響")
        direction = imp.get("direction", "中性")
        badge = {"偏多": "🟢", "偏空": "🔴", "中性": "⚪"}.get(direction, "")
        st.markdown(f"**研判方向:{badge} {direction}**")
        if imp.get("reason"):
            st.write(imp["reason"])
        sectors = imp.get("sectors", [])
        if sectors:
            st.markdown("**重點族群:** " + "、".join(str(s) for s in sectors))

    render_confluence(data)
    render_chip_events(data.get("upcoming_events", []))

    st.caption(
        "⚠️ 時間差僅為參考性連動,非必然因果;本頁由真實報價 + AI 新聞研判組成,僅供參考,非投資建議。"
    )


def render_confluence(intl: dict) -> None:
    """🔴 多重賣壓共振:美股大跌 + 四力≥2(讀 latest_chip / latest_margin,真實數據判定)。"""
    chip = load_json(CHIP_PATH)
    margin = load_json(MARGIN_PATH)
    fut_chip = load_json(FUT_CHIP_PATH)
    try:
        conf = update_data.detect_pressure_confluence(intl, chip, margin, fut_chip)
    except Exception:  # noqa: BLE001 — 偵測失敗不影響整頁
        return
    st.subheader("🔴 多重賣壓共振偵測")
    forces_txt = "、".join(f.get("detail", "") for f in conf.get("forces", [])) or "無"
    if conf.get("triggered"):
        st.error(f"⚠️ 已觸發!美股大跌 + 共振力量 {conf['count']}/4:{forces_txt}")
        st.caption("非單一利空,多股賣壓疊加 → 排程會自動推 LINE 預警。")
    else:
        gate = "✅" if conf.get("us_drops") else "❌"
        st.info(f"未觸發(美股大跌 {gate}、共振力量 {conf['count']}/4)。成立力量:{forces_txt}")
    st.caption("四力:外資提款(現貨賣超／台指期偏空／台幣貶值任一)、散戶斷頭(融資)、"
               "Fed 收緊(殖利率/美元)、配息賣壓。全為真實數據判定,非投資建議。")


def render_chip_events(events: list) -> None:
    """📅 可預測法人賣壓行事曆(純規則推算;ETF 除息檔數為真實資料)。"""
    if not events:
        return
    st.subheader("📅 可預測法人賣壓行事曆(預先知道的)")
    st.caption("以下為曆法/慣例可事先推算的籌碼事件;每日排程會在事件進入 3 個交易日窗口時推播 LINE 預告。")
    type_emoji = {"季底作帳": "🧾", "年底作帳": "🧾", "MSCI調整": "🌐",
                  "除權息旺季": "💰", "ETF除息潮": "📦"}
    for e in events:
        td = e.get("trading_days_until", 0)
        when = "今日" if td == 0 else (f"約 {td} 個交易日後" if td > 0 else "已發生")
        with st.container(border=True):
            st.markdown(
                f"{type_emoji.get(e.get('type', ''), '📌')} **{e.get('title', '')}**　"
                f"`{e.get('date', '')}`　·　{when}"
            )
            if e.get("detail"):
                st.caption(e["detail"])
            st.caption(f"來源:{e.get('source', '—')}")


def render_chip(data: dict) -> None:
    """📊 法人籌碼事後驗證:近 N 日三大法人買賣超(真實數字,單位:億元)。"""
    days = data.get("days", [])
    if not days:
        st.warning("尚無三大法人資料。")
        return
    st.caption(
        f"資料時間:{data.get('as_of', '—')} · 數字為證交所 BFI82U 真實買賣超(非 AI 估算),"
        "以下換算為億元(原始單位:元)。"
    )
    note = freshness.stale_note(data.get("as_of"), update_data.CHIP_STALE_DAYS, "法人籌碼")
    if note:
        st.warning(note)
    rows = []
    for d in days:  # days 由新到舊
        rows.append({
            "日期": d.get("date", ""),
            "外資": round(d.get("foreign", 0) / update_data.OKU, 1),
            "投信": round(d.get("trust", 0) / update_data.OKU, 1),
            "自營商": round(d.get("dealer", 0) / update_data.OKU, 1),
            "三大法人合計": round(d.get("total", 0) / update_data.OKU, 1),
        })
    df = pd.DataFrame(rows)

    latest = rows[0]
    c1, c2, c3, c4 = st.columns(4)
    ui_helpers.metric_tip(c1, "外資(最新)", f"{latest['外資']:+.0f} 億", tip_key="外資")
    ui_helpers.metric_tip(c2, "投信(最新)", f"{latest['投信']:+.0f} 億", tip_key="投信")
    ui_helpers.metric_tip(c3, "自營商(最新)", f"{latest['自營商']:+.0f} 億", tip_key="自營商")
    ui_helpers.metric_tip(c4, "三大法人合計", f"{latest['三大法人合計']:+.0f} 億", tip_key="三大法人")

    st.subheader("📈 近期買賣超趨勢(億元)")
    chart_df = df.set_index("日期")[["外資", "投信", "自營商"]].iloc[::-1]  # 還原成由舊到新
    st.line_chart(chart_df)
    ui_helpers.render_how_to_read(
        "Y軸 = 當日淨買進（億元）。正值（往上）= 買超，負值（往下）= 賣超。"
        "三色合計看三大法人合力方向：持續買超代表法人偏多，連續賣超需留意籌碼轉弱。"
        "（滑鼠移到指標名稱上可查看白話說明）"
    )

    st.subheader("📋 每日明細(億元)")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(
        "🟢 正=買超、🔴 負=賣超。外資=外資及陸資+外資自營商;自營商=自行買賣+避險。"
        "單日大幅賣超即你問的『機構賣壓』可在此事後驗證。非投資建議。"
    )

    # 欄位一律 .get + 型別檢查:缺鍵或值為 None(來源改版/單欄缺漏)只顯示「—」,
    # 不讓單欄壞資料把整個籌碼面板炸掉(故障半徑:整段 → 單欄)。
    def _fmt_num(value, fmt: str) -> str:
        return format(value, fmt) if isinstance(value, (int, float)) else "—"

    margin = load_json(MARGIN_PATH)
    if margin:
        st.subheader("💳 融資餘額(散戶槓桿/斷頭訊號)")
        m_today = margin.get("margin_today")
        m_chg = margin.get("margin_chg")
        m_pct = margin.get("margin_chg_pct")
        m1, m2 = st.columns(2)
        ui_helpers.metric_tip(
            m1, "融資餘額",
            (f"{m_today/update_data.OKU:.0f} 億"
             if isinstance(m_today, (int, float)) else "—"),
            delta=f"{m_pct:+.2f}%" if isinstance(m_pct, (int, float)) else None,
        )
        ui_helpers.metric_tip(
            m2, "單日增減(融資)",
            (f"{m_chg/update_data.OKU:+.0f} 億"
             if isinstance(m_chg, (int, float)) else "—"),
            tip_key="融資增減",
        )
        st.caption(f"資料:{margin.get('date', '—')}(證交所 MI_MARGN,真實)。"
                   "融資大減=去槓桿/斷頭賣壓,為共振偵測四力之一。")

    fut = load_json(FUT_CHIP_PATH)
    if fut and isinstance(fut.get("foreign_net_oi"), (int, float)):
        st.subheader("📐 外資台指期留倉(期貨部位偏多/偏空)")
        stance = fut.get("stance", "中性")
        badge = {"偏多": "🟢 偏多", "偏空": "🔴 偏空", "中性": "⚪ 中性"}.get(stance, stance)
        f1, f2, f3 = st.columns(3)
        ui_helpers.metric_tip(f1, "外資 期貨方向", badge,
                              delta=f"{fut['foreign_net_oi']:+,} 口",
                              tip_key="台指期")
        ui_helpers.metric_tip(f2, "投信 留倉淨額",
                              f"{_fmt_num(fut.get('trust_net_oi'), '+,')} 口",
                              tip_key="留倉")
        ui_helpers.metric_tip(f3, "自營 留倉淨額",
                              f"{_fmt_num(fut.get('dealer_net_oi'), '+,')} 口",
                              tip_key="留倉")
        st.caption(
            f"資料:{fut.get('date', '—')}(期交所「三大法人台指期」未平倉口數淨額,真實)。"
            "⚠️ 這是**前一交易日盤後**的『留倉(現在仍持有的部位)』,正=淨多偏多、負=淨空偏空;"
            "與上方現貨買賣超互補:現貨看當日流量、期貨看持有方向。非投資建議。"
        )

def render_stock_query_panel() -> None:
    """第一步:輸入個股 → Gemini 正規化中英名/代號/市場 → 抓該股新聞。"""
    with st.container(border=True):
        st.markdown("#### 🔎 輸入要健診的個股")
        st.caption(
            "可輸入中文名、英文名或代號(例:台積電 / 2330 / Nvidia / NVDA)。"
            "系統會自動判斷台股/美股、抓該股最近約 6 個月的中英文新聞,"
            "再請 Gemini 產出研究員報告風格健診:① 新聞相關性;② 股價與籌碼動向;③ 基本面與推升動能(題材);"
            "④ 護城河與競爭(龍頭/對手/技術門檻/產業上中下游供應鏈);⑤ 估值與風險 + 觀察指標 + 長期持有研判。"
            "(部分數字為 AI 估算、非即時,僅供參考)"
        )
        term = st.text_input(
            "個股", key="stockq_term_input", placeholder="台積電 / 2330 / Nvidia / NVDA …"
        )
        has_key = ensure_gemini_key()
        if st.button(
            "🔍 ① 辨識個股並抓新聞",
            use_container_width=True,
            disabled=not has_key,
            help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
        ):
            if not term.strip():
                st.warning("請先輸入個股名稱或代號。")
            else:
                with st.spinner("辨識個股並抓取相關新聞中…"):
                    try:
                        tr = update_data.translate_stock_query(term.strip())
                        news = update_data.fetch_stock_query_news(
                            tr.get("query_en", ""), tr.get("ticker", ""),
                            tr.get("aliases"), tr.get("query_zh", term.strip()),
                            tr.get("zh_aliases"),
                        )
                        st.session_state["live_stockq_translation"] = tr
                        st.session_state["live_stockq_news"] = news
                        st.session_state["live_stockq_term"] = term.strip()
                        st.session_state.pop("live_stockq", None)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"抓取失敗:{exc}")
        if not has_key:
            render_key_hint()


def generate_live_stock_query() -> None:
    """第二步:對『已抓到的該股新聞』請 Gemini 做健診分析。"""
    tr = st.session_state.get("live_stockq_translation", {})
    news = st.session_state.get("live_stockq_news", [])
    term = st.session_state.get("live_stockq_term", tr.get("query_zh", ""))
    today = tz_utils.taiwan_today()
    st.session_state["live_stockq"] = update_data.get_stock_query_analysis(
        tr.get("query_zh", term), tr.get("query_en", ""),
        tr.get("ticker", ""), tr.get("market", ""), news, today,
    )
    st.session_state.pop("live_stockq_news", None)


# ---------------------------------------------------------------------------
# 新聞策略(貼一則新聞 → 首席策略師 → 台股 ETF 進場/持有/出場決策;互動、不存檔)
# ---------------------------------------------------------------------------

def render_news_strategy_panel() -> None:
    """輸入框:貼新聞 → Gemini 產生四階段台股 ETF 策略。"""
    with st.container(border=True):
        st.markdown("#### 📰 貼上新聞 / 時事")
        st.caption(
            "貼一則新聞或時事敘述,Gemini 以「首席投資策略師」角度做深度因果鏈推導,"
            "轉化為台股 ETF 的進場/持有/出場實戰決策(四階段:因果鏈 → 三大陣營 → ETF 佈局 → 持有出場)。"
            "ETF 代號為 AI 整理,務必自行核對即時行情與溢價率;僅供參考,非投資建議。"
        )
        text = st.text_area(
            "新聞文本", key="newsetf_text_input", height=180,
            placeholder="例:美國商務部宣布對先進製程設備擴大出口管制,衝擊 AI 晶片供應鏈…",
        )
        has_key = ensure_gemini_key()
        if st.button(
            "🧠 產生 ETF 策略分析",
            use_container_width=True, disabled=not has_key,
            help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
        ):
            if len(text.strip()) < 15:
                st.warning("請貼上較完整的新聞內容(至少一兩句)。")
            else:
                with st.spinner("Gemini 策略分析中(約 15–40 秒)…"):
                    try:
                        st.session_state["live_newsetf"] = update_data.get_news_etf_strategy(
                            text.strip(), tz_utils.taiwan_today())
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"分析失敗:{exc}")
        if not has_key:
            render_key_hint()


def _render_camp(title: str, items: list, name_key: str) -> None:
    """渲染一個陣營(受害/受惠/外資回補)。"""
    st.markdown(f"**{title}**")
    if not items:
        st.caption("—(本則新聞此陣營著墨不明顯)")
        return
    for it in items:
        name = (it.get(name_key) or "").strip()
        reason = (it.get("reason") or "").strip()
        st.markdown(f"- **{name}**:{reason}" if name else f"- {reason}")


def _render_etf_list(title: str, items: list) -> None:
    """渲染一組 ETF 建議(進攻/防守)。"""
    st.markdown(f"**{title}**")
    if not items:
        st.caption("—")
        return
    for e in items:
        tk = (e.get("ticker") or "").strip()
        nm = (e.get("name") or "").strip()
        logic = (e.get("logic") or "").strip()
        head = f"{nm}（{tk}）" if tk else (nm or "(代號待確認)")
        st.markdown(f"- **{head}**:{logic}")


def render_news_strategy(data: dict) -> None:
    """渲染新聞策略四階段。"""
    if data.get("news_summary"):
        st.info(data["news_summary"])
    if data.get("category"):
        st.caption(f"事件類型:{data['category']}")
    st.caption("AI 依新聞推導,ETF 代號與數字請自行核對即時行情/溢價率;僅供參考,非投資建議。")

    p1 = data.get("phase1_causal", {})
    st.subheader("① 新聞本質與因果鏈推導")
    if p1.get("core_turn"):
        st.markdown(f"**核心轉折:** {p1['core_turn']}")
    if p1.get("first_order"):
        st.markdown(f"**第一層效應:** {p1['first_order']}")
    if p1.get("horizon_nature"):
        st.markdown(f"**性質研判:** {p1['horizon_nature']}")

    p2 = data.get("phase2_camps", {})
    st.subheader("② 台股供應鏈三大陣營")
    _render_camp("🔴 直接衝擊 / 利空受害", p2.get("victims", []), "industry")
    _render_camp("🟢 直接受惠 / 定價權", p2.get("beneficiaries", []), "industry")
    _render_camp("🔵 外資回補 / 長線外溢", p2.get("foreign_reflow", []), "sector")

    p3 = data.get("phase3_etf", {})
    st.subheader("③ 台股 ETF 實戰佈局")
    _render_etf_list("⚔️ 進攻 / 順勢追擊", p3.get("offense", []))
    _render_etf_list("🛡️ 防守 / 利空低接", p3.get("defense", []))
    if p3.get("safety_check"):
        st.warning(f"⚠️ 即時安全檢視:{p3['safety_check']}")

    p4 = data.get("phase4_playbook", {})
    st.subheader("④ 持有週期與出場劇本")
    if p4.get("holding_period"):
        st.markdown(f"**預計持有週期:** {p4['holding_period']}")
    sigs = p4.get("exit_signals", [])
    if sigs:
        st.markdown("**出場 / 獲利了結訊號:**")
        for s in sigs:
            st.markdown(f"- {s}")

    if data.get("data_notes"):
        st.caption(data["data_notes"])


def _render_stock_query_header(data: dict) -> None:
    head = data.get("query_zh", "—")
    ticker = data.get("ticker", "")
    col1, col2, col3 = st.columns(3)
    col1.metric("個股", head + (f"（{ticker}）" if ticker else ""))
    col2.metric("市場", data.get("market", "—"))
    col3.metric("英文名", data.get("query_en", "—"))
    if data.get("summary"):
        st.info(data["summary"])
    cap = mention_caption(data)
    if cap:
        st.caption(cap)
    if data.get("first_seen"):
        st.caption(
            f"🗓️ 相關新聞 {data['first_seen']} ～ {data.get('last_seen', '')}"
            f"，共 {data.get('news_count', 0)} 則(回溯約 6 個月,實際範圍受 RSS 限制)。"
        )
    st.caption("由 AI 依真實新聞整理,可能有誤,僅供參考,非投資建議。")


def _render_stock_query_relevance(data: dict) -> None:
    st.subheader("① 與目前新聞的直接相關性")
    level = data.get("relevance_level", "")
    level_emoji = {"高": "🟢 高", "中": "🟡 中", "低": "⚪ 低"}.get(level, level)
    if level_emoji:
        st.markdown(f"**相關度:{level_emoji}**")
    points = data.get("relevance_points", [])
    if points:
        for p in points:
            st.markdown(f"- {p}")
    else:
        st.caption("新聞中未見對本檔的直接著墨。")


def _render_stock_query_price_chip(data: dict) -> None:
    pc = data.get("price_chip") or {}
    if not any(pc.get(k) for k in ("price_action", "chip_flow", "technical")):
        return
    st.subheader("② 股價與籌碼動向")
    if pc.get("price_action"):
        st.markdown(f"**盤面/量能:** {pc['price_action']}")
    if pc.get("chip_flow"):
        st.markdown(f"**法人/籌碼:** {pc['chip_flow']}")
    if pc.get("technical"):
        st.markdown(f"**技術面:** {pc['technical']}")


def _render_stock_query_fundamentals(data: dict) -> None:
    st.subheader("③ 基本面與推升動能")
    if data.get("operating_performance"):
        st.markdown(f"**營運績效:** {data['operating_performance']}")
    catalysts = data.get("catalysts", [])
    if catalysts:
        st.markdown("**推升動能/題材:**")
        for cat in catalysts:
            if not isinstance(cat, dict):
                continue
            title = cat.get("title", "")
            detail = cat.get("detail", "")
            line = f"- **{title}**" + (f":{detail}" if detail else "")
            st.markdown(line)
    nature = data.get("rally_nature", "")
    nature_emoji = {
        "短期消息面": "⚡ 短期消息面",
        "基本面可持續": "🏗️ 基本面可持續",
        "資料不足判斷": "❓ 資料不足判斷",
    }.get(nature, nature)
    if nature_emoji:
        st.markdown(f"**上漲性質:** {nature_emoji}")
    if data.get("rally_reason"):
        st.write(data["rally_reason"])
    _render_evidence_news(data.get("evidence_news", []))

def _render_stock_query_moat(data: dict) -> None:
    st.subheader("④ 護城河與競爭")
    st.caption("本段含產業結構常識(非僅來自新聞),數字未必即時,僅供參考。")
    lead = data.get("is_leader", "")
    lead_emoji = {
        "龍頭": "👑 龍頭", "前段班": "🥈 前段班",
        "中後段": "📉 中後段", "資料不足": "❓ 資料不足",
    }.get(lead, lead)
    moat = data.get("moat_level", "")
    moat_emoji = {
        "高": "🟢 高", "中": "🟡 中", "低": "🔴 低", "資料不足": "❓ 資料不足",
    }.get(moat, moat)
    c1, c2 = st.columns(2)
    if lead_emoji:
        c1.metric("市場地位", lead_emoji)
    if moat_emoji:
        c2.metric("技術門檻(護城河)", moat_emoji)
    if data.get("leader_reason"):
        st.markdown(f"**地位依據:** {data['leader_reason']}")
    if data.get("moat_reason"):
        st.markdown(f"**護城河來源:** {data['moat_reason']}")
    competitors = data.get("competitors", [])
    if competitors:
        st.markdown("**主要競爭對手:**")
        for c in competitors:
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            tk = c.get("ticker", "")
            note = c.get("note", "")
            line = f"- {name}" + (f"（{tk}）" if tk else "")
            if note:
                line += f" — {note}"
            st.markdown(line)
    sc = data.get("supply_chain") or {}
    seg_labels = [("upstream", "上游"), ("midstream", "中游"), ("downstream", "下游")]
    if any(sc.get(k) for k, _ in seg_labels):
        st.markdown("**產業上中下游供應鏈:**")
        for key, label in seg_labels:
            rows = sc.get(key) or []
            if not rows:
                continue
            names = "、".join(
                (r.get("name", "") + (f"（{r.get('ticker','')}）" if r.get("ticker") else ""))
                + (f":{r.get('role','')}" if r.get("role") else "")
                for r in rows if isinstance(r, dict) and r.get("name")
            )
            if names:
                st.markdown(f"- **{label}**:{names}")


def _render_stock_query_valuation(data: dict) -> None:
    val = data.get("valuation") or {}
    risks = data.get("risks", [])
    watch = data.get("watch_points", [])
    if any(val.get(k) for k in ("level", "logic", "peer_note")) or risks or watch:
        st.subheader("⑤ 估值與風險")
        vlevel = val.get("level", "")
        vlevel_emoji = {
            "偏高": "🔴 偏高", "合理": "🟢 合理", "偏低": "🟡 偏低", "資料不足": "❓ 資料不足",
        }.get(vlevel, vlevel)
        if vlevel_emoji:
            st.markdown(f"**估值水位:** {vlevel_emoji}")
        if val.get("logic"):
            st.markdown(f"**估值邏輯:** {val['logic']}")
        if val.get("peer_note"):
            st.markdown(f"**同業估值區間:** {val['peer_note']}")
        if risks:
            st.markdown("**主要風險:**")
            for r in risks:
                st.markdown(f"- ⚠️ {r}")
        if watch:
            st.markdown("**後續觀察指標:**")
            st.markdown("　".join(f"`{w}`" for w in watch))
    if data.get("long_term_view"):
        st.success(f"**長期持有研判:** {data['long_term_view']}")
    if data.get("data_notes"):
        st.caption(f"📌 數字來源:{data['data_notes']}")
    st.caption("⚠️ 本頁由 AI 自動整理新聞而成,部分數字為 AI 估算、非即時,可能有誤,僅供參考,非投資建議。")


def render_stock_query(data: dict) -> None:
    _render_stock_query_header(data)
    _render_stock_query_relevance(data)
    _render_stock_query_price_chip(data)
    _render_stock_query_fundamentals(data)
    _render_stock_query_moat(data)
    _render_stock_query_valuation(data)

def sec_tw_stocks() -> None:
    st.subheader("📈 台股觀察 — 值得關注的台股標的")
    with st.expander("⚡ 即時重新抓取台股觀察"):
        render_stock_live_panel()
        if "live_stock_news" in st.session_state and not st.session_state.get("live_stocks"):
            news = st.session_state["live_stock_news"]
            if news:
                st.success(f"已抓到 {len(news)} 則財經新聞:")
                if st.button("🧠 ② 用 Gemini 整理台股標的", key="tws_step2",
                             disabled=not ensure_gemini_key()):
                    with st.spinner("Gemini 整理台股標的中…"):
                        try:
                            generate_live_stocks(); st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"整理台股標的失敗:{exc}")
                render_news_cards(news)
    data = st.session_state.get("live_stocks") or pick_report(STOCKS_PATH, STOCKS_ARCHIVE_DIR)
    if data is None:
        st.info("尚無台股觀察存檔。可用上方『即時產生』取得。")
        return
    render_stocks(data)

def sec_intl() -> None:
    st.subheader("🌏 國際盤預警(盤前) — 美股/台指期夜盤大跌的時間差訊號")
    with st.expander("⚡ 即時重新抓取國際盤"):
        render_intl_alert_live_panel()
        if "live_intl_quotes" in st.session_state and not st.session_state.get("live_intl_alert"):
            quotes_doc = st.session_state["live_intl_quotes"]
            st.caption(f"報價時間:{quotes_doc.get('as_of', '—')} · 真實市場報價(Yahoo Finance)")
            render_index_quotes(quotes_doc.get("quotes", {}))
            if st.button("🧠 ② 用 Gemini 解讀利空原因 + 對台股影響", key="intl_step2",
                         disabled=not ensure_gemini_key()):
                with st.spinner("Gemini 解讀中…"):
                    try:
                        generate_live_intl_alert(); st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"解讀失敗:{exc}")
    data = st.session_state.get("live_intl_alert") or pick_report(
        INTL_ALERT_PATH, INTL_ALERT_ARCHIVE_DIR)
    if data is None:
        st.info("尚無國際盤預警存檔。可用上方『即時產生』取得。")
        return
    render_intl_alert(data)


def sec_chip() -> None:
    st.subheader("📊 法人籌碼 — 三大法人買賣超(事後驗證真實賣壓)")
    ui_helpers.render_spec_card(
        name="三大法人買賣超 + 融資餘額",
        source="證交所 BFI82U（三大法人）＋ MI_MARGN（融資），真實數據非 AI 估算",
        freq="每個交易日盤後 17:30 更新（台灣時間）",
        bull="外資持續買超 ＋ 融資餘額低 → 法人主導上漲、散戶槓桿乾淨",
        bear="外資轉賣超 ＋ 融資餘額高 → 機構撤離 ＋ 散戶槓桿過重，斷頭風險上升",
        note="外資現貨可能賣超但期貨仍留多單（對沖策略），兩者互補看、不宜孤立判斷。",
    )
    data = pick_report(CHIP_PATH, CHIP_ARCHIVE_DIR)
    if data is None:
        st.info("尚無三大法人籌碼存檔。每日排程會自動更新。")
        return
    render_chip(data)

def tool_stock_query() -> None:
    render_stock_query_panel()
    if st.session_state.get("live_stockq"):
        render_stock_query(st.session_state["live_stockq"]); return
    if "live_stockq_news" in st.session_state:
        tr = st.session_state.get("live_stockq_translation", {})
        news = st.session_state["live_stockq_news"]
        ticker = tr.get("ticker", "")
        st.info(f"🔤 辨識為:**{tr.get('query_zh', '')}**"
                + (f"（{ticker}）" if ticker else "")
                + f"　·　{tr.get('market', '')}　·　英文:{tr.get('query_en', '')}")
        if news:
            st.success(f"已抓到 {len(news)} 則相關新聞,確認後再請 Gemini 健診:")
            if st.button("🧠 ② 用 Gemini 健診", key="sq_step2",
                         disabled=not ensure_gemini_key()):
                with st.spinner("Gemini 分析中…"):
                    try:
                        generate_live_stock_query(); st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"分析失敗:{exc}")
            render_news_cards(news)
        else:
            st.info("這次沒抓到相關新聞,換個名稱/代號或稍後再試。")


def tool_news_strategy() -> None:
    render_news_strategy_panel()
    if st.session_state.get("live_newsetf"):
        st.divider()
        render_news_strategy(st.session_state["live_newsetf"])

@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_2026_cached() -> dict:
    return season_chart.fetch_sp500_2026() or {}


# Yahoo 短暫故障時的重試冷卻(秒):失敗不佔 1 小時快取,但也不在每次 rerun 重打
_CYCLE_FAIL_COOLDOWN = 300


def _fetch_2026() -> dict:
    """成功結果快取 1 小時;失敗清出快取(不讓空結果佔 1 小時),改記 session 冷卻 5 分鐘。"""
    failed_at = st.session_state.get("cycle_fetch_failed_at", 0.0)
    if time.time() - failed_at < _CYCLE_FAIL_COOLDOWN:
        return {}
    data = _fetch_2026_cached()
    if data:
        st.session_state.pop("cycle_fetch_failed_at", None)
    else:
        _fetch_2026_cached.clear()
        st.session_state["cycle_fetch_failed_at"] = time.time()
    return data


def _tool_cycle_chart() -> None:
    import matplotlib.pyplot as plt
    actual = _fetch_2026() or None
    tab_chart, tab_data = st.tabs(["📊 圖表", "📋 診斷資料"])

    with tab_chart:
        fig = season_chart.build_cycle_figure(actual)
        st.pyplot(fig, use_container_width=True)
        plt.close(fig)
        if actual:
            st.caption(f"2026 實際資料：{sorted(actual.keys())} 月份已更新（每小時自動刷新）")
        else:
            st.caption("2026 實際走勢暫無法取得，僅顯示歷史均線。")

    with tab_data:
        d = season_chart.get_cycle_data(actual)
        def _fmt(v) -> str:
            try:
                return f"{float(v):+.2f}%" if v is not None else "—"
            except (TypeError, ValueError):
                return "—"
        cols: dict = {
            "月份":          d["month_labels"],
            "藍 全年均%":    [_fmt(v) for v in d["blue"]],
            "紅 第六年%":    [_fmt(v) for v in d["red"]],
            "綠 共和黨6th%": [_fmt(v) for v in d["green"]],
            "黑 期中選舉%":  [_fmt(v) for v in d["black"]],
        }
        if d["orange"]:
            cols["橘 2026實際%"] = [_fmt(v) for v in d["orange"]]
        st.dataframe(pd.DataFrame(cols), use_container_width=True, hide_index=True)


_SIGNAL_FN  = {"絕對買進": st.success, "絕對賣出": st.error}
_SIGNAL_ICO = {"絕對買進": "🟢", "絕對賣出": "🔴", "觀望續抱": "⚪"}
_DIR_BADGE  = {"bad": "🔴 轉壞", "good": "🟢 好轉", None: "⚪ 觀望"}
_IND_LABELS = {
    "ma60":    "📐 季線(60MA)慣性翻轉",
    "chip":    "💰 籌碼板塊挪移",
    "semicon": "🔬 半導體龍頭週K",
}


def _render_reversal_result(result: dict) -> None:
    """單一標的翻轉偵測結果渲染（排程存檔 & 即時偵測共用）。"""
    signal     = result.get("signal", "觀望續抱")
    confidence = result.get("confidence", 0)
    sym        = result.get("symbol", "—")
    sig_fn = _SIGNAL_FN.get(signal, st.info)
    sig_fn(
        f"{_SIGNAL_ICO.get(signal, '⚪')} **{signal}**"
        f"　·　{confidence}/3 指標共振　·　{sym}"
    )
    if result.get("error"):
        st.warning(f"偵測異常：{result['error']}")
        return
    for key, label in _IND_LABELS.items():
        sig = result.get("indicators", {}).get(key, {})
        triggered = sig.get("triggered", False)
        direction = sig.get("direction")
        detail    = sig.get("detail", "—")
        mock_tag  = "　🧪 模擬值(不計入共振)" if sig.get("is_mock") else ""
        badge_fn  = st.error if direction == "bad" else (
                    st.success if direction == "good" else st.info)
        with st.expander(
            f"{label}　{'✅ 觸發' if triggered else '❌ 未觸發'}"
            f"　{_DIR_BADGE.get(direction, '—')}{mock_tag}",
            expanded=triggered,
        ):
            badge_fn(detail) if triggered else st.info(detail)
            if key == "semicon" and sig.get("sub"):
                for sym_k, sub in sig["sub"].items():
                    sub_ico = {"bad": "🔴", "good": "🟢"}.get(sub.get("direction"), "⚪")
                    st.caption(f"{sub_ico} **{sym_k}**：{sub.get('detail', '—')}")


def sec_reversal() -> None:
    """唯讀面板：顯示每日排程產出的翻轉偵測存檔（latest_reversal.json）。"""
    st.subheader("🔭 中線翻轉偵測 — 排程大盤共振訊號（每日 06:00 自動更新）")
    doc = load_json(REVERSAL_PATH)
    if doc is None:
        st.info("尚無排程翻轉偵測存檔（每日 06:00 自動產生）。可用下方互動工具即時偵測。")
        return
    st.caption(
        f"資料日期：{doc.get('report_date', '—')}　·　"
        f"更新時間：{doc.get('as_of', '—')}　·　"
        "60MA + 半導體週K 為真實資料；籌碼歷史序列仍為模擬值 — **已排除於共振計算**"
        "（訊號僅由兩個真實指標決定；待接通期交所真實 API 後自動納入）"
    )
    for result in doc.get("signals", []):
        with st.container(border=True):
            _render_reversal_result(result)


@st.cache_data(ttl=3600, show_spinner=False)
def _detect_reversal_cached(symbol: str, is_market: bool) -> dict:
    """以 (symbol, is_market) 為 key 快取翻轉偵測結果，每小時更新一次。"""
    return reversal_signals.detect_trend_reversal(symbol, is_market=is_market)


def tool_reversal_detector() -> None:
    """互動工具：中線行情翻轉偵測（三大指標共振）。"""
    with st.container(border=True):
        st.markdown("#### 🔭 輸入標的")
        st.caption(
            "輸入 Yahoo Finance 代號。台股大盤:`^TWII`；美股費半:`^SOX`；"
            "台股個股:`2330.TW`；美股個股:`NVDA`。"
            "偵測約需 3~8 秒（抓 yfinance 週K）。"
        )
        col_sym, col_mode = st.columns([3, 2])
        symbol = col_sym.text_input(
            "代號", key="reversal_symbol",
            placeholder="^TWII / ^SOX / 2330.TW / NVDA …",
        )
        is_market = col_mode.radio(
            "模式", ["大盤指數", "個股"],
            key="reversal_mode", horizontal=True,
        ) == "大盤指數"

        if st.button("🔭 執行中線翻轉偵測", use_container_width=True,
                     disabled=not symbol.strip()):
            if not symbol.strip():
                st.warning("請先輸入代號。")
            else:
                st.session_state["reversal_sym_done"] = symbol.strip()
                st.session_state["reversal_mkt_done"] = is_market
                # 清舊快取讓新代號重跑
                _detect_reversal_cached.clear()

    sym_done = st.session_state.get("reversal_sym_done", "")
    mkt_done = st.session_state.get("reversal_mkt_done", True)
    if not sym_done:
        return

    with st.spinner(f"偵測 {sym_done} 中（抓日K+週K+籌碼）…"):
        result = _detect_reversal_cached(sym_done, mkt_done)

    _render_reversal_result(result)
    if not result.get("error"):
        mock_note = ("⚠️ 籌碼面目前為 mock 資料，**已排除於共振計算**"
                     "（接通期交所/集保所真實 API 後自動納入）。"
                     if result.get("chip_is_mock") else "")
        st.caption(
            f"{mock_note}三大指標均為程式自動運算，非 AI 估算；僅供中線參考，非投資建議。"
        )


# ── 4 大頁 ─────────────────────────────────────────────────────────────────
def page_tw() -> None:
    st.header("📊 台股")
    ui_helpers.render_intro_banner(
        page_key="tw",
        title="台股頁",
        steps=[
            "先看 🌏 **國際盤預警**：美股夜盤大跌時，台股隔日通常跟跌；平靜日可放心布局。",
            "再看 📊 **法人籌碼**：外資持續買超 = 主力看多；搭配融資餘額確認散戶槓桿是否過熱。",
            "最後看 📈 **台股觀察**：AI 整理當日新聞熱門標的，配合籌碼方向交叉確認。",
        ],
    )
    payload = {
        "國際盤預警": load_json(INTL_ALERT_PATH),
        "法人籌碼": load_json(CHIP_PATH),
        "台股觀察": load_json(STOCKS_PATH),
    }
    render_market_digest("台股", {k: v for k, v in payload.items() if v})
    st.divider(); sec_intl()
    st.divider(); sec_chip()
    st.divider(); sec_tw_stocks()
    st.divider(); sec_reversal()
    st.divider()
    st.markdown("### 🛠 互動工具")
    with st.expander("🩺 個股健診 — 輸入個股,看它跟新聞的相關性與上漲性質"):
        tool_stock_query()
    with st.expander("📰 新聞策略 — 貼一則新聞,轉化為台股 ETF 進出場決策"):
        tool_news_strategy()
    with st.expander("📅 總統任期週期 — 2026 走勢預測參考"):
        _tool_cycle_chart()
    with st.expander("🔭 中線翻轉偵測 — 台股/美股大盤與個股共振訊號"):
        tool_reversal_detector()
