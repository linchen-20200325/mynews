"""app.py — 熊市回撤 vs 修復分析看板（Streamlit 進入點）。

本地執行：streamlit run app.py
雲端部署：Streamlit Community Cloud → Main file path = drawdown_app/app.py（見 README）。

資料採防禦式多來源回退：上傳 CSV → GitHub raw CSV URL → yfinance 線上 → 本地
data/<ticker>.csv 備援；全程 @st.cache_data(ttl=3600)，任一失敗友善降級不崩潰。
"""

from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# 確保無論啟動器/CWD 都能 import 同層模組（正式 streamlit run 亦會自動補上）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drawdown_core as dc  # noqa: E402 — 需在 sys.path 補齊後才 import 同層模組

COMMON_TICKERS = ["^GSPC", "^TWII", "^IXIC", "^DJI"]

# 雙語欄位標籤（欄鍵 → 中 / 英）
LABELS: dict[str, str] = {
    "peak_date": "峰頂日 / Peak", "peak_price": "峰頂價 / Peak px",
    "trough_date": "谷底日 / Trough", "trough_price": "谷底價 / Trough px",
    "recovery_date": "修復日 / Recovery", "recovery_price": "修復價 / Rec px",
    "drawdown": "跌幅 / Drawdown", "required_gain": "收復漲幅 / Req. gain",
    "decline_cal_days": "下跌日曆天 / Decline(cal)", "decline_tdays": "下跌交易天 / Decline(td)",
    "recovery_cal_days": "修復日曆天 / Recovery(cal)", "recovery_tdays": "修復交易天 / Recovery(td)",
    "roundtrip_cal_days": "往返日曆天 / Round-trip(cal)", "roundtrip_tdays": "往返交易天 / Round-trip(td)",
    "implied_recovery_cagr": "隱含修復CAGR / Implied CAGR", "recovered": "已修復 / Recovered",
    "source": "資料來源 / Source", "year": "年 / Year",
}


def _fmt_pct(v) -> str:
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v * 100:.1f}%"


def _fmt_p(p) -> str:
    if p is None or (isinstance(p, float) and np.isnan(p)):
        return "—"
    return "<0.001" if p < 0.001 else f"{p:.3g}"


# ── 資料載入（防禦式多來源回退）────────────────────────────────────────────

def _coerce(df: pd.DataFrame, name: str, date_is_index: bool) -> pd.DataFrame:
    """把任意來源整成 [Close(, Low, High)] + DatetimeIndex；容錯欄名/去重/亂序/NaN。"""
    if date_is_index:
        idx = pd.to_datetime(df.index, errors="coerce")
        cols = {c.lower(): c for c in df.columns}
    else:
        cols = {c.lower(): c for c in df.columns}
        dkey = next((k for k in ("date", "datetime", "日期") if k in cols), None)
        if dkey is None:
            raise ValueError(f"{name} 缺日期欄（Date/Datetime/日期）")
        idx = pd.to_datetime(df[cols[dkey]], errors="coerce")

    out = pd.DataFrame(index=idx)
    ckey = next((k for k in ("close", "adj close", "adj_close", "收盤", "收盤價") if k in cols), None)
    if ckey is None:
        raise ValueError(f"{name} 缺收盤欄（Close 優先，其次 Adj Close）")
    out["Close"] = pd.to_numeric(df[cols[ckey]].to_numpy(), errors="coerce")
    for std, keys in (("Low", ("low", "最低")), ("High", ("high", "最高"))):
        kk = next((k for k in keys if k in cols), None)
        if kk is not None:
            out[std] = pd.to_numeric(df[cols[kk]].to_numpy(), errors="coerce")

    out = out[out.index.notna()].sort_index()
    out = out[~out.index.duplicated(keep="last")]        # 去重日期（留最後）
    out = out.dropna(subset=["Close"])                    # 丟無收盤列
    if len(out) < 30:
        raise ValueError(f"{name} 有效資料僅 {len(out)} 列，不足以分析（需 ≥30）")
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def _yf_download(ticker: str, start: str, end: str) -> pd.DataFrame:
    """線上抓取（yfinance，auto_adjust=False 以保留原始 Close 與 Low）。快取 1 小時。"""
    import yfinance as yf

    raw = yf.download(ticker, start=start, end=end, auto_adjust=False, progress=False)
    if raw is None or raw.empty:
        raise ValueError("yfinance 回傳空資料")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    return _coerce(raw, f"yfinance {ticker}", date_is_index=True)


