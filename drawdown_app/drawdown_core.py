"""drawdown_core.py — 空頭回撤 vs 復原分析引擎（reclaim-prior-peak 定義）。

⚠️ 【依規格重建】本檔非原版貼上，而是「依使用者書面定義重新建構」。
   原版 drawdown_core.py 全文當時未提供（規格書留白），故總管選路徑 B：
   嚴格照文字定義重建演算法與指標。**請與你手上的原版 diff**，指標語意應一致，
   若有出入以你原版為準、回報我對齊。

────────────────────────────────────────────────────────────────────────
核心定義（reclaim-prior-peak，收復前高才算復原）
  • 歷史新高 ATH        = 收盤價的 running cummax（累積最大值，向量化）。
  • 水下（underwater）  = 收盤 < ATH（嚴格小於）；連續為 True 的一段 = 一次回撤事件。
  • 峰頂 peak           = 水下期「之前」的那個歷史新高（進入水下前一日的收盤）。
                          水下期間 ATH 不變 → peak 即該事件全程的 ATH。
  • 谷底 trough         = 事件區間內的最低收盤（groupby idxmin）。
  • 復原日 recovery     = 谷底之後、首個「收盤 ≥ 峰頂」之日；即水下段結束、
                          收盤重新站回 ATH 的那一天（若資料到期仍未站回 → 尚未復原）。

指標（語意需與原版一致）
  • drawdown            = trough / peak − 1            （≤ 0，最大回撤深度）
  • required_gain       = peak / trough − 1            （≥ 0，谷底收復前高所需漲幅）
  • 下跌/復原/來回 天數 = 峰→谷 / 谷→復 / 峰→復，**日曆天與交易天各一份**
  • implied_recovery_cagr = (peak / trough) ** (252 / recovery_tdays) − 1
                          （以谷→復交易天、252 交易日年化的隱含復原 CAGR）

設計原則
  • 熱路徑全向量化：cummax（ATH）、布林 run-length 分段（.ne(shift).cumsum()）、
    groupby idxmin（谷底）。逐事件迴圈只跑「事件數」次（數十年也僅數十筆），
    非逐列（per-row），不是熱路徑。
  • 統計：scipy.stats 為主（Pearson/Spearman/OLS 含 p 值與精確 CI）；scipy 缺席時
    自動退回純 numpy（Fisher-z CI、p 值從缺），不硬崩（相依風險見 GOTCHAS「cp314 × 未鎖依賴」）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252  # CAGR 年化用交易日數（SSOT，勿散落各檔）


# ── 反常態累積分布（Acklam 有理逼近，取代 scipy.stats.norm.ppf）─────────────

def inv_norm_cdf(p: float) -> float:
    """標準常態分布的反累積分布函數 Φ⁻¹(p)，p∈(0,1)。

    純 Python 實作（Peter Acklam 有理逼近，絕對誤差 < 1.15e-9），
    用於 Fisher-z 信賴區間的臨界值，免依賴 scipy。
    p=0.975 → 約 1.959964（雙尾 95% 的單尾臨界）。
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"inv_norm_cdf 需 0<p<1，收到 {p!r}")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)


# ── 事件資料結構 ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DrawdownEpisode:
    """單一回撤事件（reclaim-prior-peak）。天數皆為非負整數；未復原者復原/來回欄為 None。"""

    peak_date: pd.Timestamp
    peak_price: float
    trough_date: pd.Timestamp
    trough_price: float
    recovery_date: pd.Timestamp | None
    recovery_price: float | None

    drawdown: float             # trough/peak − 1（≤0）
    required_gain: float        # peak/trough − 1（≥0）

    decline_cal_days: int       # 峰→谷 日曆天
    decline_tdays: int          # 峰→谷 交易天
    recovery_cal_days: int | None   # 谷→復 日曆天
    recovery_tdays: int | None      # 谷→復 交易天
    roundtrip_cal_days: int | None  # 峰→復 日曆天
    roundtrip_tdays: int | None     # 峰→復 交易天

    implied_recovery_cagr: float | None  # (peak/trough)^(252/recovery_tdays)−1
    recovered: bool

    def to_dict(self) -> dict:
        return asdict(self)


