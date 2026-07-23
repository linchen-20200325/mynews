"""app.py — 空頭回撤 vs 復原 分析看板（Streamlit 入口）。

本地執行：streamlit run app.py
雲端部署：Streamlit Cloud → Main file path 指到 drawdown_app/app.py（見 README）。

資料來源採防呆多路回退：上傳 CSV → yfinance 線上抓取 → 本地 data/<ticker>.csv 備援。
沙箱／離線或代理封鎖 yfinance 時，放一份真實 CSV 到 data/ 即可完整驗證（含 r≈0.85）。
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

# 確保無論啟動器與 CWD（streamlit run / AppTest / python -m）都能 import 同層模組。
# 正式 `streamlit run` 會自動把主腳本目錄加進 sys.path，此處明確補上以杜絕邊角失敗。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drawdown_core as dc  # noqa: E402 — 需在 sys.path 補齊後才 import 同層模組

# ── 雙語標籤（單一真相：欄鍵 → (中, 英)）───────────────────────────────────
LABELS: dict[str, tuple[str, str]] = {
    "peak_date": ("峰頂日", "Peak date"),
    "peak_price": ("峰頂價", "Peak"),
    "trough_date": ("谷底日", "Trough date"),
    "trough_price": ("谷底價", "Trough"),
    "recovery_date": ("復原日", "Recovery date"),
    "recovery_price": ("復原價", "Recovery"),
    "drawdown": ("回撤", "Drawdown"),
    "required_gain": ("收復所需漲幅", "Required gain"),
    "decline_cal_days": ("下跌(日曆天)", "Decline (cal.)"),
    "decline_tdays": ("下跌(交易天)", "Decline (trad.)"),
    "recovery_cal_days": ("復原(日曆天)", "Recovery (cal.)"),
    "recovery_tdays": ("復原(交易天)", "Recovery (trad.)"),
    "roundtrip_cal_days": ("來回(日曆天)", "Round-trip (cal.)"),
    "roundtrip_tdays": ("來回(交易天)", "Round-trip (trad.)"),
    "implied_recovery_cagr": ("隱含復原CAGR", "Implied recovery CAGR"),
    "recovered": ("已復原", "Recovered"),
}

# 相關分析可選的數值變數（欄鍵 → 雙語）
NUMERIC_VARS = [
    "drawdown", "required_gain",
    "decline_tdays", "recovery_tdays", "roundtrip_tdays",
    "decline_cal_days", "recovery_cal_days", "roundtrip_cal_days",
    "implied_recovery_cagr",
]


def _bi(key: str) -> str:
    """欄鍵 → 「中 / 英」雙語標籤。"""
    zh, en = LABELS.get(key, (key, key))
    return f"{zh} / {en}"


# ── 資料載入（防呆多路回退）────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _download_yfinance(ticker: str, start: str, end: str) -> pd.DataFrame:
    """線上抓取（yfinance）。快取 1 小時。回傳日期索引 + 'Close' 欄；失敗拋例外由呼叫端接。"""
    import yfinance as yf

    raw = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
    if raw is None or raw.empty:
        raise ValueError("yfinance 回傳空資料")
    # yfinance 有時回多層欄索引（即使單一 ticker）→ 攤平取第一層
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    if "Close" not in raw.columns:
        raise ValueError(f"抓取結果缺 Close 欄；現有：{list(raw.columns)}")
    out = raw[["Close"]].copy()
    out.index = pd.to_datetime(out.index)
    return out


def _parse_csv(data: bytes | str, name: str = "CSV") -> pd.DataFrame:
    """把 CSV bytes/str 解析成日期索引 + 'Close' 欄；容錯欄名大小寫與日期欄。"""
    buf = io.BytesIO(data) if isinstance(data, bytes) else io.StringIO(data)
    df = pd.read_csv(buf)
    lower = {c.lower(): c for c in df.columns}
    date_col = next((lower[k] for k in ("date", "datetime", "日期") if k in lower), None)
    close_col = next((lower[k] for k in ("close", "adj close", "adj_close", "收盤", "收盤價") if k in lower), None)
    if date_col is None or close_col is None:
        raise ValueError(f"{name} 需含日期欄與收盤欄；現有欄位：{list(df.columns)}")
    out = pd.DataFrame({"Close": pd.to_numeric(df[close_col], errors="coerce")})
    out.index = pd.to_datetime(df[date_col], errors="coerce")
    out = out[out.index.notna()].dropna(subset=["Close"]).sort_index()
    if out.empty:
        raise ValueError(f"{name} 解析後無有效列（日期／收盤皆需可解析）")
    return out


def load_prices(
    source: str, ticker: str, start: str, end: str, uploaded, data_dir,
) -> tuple[pd.DataFrame | None, list[str]]:
    """依來源載入價格，回傳 (df, 狀態訊息)。df 為 None 表全部路徑失敗。

    回退順序：① 上傳 CSV（若有）② yfinance 線上 ③ 本地 data/<ticker>.csv 備援。
    每一步都記錄狀態訊息，UI 透明呈現「真的有在退」。
    """
    msgs: list[str] = []

    # ① 使用者上傳（最高優先，離線可用）
    if uploaded is not None:
        try:
            df = _parse_csv(uploaded.getvalue(), uploaded.name)
            msgs.append(f"✅ 使用上傳檔 {uploaded.name}（{len(df)} 列）")
            return df, msgs
        except Exception as exc:  # noqa: BLE001 — 上傳失敗續走下一路
            msgs.append(f"⚠️ 上傳檔解析失敗：{exc}")

    # ② 線上（yfinance）；此環境代理可能封鎖，失敗即退備援
    if source in ("線上 yfinance / Online", "自動 / Auto"):
        try:
            df = _download_yfinance(ticker, start, end)
            msgs.append(f"✅ yfinance 抓取 {ticker}（{len(df)} 列，{start}→{end}）")
            return df, msgs
        except Exception as exc:  # noqa: BLE001 — 線上失敗 → 退本地備援
            msgs.append(f"⚠️ yfinance 抓取失敗：{exc} → 改試本地備援")

    # ③ 本地備援 data/<ticker>.csv
    fixture = Path(data_dir) / f"{ticker}.csv"
    if fixture.exists():
        try:
            df = _parse_csv(fixture.read_bytes(), fixture.name)
            df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
            if df.empty:
                msgs.append(f"⚠️ 本地 {fixture.name} 在 {start}→{end} 無資料")
            else:
                msgs.append(f"✅ 使用本地備援 {fixture.name}（{len(df)} 列）")
                return df, msgs
        except Exception as exc:  # noqa: BLE001
            msgs.append(f"⚠️ 本地備援解析失敗：{exc}")
    else:
        msgs.append(f"ℹ️ 無本地備援 {fixture.name}（可放真實 CSV 到 data/ 供離線／驗證）")

    msgs.append("❌ 所有資料來源皆失敗——請上傳 CSV 或改用有網路的環境。")
    return None, msgs


# ── 繪圖（全 Plotly）──────────────────────────────────────────────────────

def _fmt_pct(v: float | None) -> str:
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v * 100:.1f}%"


def fig_underwater(series: pd.DataFrame) -> go.Figure:
    """水下曲線：收盤 vs 歷史新高（上），回撤填色（下）。"""
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=series.index, y=series["close"], name="收盤 / Close",
                             line=dict(color="#2c3e50", width=1.3)))
    fig.add_trace(go.Scatter(x=series.index, y=series["ath"], name="歷史新高 / ATH",
                             line=dict(color="#27ae60", width=1, dash="dot")))
    fig.add_trace(go.Scatter(
        x=series.index, y=series["drawdown"] * 100.0, name="回撤 % / Drawdown",
        yaxis="y2", fill="tozeroy", line=dict(color="#c0392b", width=0.8),
        fillcolor="rgba(192,57,43,0.18)"))
    fig.update_layout(
        height=520, hovermode="x unified", legend=dict(orientation="h", y=1.06),
        margin=dict(l=10, r=10, t=30, b=10),
        yaxis=dict(title="價格 / Price"),
        yaxis2=dict(title="回撤 % / DD", overlaying="y", side="right",
                    range=[min(series["drawdown"].min() * 100 * 1.1, -1), 5]))
    return fig


def fig_scatter(xv: np.ndarray, yv: np.ndarray, rep: dict, x_key: str, y_key: str) -> go.Figure:
    """跌幅 vs 復原 散點 + OLS 迴歸線。"""
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xv, y=yv, mode="markers", name="事件 / Episodes",
                             marker=dict(size=10, color="#c0392b", opacity=0.75,
                                         line=dict(width=1, color="#7b241c"))))
    if rep.get("ols_slope") is not None and len(xv) >= 2:
        xs = np.linspace(float(np.min(xv)), float(np.max(xv)), 100)
        ys = rep["ols_intercept"] + rep["ols_slope"] * xs
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name="OLS",
                                 line=dict(color="#2c3e50", width=2, dash="dash")))
    fig.update_layout(height=480, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_title=_bi(x_key), yaxis_title=_bi(y_key),
                      legend=dict(orientation="h", y=1.08))
    return fig


def fig_recovery_hist(rec_tdays: np.ndarray) -> go.Figure:
    """復原時間（交易天）分布直方圖。"""
    fig = go.Figure(go.Histogram(x=rec_tdays, marker_color="#2980b9", nbinsx=20))
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_title="復原交易天 / Recovery trading days",
                      yaxis_title="事件數 / Count", bargap=0.05)
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
        st.error(
            f"⚠️ 偵測到 pyarrow {pyarrow.__version__}(≥25) —— requirements.txt 已釘 <25，"
            "此版在 st.dataframe 重繪會 segfault，請確認部署環境的 pin 生效。")


def main() -> None:
    st.set_page_config(page_title="空頭回撤 vs 復原 | Drawdown vs Recovery",
                       page_icon="🐻", layout="wide")
    _pyarrow_guard()
    st.title("🐻 空頭回撤 vs 復原分析 / Bear-market Drawdown vs Recovery")
    st.caption("reclaim-prior-peak：收復前高才算復原 · 資料僅供研究，非投資建議 / Not investment advice")

    data_dir = Path(__file__).resolve().parent / "data"

    # ── 側邊欄：輸入 ──
    with st.sidebar:
        st.header("⚙️ 參數 / Parameters")
        ticker = st.text_input("代碼 / Ticker", value="^GSPC",
                               help="yfinance 代碼。^GSPC=S&P500、^TWII=台股加權、^IXIC=Nasdaq。")
        c1, c2 = st.columns(2)
        start = c1.date_input("起 / Start", value=date(1990, 1, 1),
                              min_value=date(1900, 1, 1), max_value=date.today())
        end = c2.date_input("迄 / End", value=date.today(),
                            min_value=date(1900, 1, 1), max_value=date.today())
        source = st.radio("資料來源 / Source",
                          ["自動 / Auto", "線上 yfinance / Online", "只用本地 / Local only"],
                          help="自動＝先線上、失敗退本地 data/<ticker>.csv。")
        uploaded = st.file_uploader("或上傳 CSV（含 Date, Close）", type=["csv"])
        thr_pct = st.slider("納入門檻 |回撤| ≥ / Min drawdown", 5, 60, 10, step=1,
                            help="只收錄回撤深度 ≥ 此值的事件。10%＝修正級、20%＝熊市級。")
        min_drawdown = -thr_pct / 100.0
        ci = st.select_slider("信賴水準 / CI", options=[0.80, 0.90, 0.95, 0.99], value=0.95)

    if start >= end:
        st.error("起始日需早於結束日 / Start must be before End。")
        st.stop()

    df, msgs = load_prices(source, ticker.strip(), start.isoformat(), end.isoformat(),
                           uploaded, data_dir)
    for m in msgs:
        (st.error if m.startswith("❌") else st.warning if m.startswith("⚠️")
         else st.info if m.startswith("ℹ️") else st.success)(m)
    if df is None or df.empty:
        st.stop()

    # ── 核心計算 ──
    series = dc.compute_drawdown_series(df, "Close")
    episodes = dc.find_episodes(df, "Close", min_drawdown=min_drawdown)
    frame = dc.episodes_to_frame(episodes)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("資料列數 / Rows", f"{len(series):,}")
    k2.metric("事件數 / Episodes", f"{len(episodes)}")
    k3.metric("最深回撤 / Max DD", _fmt_pct(series["drawdown"].min()))
    n_recovered = int(frame["recovered"].sum()) if not frame.empty else 0
    k4.metric("已復原 / Recovered", f"{n_recovered}/{len(episodes)}")

    t1, t2, t3, t4, t5 = st.tabs([
        "📉 水下曲線 / Underwater", "🐻 事件表 / Episodes",
        "🔗 跌幅 vs 復原 / DD vs Recovery", "⏱️ 復原時間 / Recovery time",
        "🧪 資料與方法 / Data & Method"])

    # Tab 1 — 水下曲線
    with t1:
        st.plotly_chart(fig_underwater(series), width="stretch")
        st.caption("綠虛線＝歷史新高(ATH)；紅色填色＝距前高回撤%。收盤跌破 ATH 即進入水下。")

    # Tab 2 — 事件表
    with t2:
        if frame.empty:
            st.info(f"門檻 |回撤| ≥ {thr_pct}% 下無事件；可調低門檻。")
        else:
            show = frame.copy()
            for c in ("drawdown", "required_gain", "implied_recovery_cagr"):
                show[c] = show[c].map(_fmt_pct)
            for c in ("peak_date", "trough_date", "recovery_date"):
                show[c] = pd.to_datetime(show[c]).dt.strftime("%Y-%m-%d").fillna("—")
            for c in ("peak_price", "trough_price", "recovery_price"):
                show[c] = show[c].map(lambda v: "—" if pd.isna(v) else f"{v:,.1f}")
            show = show.rename(columns={k: _bi(k) for k in show.columns})
            st.dataframe(show, width="stretch", hide_index=True)
            st.download_button("⬇️ 下載事件 CSV / Download", frame.to_csv(index=False),
                               file_name=f"{ticker}_episodes.csv", mime="text/csv")

    # Tab 3 — 跌幅 vs 復原（含相關/CI/OLS）
    with t3:
        rec = frame[frame["recovered"]] if not frame.empty else frame
        if len(rec) < 3:
            st.info("已復原事件 < 3 筆，無法穩健估計相關；請調低門檻或拉長期間。")
        else:
            cc1, cc2 = st.columns(2)
            x_key = cc1.selectbox("X 軸 / X", NUMERIC_VARS, index=NUMERIC_VARS.index("required_gain"),
                                  format_func=_bi)
            y_key = cc2.selectbox("Y 軸 / Y", NUMERIC_VARS, index=NUMERIC_VARS.index("recovery_tdays"),
                                  format_func=_bi)
            xv = np.abs(rec[x_key].to_numpy(float)) if x_key == "drawdown" else rec[x_key].to_numpy(float)
            yv = np.abs(rec[y_key].to_numpy(float)) if y_key == "drawdown" else rec[y_key].to_numpy(float)
            rep = dc.correlation_report(xv, yv, ci=ci)
            m1, m2, m3 = st.columns(3)
            r = rep["pearson_r"]
            lo, hi = rep["pearson_ci"]
            m1.metric("Pearson r", "—" if r is None else f"{r:.3f}",
                      help=None if lo is None else f"{int(ci*100)}% CI: [{lo:.3f}, {hi:.3f}]")
            m2.metric("Spearman ρ", "—" if rep["spearman_rho"] is None else f"{rep['spearman_rho']:.3f}")
            m3.metric("OLS R²", "—" if rep["ols_r2"] is None else f"{rep['ols_r2']:.3f}")
            st.plotly_chart(fig_scatter(xv, yv, rep, x_key, y_key), width="stretch")
            if lo is not None:
                st.caption(f"Pearson r 的 {int(ci*100)}% 信賴區間 [{lo:.3f}, {hi:.3f}]"
                           f"（Fisher-z）；n={rep['n']}。drawdown 軸自動取絕對值以利判讀。")

    # Tab 4 — 復原時間分布
    with t4:
        rec = frame[frame["recovered"]] if not frame.empty else frame
        if rec.empty:
            st.info("尚無已復原事件。")
        else:
            rt = rec["recovery_tdays"].to_numpy(float)
            st.plotly_chart(fig_recovery_hist(rt), width="stretch")
            q1, q2, q3 = st.columns(3)
            q1.metric("中位復原(交易天) / Median", f"{np.median(rt):.0f}")
            q2.metric("最長復原(交易天) / Max", f"{np.max(rt):.0f}")
            q3.metric("平均復原(交易天) / Mean", f"{np.mean(rt):.0f}")

    # Tab 5 — 資料與方法
    with t5:
        st.subheader("資料來源狀態 / Data source status")
        for m in msgs:
            st.write(m)
        st.subheader("方法 / Methodology")
        st.markdown(
            "- **水下 / underwater**：收盤 < 歷史新高（cummax）。\n"
            "- **峰頂 / peak**：進入水下前的歷史新高（水下期間 ATH 不變）。\n"
            "- **谷底 / trough**：事件區間最低收盤。\n"
            "- **復原 / recovery**：谷底後首個收盤 ≥ 峰頂之日（收復前高）。\n"
            "- **required_gain** = peak/trough − 1；**drawdown** = trough/peak − 1。\n"
            "- **隱含復原 CAGR** = (peak/trough)^(252/復原交易天) − 1。\n"
            "- 統計：Pearson（Fisher-z 信賴區間）、Spearman、OLS，皆純 numpy。")
        st.caption("⚠️ drawdown_core.py 為【依規格重建】，請與原版 diff 確認指標語意一致。")


if __name__ == "__main__":
    main()