@st.cache_data(ttl=3600, show_spinner=False)
def _csv_from_url(url: str) -> pd.DataFrame:
    """讀取使用者提供的 CSV URL（如 GitHub raw）。快取 1 小時。"""
    return _coerce(pd.read_csv(url), f"URL {url[:40]}…", date_is_index=False)


def load_prices(source, ticker, start, end, raw_url, uploaded, data_dir):
    """依來源載入價格，回 (df, source_label, msgs)。df=None 表全部失敗。

    優先序：① 上傳 CSV（明確意圖）② raw CSV URL ③ yfinance 線上 ④ 本地備援。
    每步記狀態訊息，UI 透明呈現實際走到哪一路。
    """
    msgs: list[str] = []

    if uploaded is not None:
        try:
            df = _coerce(pd.read_csv(io.BytesIO(uploaded.getvalue())), uploaded.name, date_is_index=False)
            msgs.append(f"✅ 使用上傳檔 {uploaded.name}（{len(df)} 列）")
            return df, f"upload:{uploaded.name}", msgs
        except Exception as exc:  # noqa: BLE001
            msgs.append(f"⚠️ 上傳檔解析失敗：{exc}")

    if raw_url.strip():
        try:
            df = _csv_from_url(raw_url.strip())
            msgs.append(f"✅ 使用 URL CSV（{len(df)} 列）")
            return df, "url", msgs
        except Exception as exc:  # noqa: BLE001
            msgs.append(f"⚠️ URL CSV 讀取失敗：{exc}")

    if source != "只用本地 / Local only":
        try:
            df = _yf_download(ticker, start, end)
            msgs.append(f"✅ yfinance 抓取 {ticker}（{len(df)} 列，{start}→{end}）")
            return df, f"yfinance:{ticker}", msgs
        except Exception as exc:  # noqa: BLE001
            msgs.append(f"⚠️ yfinance 失敗：{exc} → 改試本地備援")

    fixture = Path(data_dir) / f"{ticker}.csv"
    if fixture.exists():
        try:
            df = _coerce(pd.read_csv(fixture), fixture.name, date_is_index=False)
            df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
            if df.empty:
                msgs.append(f"⚠️ 本地 {fixture.name} 在 {start}→{end} 無資料")
            else:
                msgs.append(f"✅ 使用本地備援 {fixture.name}（{len(df)} 列）")
                return df, f"local:{fixture.name}", msgs
        except Exception as exc:  # noqa: BLE001
            msgs.append(f"⚠️ 本地備援解析失敗：{exc}")
    else:
        msgs.append(f"ℹ️ 無本地備援 {fixture.name}（可放真實 CSV 到 data/ 供離線／驗收）")

    msgs.append("❌ 所有資料來源皆失敗——請上傳 CSV、給 raw URL 或改用有網路的環境。")
    return None, "none", msgs


# ── 分析輔助 ──────────────────────────────────────────────────────────────

def _unit_cols(unit: str) -> tuple[str, str, str]:
    """天數單位 → (decline, recovery, roundtrip) 欄名。"""
    suf = "tdays" if unit.startswith("交易") else "cal_days"
    return f"decline_{suf}", f"recovery_{suf}", f"roundtrip_{suf}"