# ── 向量化：水下曲線 ──────────────────────────────────────────────────────

def compute_drawdown_series(prices: pd.DataFrame, price_col: str = "Close") -> pd.DataFrame:
    """回傳逐日水下序列：close / ath(歷史新高) / drawdown(≤0) / underwater(bool)。

    Parameters
    ----------
    prices : pd.DataFrame
        以日期為索引（DatetimeIndex）、含收盤價欄的價格表。
    price_col : str
        收盤價欄名（預設 "Close"）。

    Returns
    -------
    pd.DataFrame
        欄：close, ath, drawdown, underwater。索引沿用輸入（已排序）。
    """
    if price_col not in prices.columns:
        raise KeyError(f"價格表缺少欄位 {price_col!r}；現有欄位：{list(prices.columns)}")
    close = pd.to_numeric(prices[price_col], errors="coerce")
    out = pd.DataFrame(index=prices.index.copy())
    out["close"] = close
    out = out.dropna(subset=["close"]).sort_index()
    out["ath"] = out["close"].cummax()                 # running all-time-high（向量化）
    out["drawdown"] = out["close"] / out["ath"] - 1.0  # ≤ 0
    out["underwater"] = out["close"] < out["ath"]      # 嚴格小於 → 水下
    return out


# ── 向量化分段 + 逐事件度量 ────────────────────────────────────────────────

def find_episodes(
    prices: pd.DataFrame,
    price_col: str = "Close",
    min_drawdown: float = -0.10,
) -> list[DrawdownEpisode]:
    """切出所有 |回撤| ≥ 門檻的水下事件（reclaim-prior-peak）。

    Parameters
    ----------
    prices : pd.DataFrame
        日期索引 + 收盤欄。
    price_col : str
        收盤欄名。
    min_drawdown : float
        納入門檻（負值）。例：-0.10 收錄修正級以上、-0.20 只收熊市級。
        僅保留 drawdown ≤ min_drawdown 的事件（更深才納入）。

    Returns
    -------
    list[DrawdownEpisode]
        依峰頂日期由舊到新排序。資料到期仍未站回前高者以 recovered=False 收錄。
    """
    ser = compute_drawdown_series(prices, price_col)
    n = len(ser)
    if n == 0 or not bool(ser["underwater"].any()):
        return []

    underwater = ser["underwater"]
    # 布林 run-length 分段：相鄰值變動處 +1，得到連續段的 group id（向量化）
    run_id = underwater.ne(underwater.shift()).cumsum()

    close_np = ser["close"].to_numpy(dtype=float)
    dates = ser.index

    # 只取水下列，以 run_id 分組；trough 用 groupby idxmin（向量化取每段最低收盤位置）
    work = pd.DataFrame({"pos": np.arange(n), "close": close_np, "run_id": run_id.to_numpy()})
    uw = work[underwater.to_numpy()]
    trough_pos_by_run = uw.groupby("run_id")["close"].idxmin()   # 值＝work 的 RangeIndex＝位置
    start_pos_by_run = uw.groupby("run_id")["pos"].first()
    end_pos_by_run = uw.groupby("run_id")["pos"].last()

    episodes: list[DrawdownEpisode] = []
    for rid in start_pos_by_run.index:                # 逐事件（非逐列）：僅事件數次
        start_pos = int(start_pos_by_run.loc[rid])
        end_pos = int(end_pos_by_run.loc[rid])
        peak_pos = start_pos - 1
        if peak_pos < 0:
            continue                                  # 防呆：index 0 恆非水下（close==ath），理應不發生
        trough_pos = int(trough_pos_by_run.loc[rid])

        peak_price = float(close_np[peak_pos])        # ＝水下期間不變的 ATH
        trough_price = float(close_np[trough_pos])
        drawdown = trough_price / peak_price - 1.0
        # 太淺才跳過。+1e-9 容忍浮點：如 100→90 得 -0.09999999998，與門檻 -0.10 屬同值，
        # 邊界(剛好 -10%/-20%)應納入（docstring 承諾 drawdown ≤ 門檻為含）。
        if drawdown > min_drawdown + 1e-9:
            continue
        required_gain = peak_price / trough_price - 1.0

        peak_date = dates[peak_pos]
        trough_date = dates[trough_pos]
        decline_tdays = trough_pos - peak_pos
        decline_cal_days = int((trough_date - peak_date).days)

        recovered = (end_pos + 1) < n                 # 水下段之後尚有資料 → 已站回前高
        if recovered:
            recovery_pos = end_pos + 1                # 首個 close ≥ peak 之日（水下段結束隔日）
            recovery_date = dates[recovery_pos]
            recovery_price = float(close_np[recovery_pos])
            recovery_tdays = recovery_pos - trough_pos
            recovery_cal_days = int((recovery_date - trough_date).days)
            roundtrip_tdays = recovery_pos - peak_pos
            roundtrip_cal_days = int((recovery_date - peak_date).days)
            implied_cagr = (
                (peak_price / trough_price) ** (TRADING_DAYS_PER_YEAR / recovery_tdays) - 1.0
                if recovery_tdays > 0 else None
            )
        else:
            recovery_date = recovery_price = None
            recovery_tdays = recovery_cal_days = None
            roundtrip_tdays = roundtrip_cal_days = None
            implied_cagr = None

        episodes.append(DrawdownEpisode(
            peak_date=peak_date, peak_price=peak_price,
            trough_date=trough_date, trough_price=trough_price,
            recovery_date=recovery_date, recovery_price=recovery_price,
            drawdown=drawdown, required_gain=required_gain,
            decline_cal_days=decline_cal_days, decline_tdays=decline_tdays,
            recovery_cal_days=recovery_cal_days, recovery_tdays=recovery_tdays,
            roundtrip_cal_days=roundtrip_cal_days, roundtrip_tdays=roundtrip_tdays,
            implied_recovery_cagr=implied_cagr, recovered=recovered,
        ))

    episodes.sort(key=lambda e: e.peak_date)
    return episodes


