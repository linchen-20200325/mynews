"""pages/ai_brain.py — AI 中央決策大腦儀表板。"""
from __future__ import annotations

import streamlit as st

import paths
import update_data
from app_core import (
    load_json,
    ensure_gemini_key,
)

def page_ai_brain() -> None:
    """🧠 中央決策儀表板 — 四路合流 Gemini 決策視覺化。"""
    import plotly.graph_objects as go

    st.header("🧠 AI 中央決策大腦")
    st.caption("每日台灣時間 06:00 自動更新；四路特徵（籌碼/總經/新聞/技術）合流後由 Gemini 一次決策。")

    decision = load_json(paths.LATEST_DECISION)

    if not decision:
        st.info("尚無決策資料。每日排程自動產生，或點下方按鈕立即觸發。")
        if st.button("🚀 立即產生決策", disabled=not ensure_gemini_key()):
            with st.spinner("四路合流中，Gemini 分析中…"):
                try:
                    import feature_aligner as _fa
                    feats = _fa.build_feature_json()
                    dec = update_data.get_master_decision(_features=feats)
                    dec["features"] = feats
                    paths.LATEST_DECISION.write_text(
                        __import__("json").dumps(dec, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"決策產生失敗：{exc}")
        return

    # ── 訊號標頭 ─────────────────────────────────────────────────────────
    signal  = decision.get("action_signal", "HOLD")
    score   = int(decision.get("confidence_score") or 0)
    regime  = decision.get("market_regime", "—")
    dec_date = decision.get("date", "—")

    _sig_icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(signal, "⚪")
    _sig_delta = {"BUY": "看多", "SELL": "看空", "HOLD": "觀望"}.get(signal, "")

    c1, c2, c3 = st.columns([2, 1, 1])
    c1.metric("市場狀態", regime)
    c2.metric(f"{_sig_icon} 操作訊號", signal, _sig_delta)
    c3.metric("信心分數", f"{score} / 100")
    st.caption(f"決策日期：{dec_date}")
    st.divider()

    # ── 四路權重長條圖 ────────────────────────────────────────────────────
    weights = decision.get("decision_weights") or {}
    if weights:
        st.subheader("📊 四路決策權重")
        _labels = {"chip": "籌碼", "macro": "總經", "news": "新聞", "tech": "技術"}
        _colors = ["#4da6ff", "#ff6666", "#44ff88", "#ffaa00"]
        fig_w = go.Figure(go.Bar(
            x=[_labels.get(k, k) for k in weights],
            y=[round(v * 100, 1) for v in weights.values()],
            marker_color=_colors,
            text=[f"{v * 100:.0f}%" for v in weights.values()],
            textposition="outside",
        ))
        fig_w.update_layout(
            plot_bgcolor="#0d1117", paper_bgcolor="#0d1117",
            font_color="#cccccc", height=220,
            yaxis=dict(range=[0, 100], title="權重 (%)"),
            margin=dict(t=10, b=10, l=40, r=10), showlegend=False,
        )
        st.plotly_chart(fig_w, use_container_width=True)

    # ── 核心驅動 + 風險提示 ───────────────────────────────────────────────
    col_d, col_r = st.columns([3, 2])
    with col_d:
        st.subheader("🎯 核心驅動因子")
        for i, driver in enumerate((decision.get("key_drivers") or []), 1):
            st.markdown(f"**{i}.** {driver}")
    with col_r:
        risk = (decision.get("risk_alert") or "").strip()
        if risk:
            st.warning(f"⚠️ **風險提示**\n\n{risk}")

    st.divider()

    # ── 四路特徵明細 ──────────────────────────────────────────────────────
    st.subheader("📋 四路特徵明細")
    feats = decision.get("features") or {}

    qa, qb, qc, qd = st.columns(4)

    with qa:
        st.markdown("**🌐 總經 / 美股**")
        m = feats.get("macro") or {}
        if m:
            for label, key in [("S&P 500", "sp500_chg_pct"), ("那斯達克", "nasdaq_chg_pct"),
                                ("SOX 半導", "sox_chg_pct"), ("DXY 美元", "dxy"),
                                ("10Y 殖利率", "us_10y_yield"), ("USD/TWD", "usd_twd")]:
                v = m.get(key)
                if v is not None:
                    pct = "%" if "chg" in key else ""
                    st.caption(f"{label}：**{v:+.2f}{pct}**" if "chg" in key else f"{label}：**{v:.2f}**")
        else:
            st.caption("（無資料）")

    with qb:
        st.markdown("**🏦 籌碼 / 法人**")
        c = feats.get("chip") or {}
        if c:
            for label, key in [("外資", "foreign_oku"), ("投信", "trust_oku"), ("合計", "total_oku")]:
                v = c.get(key)
                if v is not None:
                    color = "green" if v > 0 else "red" if v < 0 else "grey"
                    st.markdown(f":{color}[{label}：**{v:+.1f} 億**]")
            st.caption(f"日期：{c.get('date', '—')}")
        else:
            st.caption("（無資料）")

    with qc:
        st.markdown("**📰 新聞輿情**")
        n = feats.get("news") or {}
        if n:
            s = n.get("sentiment_score", 0)
            bar_len = int((s + 1) / 2 * 10)
            bar_str = "█" * bar_len + "░" * (10 - bar_len)
            st.caption(f"情感分數：**{s:+.2f}**")
            st.code(f"空 [{bar_str}] 多", language=None)
            st.caption(f"新聞數：{n.get('headline_count', 0)} 則")
            for hl in (n.get("top_headlines") or [])[:3]:
                st.caption(f"• {hl[:35]}…" if len(hl) > 35 else f"• {hl}")
        else:
            st.caption("（無資料）")

    with qd:
        st.markdown("**📈 技術面 / 股票**")
        t = feats.get("tech") or {}
        if t:
            total = t.get("stock_count", 0)
            bull  = t.get("bull_count", 0)
            bear  = t.get("bear_count", 0)
            if total:
                st.caption(f"觀察股票：{total} 檔")
                st.caption(f"看多：{bull} 檔（{t.get('bull_ratio', 0):.0%}）")
                st.caption(f"看空：{bear} 檔（{t.get('bear_ratio', 0):.0%}）")
            st.caption(f"報告日：{t.get('report_date', '—')}")
        else:
            st.caption("（無資料）")

    st.divider()
    st.caption(decision.get("disclaimer", "此分析僅供研究參考，非投資建議。"))