def stats_table(rec: pd.DataFrame, unit: str, ci: float) -> pd.DataFrame:
    """對 recovery/roundtrip/decline（vs |跌幅|%）各算 Pearson/Spearman/OLS，回摘要表。"""
    d_col, r_col, rt_col = _unit_cols(unit)
    x = (rec["drawdown"].abs() * 100.0).to_numpy(float)
    rows = []
    for label, col in [("修復 / Recovery", r_col), ("往返 / Round-trip", rt_col), ("下跌 / Decline", d_col)]:
        rep = dc.correlation_report(x, rec[col].to_numpy(float), ci=ci)
        lo, hi = rep["pearson_ci"]
        rows.append({
            "對象 / vs |跌幅|": label, "n": rep["n"],
            "Pearson r": None if rep["pearson_r"] is None else round(rep["pearson_r"], 3),
            f"{int(ci*100)}% CI": "—" if lo is None else f"[{lo:.2f}, {hi:.2f}]",
            "Pearson p": _fmt_p(rep["pearson_p"]),
            "Spearman ρ": None if rep["spearman_rho"] is None else round(rep["spearman_rho"], 3),
            "Spearman p": _fmt_p(rep["spearman_p"]),
            "OLS 斜率 / slope": None if rep["ols_slope"] is None else round(rep["ols_slope"], 2),
            "OLS R²": None if rep["ols_r2"] is None else round(rep["ols_r2"], 3),
            "斜率 p / slope p": _fmt_p(rep["ols_slope_p"]),
        })
    return pd.DataFrame(rows)


# ── 繪圖（全 Plotly）──────────────────────────────────────────────────────

def fig_vshape(series: pd.DataFrame, episodes: list, unit: str, max_days: int) -> go.Figure:
    """V 型疊圖：每次熊市一條 normalized（峰頂=100）折線，谷底標點。"""
    x_field = "tdays" if unit.startswith("交易") else "cal_days"
    fig = go.Figure()
    for i, ep in enumerate(episodes):
        path = dc.episode_path(series, ep)
        path = path[path[x_field] <= max_days]
        if path.empty:
            continue
        yr = pd.Timestamp(ep.peak_date).year
        hue = f"hsl({(i * 47) % 360},70%,45%)"
        fig.add_trace(go.Scatter(
            x=path[x_field], y=path["norm"], mode="lines", name=f"{yr} ({ep.drawdown*100:.0f}%)",
            line=dict(width=1.4, color=hue),
            customdata=np.stack([path.index.strftime("%Y-%m-%d"), (path["norm"] - 100)], axis=-1),
            hovertemplate="%{customdata[0]}<br>距峰頂 %{x} 天<br>距峰頂 %{customdata[1]:.1f}%<extra>" + str(yr) + "</extra>"))
        t = path[path["norm"] == path["norm"].min()].head(1)
        fig.add_trace(go.Scatter(x=t[x_field], y=t["norm"], mode="markers", showlegend=False,
                                 marker=dict(size=7, color=hue, symbol="circle-open", line=dict(width=2))))
    fig.add_hline(y=100, line=dict(color="#888", width=1, dash="dot"))
    fig.update_layout(height=560, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_title=f"距峰頂天數（{'交易天' if x_field=='tdays' else '日曆天'}）/ Days from peak",
                      yaxis_title="正規化（峰頂=100）/ Normalized", legend=dict(font=dict(size=10)))
    return fig


def fig_scatter(rec: pd.DataFrame, y_col: str, y_label: str, rep: dict) -> go.Figure:
    """深度 vs 時間散布 + OLS 線 + 每點年份標註。"""
    x = (rec["drawdown"].abs() * 100.0).to_numpy(float)
    y = rec[y_col].to_numpy(float)
    yrs = [str(pd.Timestamp(d).year) for d in rec["peak_date"]]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=y, mode="markers+text", text=yrs, textposition="top center",
                             textfont=dict(size=9), name="熊市 / Bears",
                             marker=dict(size=11, color="#c0392b", opacity=0.8, line=dict(width=1, color="#7b241c"))))
    if rep.get("ols_slope") is not None:
        xs = np.linspace(float(x.min()), float(x.max()), 100)
        fig.add_trace(go.Scatter(x=xs, y=rep["ols_intercept"] + rep["ols_slope"] * xs,
                                 mode="lines", name="OLS", line=dict(color="#2c3e50", width=2, dash="dash")))
    fig.update_layout(height=520, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_title="跌幅 |%| / Drawdown depth", yaxis_title=y_label,
                      legend=dict(orientation="h", y=1.08))
    return fig