def episodes_to_frame(episodes: list[DrawdownEpisode]) -> pd.DataFrame:
    """事件清單 → 整潔 DataFrame（英文欄鍵，供 UI 對應雙語標籤）。空清單回空表。"""
    cols = [
        "peak_date", "peak_price", "trough_date", "trough_price",
        "recovery_date", "recovery_price", "drawdown", "required_gain",
        "decline_cal_days", "decline_tdays", "recovery_cal_days", "recovery_tdays",
        "roundtrip_cal_days", "roundtrip_tdays", "implied_recovery_cagr", "recovered",
    ]
    if not episodes:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([e.to_dict() for e in episodes])[cols]


def episode_path(series: pd.DataFrame, ep: "DrawdownEpisode") -> pd.DataFrame:
    """單一事件從峰頂到復原（或資料末）的正規化路徑，供 V 型疊圖。

    Parameters
    ----------
    series : pd.DataFrame
        compute_drawdown_series 的輸出（需含 'close' 欄、DatetimeIndex）。
        **須與產生 ep 的 price_col 一致**（如 Low 基準：series 與 ep 都要來自 Low），
        否則正規化基準錯位、峰頂不落在 100。
    ep : DrawdownEpisode
        目標事件。

    Returns
    -------
    pd.DataFrame
        欄：close、norm（峰頂=100 的正規化值 close/peak*100）、
        cal_days（距峰頂日曆天）、tdays（距峰頂交易天，0..k）。
    """
    end = ep.recovery_date if (ep.recovered and ep.recovery_date is not None) else series.index[-1]
    seg = series.loc[ep.peak_date:end, ["close"]].copy()
    seg["norm"] = seg["close"] / ep.peak_price * 100.0
    seg["cal_days"] = (seg.index - ep.peak_date).days
    seg["tdays"] = np.arange(len(seg))
    return seg


# ── 統計：Pearson/Spearman/OLS（scipy 優先，含 p 值；numpy 為退路）─────────────