def fig_cagr_bar(rec: pd.DataFrame) -> go.Figure:
    """修復速度長條：隱含修復 CAGR，排序 + 中位數水平線。"""
    d = rec.dropna(subset=["implied_recovery_cagr"]).copy()
    d["year"] = [pd.Timestamp(x).year for x in d["peak_date"]]
    d = d.sort_values("implied_recovery_cagr", ascending=False)
    vals = d["implied_recovery_cagr"].to_numpy(float) * 100.0
    fig = go.Figure(go.Bar(x=[str(y) for y in d["year"]], y=vals, marker_color="#27ae60",
                           text=[f"{v:.0f}%" for v in vals], textposition="outside"))
    med = float(np.median(vals))
    fig.add_hline(y=med, line=dict(color="#c0392b", width=2, dash="dash"),
                  annotation_text=f"中位數 / median {med:.0f}%", annotation_position="top right")
    fig.update_layout(height=460, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_title="熊市（峰頂年）/ Bear (peak year)",
                      yaxis_title="隱含修復 CAGR %", bargap=0.25)
    return fig


# ── 主程式 ────────────────────────────────────────────────────────────────

def _pyarrow_guard() -> None:
    """pin 漂版守門：pyarrow≥25 在 cp314 的 st.dataframe 重繪會 segfault，開站即告警。"""
    try:
        import pyarrow
        major = int(pyarrow.__version__.split(".")[0])
    except Exception:  # noqa: BLE001
        return
    if major >= 25:
        st.error(f"⚠️ 偵測到 pyarrow {pyarrow.__version__}(≥25) —— requirements 已釘 <25，"
                 "此版在 st.dataframe 重繪會 segfault，請確認部署環境 pin 生效。")