def _fisher_ci(r: float, n: int, ci: float) -> "tuple[float | None, float | None]":
    """Pearson r 的 Fisher-z 信賴區間（無 scipy 時退路）。|r|==1→(r,r)；n≤3→從缺（避免 1/√0）。"""
    if abs(r) >= 1.0:
        return (r, r)
    if n <= 3:
        return (None, None)
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    zc = inv_norm_cdf(0.5 + ci / 2.0)
    return (math.tanh(z - zc * se), math.tanh(z + zc * se))


def correlation_report(x, y, ci: float = 0.95) -> dict:
    """相關/回歸摘要：Pearson（r, p, CI）、Spearman（ρ, p）、OLS（斜率/截距/R²/斜率 p）。

    scipy 可用 → 走 scipy.stats（含 p 值與精確 CI）；否則退回純 numpy（Fisher-z CI、
    p 值從缺）。成對非有限值（NaN/inf）先剔除；n<3 或某軸無變異 → 相關/CI/p 皆從缺
    （不會拋例外）。

    Parameters
    ----------
    x, y : array-like  等長數列。
    ci : float  Pearson 信賴區間水準（0<ci<1，預設 0.95）。

    Returns
    -------
    dict  n, pearson_r, pearson_p, pearson_ci(low,high), spearman_rho, spearman_p,
          ols_slope, ols_intercept, ols_r2, ols_slope_p。
    """
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    if xa.shape != ya.shape:
        raise ValueError(f"x、y 長度不一致：{xa.shape} vs {ya.shape}")
    mask = np.isfinite(xa) & np.isfinite(ya)
    xa, ya = xa[mask], ya[mask]
    n = int(xa.size)
    result: dict = {
        "n": n, "pearson_r": None, "pearson_p": None, "pearson_ci": (None, None),
        "spearman_rho": None, "spearman_p": None,
        "ols_slope": None, "ols_intercept": None, "ols_r2": None, "ols_slope_p": None,
    }
    if n < 3 or np.ptp(xa) == 0 or np.ptp(ya) == 0:
        return result   # 樣本不足或某軸無變異 → 相關無定義

    try:
        from scipy import stats  # 優先：提供 p 值與精確信賴區間
    except ImportError:
        stats = None

    if stats is not None:
        pr = stats.pearsonr(xa, ya)
        result["pearson_r"] = float(pr.statistic)
        result["pearson_p"] = float(pr.pvalue)
        if n <= 3:
            result["pearson_ci"] = (None, None)   # 與 numpy 退路一致：n≤3 的 CI 無資訊 → 從缺
        else:
            try:
                civ = pr.confidence_interval(confidence_level=ci)
                result["pearson_ci"] = (float(civ.low), float(civ.high))
            except Exception:  # noqa: BLE001 — 舊版 scipy 無 confidence_interval → 退 Fisher-z
                result["pearson_ci"] = _fisher_ci(result["pearson_r"], n, ci)
        sr = stats.spearmanr(xa, ya)
        result["spearman_rho"] = float(sr.statistic)
        result["spearman_p"] = float(sr.pvalue)
        lin = stats.linregress(xa, ya)
        result["ols_slope"] = float(lin.slope)
        result["ols_intercept"] = float(lin.intercept)
        result["ols_r2"] = float(lin.rvalue ** 2)
        result["ols_slope_p"] = float(lin.pvalue)
        return result

    # ── numpy 退路（無 scipy）：r / Fisher-z CI / Spearman / OLS；p 值從缺 ──
    r = float(np.corrcoef(xa, ya)[0, 1])
    result["pearson_r"] = r
    result["pearson_ci"] = _fisher_ci(r, n, ci)
    rx = pd.Series(xa).rank().to_numpy()
    ry = pd.Series(ya).rank().to_numpy()
    result["spearman_rho"] = float(np.corrcoef(rx, ry)[0, 1])
    slope, intercept = np.polyfit(xa, ya, 1)
    yhat = intercept + slope * xa
    ss_res = float(np.sum((ya - yhat) ** 2))
    ss_tot = float(np.sum((ya - ya.mean()) ** 2))
    result["ols_slope"] = float(slope)
    result["ols_intercept"] = float(intercept)
    result["ols_r2"] = (1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    return result