def main() -> None:
    st.set_page_config(page_title="熊市回撤 vs 修復 | Bear Drawdown vs Recovery",
                       page_icon="🐻", layout="wide")
    _pyarrow_guard()
    st.title("🐻 熊市回撤 vs 修復分析 / Bear-market Drawdown vs Recovery")
    st.caption("reclaim-prior-peak：收復前高才算修復 · 資料僅供研究，非投資建議 / Not investment advice")
    data_dir = Path(__file__).resolve().parent / "data"

    with st.sidebar:
        st.header("⚙️ 參數 / Parameters")
        pick = st.selectbox("指數 / Ticker", COMMON_TICKERS + ["自訂 / Custom"], index=0, key="ticker_pick")
        ticker = st.text_input("自訂代碼 / Custom ticker", value="^GSPC", key="ticker_custom").strip() \
            if pick.startswith("自訂") else pick
        c1, c2 = st.columns(2)
        start = c1.date_input("起始 / Start", value=date(1970, 1, 1),
                              min_value=date(1900, 1, 1), max_value=date.today())
        end = c2.date_input("結束 / End", value=date.today(),
                            min_value=date(1900, 1, 1), max_value=date.today())
        thr = st.slider("熊市門檻 |跌幅| ≥ / Bear threshold", 5, 60, 20, step=1)
        min_drawdown = -thr / 100.0
        basis = st.radio("峰頂基準 / Price basis", ["收盤 close", "盤中最低 low"], horizontal=True,
                         key="price_basis", help="close＝以收盤序列；low＝以盤中最低序列各自算一遍。")
        unit = st.radio("天數單位 / Day unit", ["交易天 / Trading", "日曆天 / Calendar"],
                        horizontal=True, key="day_unit")
        include_ongoing = st.checkbox("納入尚未修復 ongoing / Include ongoing", value=True)
        ci = st.select_slider("信賴水準 / CI", options=[0.80, 0.90, 0.95, 0.99], value=0.95)
        with st.expander("📥 備援資料來源 / Fallback data"):
            source = st.radio("線上來源 / Online", ["自動 / Auto", "只用本地 / Local only"], key="src_online")
            raw_url = st.text_input("GitHub raw CSV URL", value="",
                                    help="含 Date, Close(, Low) 的 CSV 直鏈；優先於 yfinance。")
            uploaded = st.file_uploader("或上傳 CSV / Upload", type=["csv"])

    if start >= end:
        st.error("起始日需早於結束日 / Start must be before End。")
        st.stop()

    df, src_label, msgs = load_prices(source, ticker, start.isoformat(), end.isoformat(),
                                      raw_url, uploaded, data_dir)
    for m in msgs:
        (st.error if m[0] == "❌" else st.warning if m[0] == "⚠"
         else st.info if m[0] == "ℹ" else st.success)(m)
    if df is None or df.empty:
        st.stop()

    # 峰頂基準 → 選欄；low 基準但無 Low 欄則降級
    price_col = "Close"
    if basis.startswith("盤中"):
        if "Low" in df.columns:
            price_col = "Low"
        else:
            st.warning("此資料源無 Low 欄 → 已降級以收盤 close 計算。")

    series = dc.compute_drawdown_series(df, price_col)
    episodes_all = dc.find_episodes(df, price_col, min_drawdown=min_drawdown)
    episodes = episodes_all if include_ongoing else [e for e in episodes_all if e.recovered]
    frame = dc.episodes_to_frame(episodes)
    if not frame.empty:
        frame = frame.assign(year=[pd.Timestamp(d).year for d in frame["peak_date"]], source=src_label)
    rec = frame[frame["recovered"]].copy() if not frame.empty else frame  # 已修復（相關分析用）
    d_col, r_col, rt_col = _unit_cols(unit)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("資料列 / Rows", f"{len(series):,}")
    k2.metric("熊市數 / Bears", f"{len(episodes)}")
    k3.metric("最深回撤 / Max DD", _fmt_pct(series["drawdown"].min()))
    k4.metric("已修復 / Recovered", f"{len(rec)}/{len(episodes)}")

    t1, t2, t3, t4, t5 = st.tabs([
        "① 熊市清單 / List", "② V 型疊圖 / V-shape", "③ 深度 vs 時間 / Depth vs Time",
        "④ 統計面板 / Stats", "⑤ 修復速度 / Recovery speed"])

    # ── Tab1 熊市清單 ──
    with t1:
        if frame.empty:
            st.info(f"門檻 |跌幅| ≥ {thr}% 下無熊市；可調低門檻。")
        else:
            show = frame.copy()
            for c in ("drawdown", "required_gain", "implied_recovery_cagr"):
                show[c] = show[c].map(_fmt_pct)
            for c in ("peak_date", "trough_date", "recovery_date"):
                show[c] = pd.to_datetime(show[c]).dt.strftime("%Y-%m-%d").fillna("—")
            for c in ("peak_price", "trough_price", "recovery_price"):
                show[c] = show[c].map(lambda v: "—" if pd.isna(v) else f"{v:,.1f}")
            show = show.rename(columns={k: LABELS.get(k, k) for k in show.columns})
            st.dataframe(show, width="stretch", hide_index=True)
            st.download_button("⬇️ 下載熊市 CSV / Download", frame.to_csv(index=False),
                               file_name=f"{ticker}_bears.csv", mime="text/csv")

    # ── Tab2 V 型疊圖 ──
    with t2:
        if not episodes:
            st.info("無事件可繪。")
        else:
            max_span = max((dc.episode_path(series, e)[
                "tdays" if unit.startswith("交易") else "cal_days"].max() for e in episodes), default=100)
            max_days = st.slider("x 軸範圍（距峰頂天數）/ X range", 20, int(max_span), int(max_span), step=10)
            st.plotly_chart(fig_vshape(series, episodes, unit, max_days), width="stretch")
            st.caption("每條線＝一次熊市（峰頂正規化為 100）；圈點＝谷底。虛線 100＝收復前高。")

    # ── Tab3 深度 vs 時間散布 ──
    with t3:
        if len(rec) < 3:
            st.info("已修復事件 < 3 筆，無法穩健估計相關；請調低門檻或拉長期間。")
        else:
            y_choice = st.radio("Y 軸 / Y", ["修復天數 / Recovery", "全程天數 / Round-trip"], horizontal=True)
            y_col = r_col if y_choice.startswith("修復") else rt_col
            x = (rec["drawdown"].abs() * 100.0).to_numpy(float)
            rep = dc.correlation_report(x, rec[y_col].to_numpy(float), ci=ci)
            m1, m2, m3, m4 = st.columns(4)
            lo, hi = rep["pearson_ci"]
            m1.metric("Pearson r", f"{rep['pearson_r']:.3f}",
                      help=None if lo is None else f"{int(ci*100)}% CI [{lo:.3f}, {hi:.3f}] · p={_fmt_p(rep['pearson_p'])}")
            m2.metric("Spearman ρ", f"{rep['spearman_rho']:.3f}", help=f"p={_fmt_p(rep['spearman_p'])}")
            m3.metric("OLS R²", "—" if rep["ols_r2"] is None else f"{rep['ols_r2']:.3f}")
            m4.metric("n", f"{rep['n']}")
            st.plotly_chart(fig_scatter(rec, y_col, LABELS[y_col], rep), width="stretch")
            if lo is not None:
                st.caption(f"Pearson r {int(ci*100)}% CI [{lo:.3f}, {hi:.3f}]（Fisher z）· "
                           f"p={_fmt_p(rep['pearson_p'])} · 每點標峰頂年份。")

    # ── Tab4 統計面板 + leave-one-out ──
    with t4:
        if len(rec) < 3:
            st.info("已修復事件 < 3 筆，統計不穩健。")
        else:
            st.subheader("全樣本 / Full sample")
            st.dataframe(stats_table(rec, unit, ci), width="stretch", hide_index=True)
            st.subheader("敏感度：Leave-one-out（排除任意熊市後重算）")
            opts = {f"{int(r.year)} ({r.drawdown*100:.0f}%)": i for i, (_, r) in enumerate(rec.iterrows())}
            drop = st.multiselect("排除 / Exclude", list(opts.keys()))
            if drop:
                keep = rec.drop(rec.index[[opts[d] for d in drop]])
                if len(keep) < 3:
                    st.warning("排除後樣本 < 3，無法重算。")
                else:
                    st.dataframe(stats_table(keep, unit, ci), width="stretch", hide_index=True)
                    st.caption(f"已排除 {len(drop)} 次熊市，剩 n={len(keep)}。")
            else:
                st.caption("未排除任何熊市；於上方選取以觀察單一事件對相關的影響。")

    # ── Tab5 修復速度長條 ──
    with t5:
        speed = rec.dropna(subset=["implied_recovery_cagr"]) if not rec.empty else rec
        if speed.empty:
            st.info("尚無已修復事件可計 CAGR。")
        else:
            st.plotly_chart(fig_cagr_bar(speed), width="stretch")
            vals = speed["implied_recovery_cagr"].to_numpy(float) * 100.0
            q1, q2, q3 = st.columns(3)
            q1.metric("最快 / Fastest", f"{np.max(vals):.0f}%")
            q2.metric("中位 / Median", f"{np.median(vals):.0f}%")
            q3.metric("最慢 / Slowest", f"{np.min(vals):.0f}%")

    with st.expander("🧪 資料來源狀態與方法 / Data status & method"):
        for m in msgs:
            st.write(m)
        st.markdown(
            "- **水下**＝收盤<歷史新高(cummax)；**峰頂**＝進入水下前 ATH；**谷底**＝區間最低；"
            "**修復**＝谷底後首次收盤≥峰頂。\n"
            "- `drawdown=trough/peak−1`、`required_gain=peak/trough−1`、`隱含CAGR=(peak/trough)^(252/修復交易天)−1`。\n"
            "- 統計：Pearson（Fisher-z CI + p）、Spearman（ρ + p）、OLS（斜率/截距/R²/斜率 p），scipy 提供 p 值。")
        st.caption("⚠️ drawdown_core.py 為【依規格重建】，核心定義未改；請與原版 diff 確認一致。")


if __name__ == "__main__":
    main()
