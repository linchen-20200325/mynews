"""reversal_signals.py — 中線行情翻轉偵測 SSOT

台股大盤 / 美股費半指數 / 個股通用。三大硬指標共振：
  1. 季線（60MA）慣性翻轉：連 3 天實體站穩/跌破 + 扣抵值方向預測
  2. 籌碼板塊挪移：大盤模式（融資維持率 + 外資期貨）/ 個股模式（集保持股分級）
  3. 全球半導體龍頭週K線結構：TSM（台積電 ADR）& NVDA（輝達）共振

主入口：detect_trend_reversal(symbol, is_market=True, chip_df=None) → dict

【mock 資料治理】籌碼歷史序列尚未接通真實 API 時以 mock 填充,mock 列一律帶
``is_mock=True`` 欄位;_check_chip_* 偵測到 mock 即回 triggered=False(不納入共振),
避免「亂數湊出的籌碼共振」污染絕對買進/賣出訊號 — 假確定性比沒有訊號更危險。
mock 序列以 hashlib.md5 產生(跨進程確定性;內建 hash() 受 PYTHONHASHSEED 影響會漂移)。

籌碼 DataFrame 欄位規格（preloaded 或 mock，後續接通真實 API 時替換 mock 函數即可）：
  ┌ 大盤模式 ──────────────────────────────────────────────────────────────┐
  │  date              str  "YYYY-MM-DD"（期交所公告日）                    │
  │  margin_ratio      float  融資維持率（%），如 158.3                      │
  │  foreign_net_futures int  外資期貨淨部位（口，正=多單 負=空單），如 -38000 │
  └────────────────────────────────────────────────────────────────────────┘
  ┌ 個股模式 ──────────────────────────────────────────────────────────────┐
  │  week_end          str  "YYYY-MM-DD"（集保統計週截止日）                │
  │  large_holder_pct  float  1000張以上大戶持股比率（%）                   │
  │  small_holder_pct  float  10張以下散戶持股比率（%）                     │
  └────────────────────────────────────────────────────────────────────────┘

Streamlit 快取：本模組為純計算/資料層（無 st 依賴）。UI 層呼叫時請自行包裹
    @st.cache_data(ttl=3600) 以避免每次 rerun 重打 yfinance / TWSE。
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pandas as pd

import tech_signals  # fetch_daily_k + SMA 計算 SSOT（台股 TWSE 日K）
import tz_utils      # 台灣時區 SSOT

# ── 常數 ─────────────────────────────────────────────────────────────────
_MA_WINDOW       = 60    # 季線窗格（交易日）
_CONFIRM_DAYS    = 3     # 連續確認天數（實體跌破/站穩）
_WEEKLY_LOOKBACK = 4     # 週K破低回看週數
_ENGULF_LOOKBACK = 2     # 吞噬前幾週
_LOWER_SHADOW_X  = 2.0   # 下影線 ≥ N 倍實體

# 大盤籌碼門檻
_MARGIN_WARN      = 160.0   # 融資維持率轉壞門檻（%）
_MARGIN_CLIMAX    = 140.0   # 斷頭潮尾聲門檻（%）
_SHORT_THRESHOLD  = 35_000  # 外資期貨淨空單連 3 天超過此口數 → 轉壞
_BUYBACK_DELTA    = 10_000  # 3 天內回補超過此口數 → 好轉

# 個股籌碼確認週數
_CHIP_WEEKS = 3

# 半導體領先標的（yfinance 代號）
_SEMICON_LEADERS = ("TSM", "NVDA")


# ── yfinance 資料抓取 ──────────────────────────────────────────────────
def _yf_download(symbol: str, period: str, interval: str) -> pd.DataFrame:
    """yfinance 下載；lazy import、失敗回空 DataFrame。

    處理 yfinance ≥0.2 在部分版本回傳 MultiIndex columns 的相容問題。
    """
    try:
        yf = importlib.import_module("yfinance")
        df: pd.DataFrame = yf.download(
            symbol, period=period, interval=interval,
            auto_adjust=True, progress=False
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna(subset=["Close"])
    except Exception:  # noqa: BLE001 — 網路失敗或模組缺失回空 DataFrame
        return pd.DataFrame()


def _fetch_daily_df(symbol: str) -> pd.DataFrame:
    """取日K DataFrame（需含 Close、Open 兩欄，由舊到新排序）。

    台灣上市代號（純數字）→ tech_signals.fetch_daily_k（TWSE STOCK_DAY SSOT）；
    其餘（美股、^指數、*.TW 等）→ yfinance。

    yfinance 的 Open 偶爾含 NaN（尚未開盤），以該日 Close 填補確保實體計算不崩。
    """
    base = symbol.upper().replace(".TW", "").replace(".TWO", "")
    if base.isdigit():
        rows = tech_signals.fetch_daily_k(base, months=6)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["Close"] = pd.to_numeric(df["close"], errors="coerce")
        df["Open"]  = pd.to_numeric(df["open"],  errors="coerce").fillna(df["Close"])
        return df[["Close", "Open"]].dropna(subset=["Close"]).reset_index(drop=True)

    df = _yf_download(symbol, period="6mo", interval="1d")
    if df.empty or "Close" not in df.columns:
        return pd.DataFrame()
    if "Open" not in df.columns:
        df["Open"] = df["Close"]
    else:
        df["Open"] = df["Open"].fillna(df["Close"])
    return df[["Close", "Open"]].reset_index(drop=True)


def _fetch_weekly_df(symbol: str) -> pd.DataFrame:
    """取週K DataFrame（需含 Close/Open/High/Low）；一律走 yfinance。

    取 6 個月週K：足夠涵蓋 _WEEKLY_LOOKBACK(4) + 緩衝，同時不至過重。
    """
    return _yf_download(symbol, period="6mo", interval="1wk")


# ── 指標一：60MA 慣性翻轉 ──────────────────────────────────────────────
def _check_ma60(df: pd.DataFrame) -> dict:
    """連 3 天實體跌破/站穩季線 + 扣抵值方向判斷。

    扣抵值（closes[-60]）= 60MA 計算窗格最老的那根；下一交易日加入新K棒時
    此值被移出，若扣抵值 > 當前收盤 → MA 將下行（看空），反之將上行（看多）。

    以當前 MA60 做為近 3 日實體比對基準（3 天的 MA 變動幅度 <0.2%，誤差可忽略）。
    """
    result: dict = {"triggered": False, "direction": None, "detail": "資料不足"}

    if len(df) < _MA_WINDOW + _CONFIRM_DAYS + 1:
        return result

    closes = df["Close"].ffill().tolist()
    opens  = df["Open"].fillna(df["Close"]).tolist()

    ma60_current = sum(closes[-_MA_WINDOW:]) / _MA_WINDOW
    current_close = closes[-1]
    deduct_val    = closes[-_MA_WINDOW]  # 即將被扣抵的 60 日前收盤

    # 近 _CONFIRM_DAYS 根K棒的實體（最高 vs 最低端）相對 MA60 的位置
    body_top = [max(closes[i], opens[i]) for i in range(-_CONFIRM_DAYS, 0)]
    body_bot = [min(closes[i], opens[i]) for i in range(-_CONFIRM_DAYS, 0)]

    all_below = all(top < ma60_current for top in body_top)   # 整根實體在 MA 下方
    all_above = all(bot > ma60_current for bot in body_bot)   # 整根實體在 MA 上方

    deduct_bearish = deduct_val > current_close  # 扣抵值高 → MA 即將下彎
    deduct_bullish = deduct_val < current_close  # 扣抵值低 → MA 即將走平上揚

    recent_str = "→".join(f"{closes[i]:.2f}" for i in range(-_CONFIRM_DAYS, 0))

    if all_below and deduct_bearish:
        result.update({
            "triggered": True, "direction": "bad",
            "detail": (
                f"連{_CONFIRM_DAYS}天實體跌破60MA({ma60_current:.2f})，"
                f"近收盤{recent_str}，"
                f"扣抵值{deduct_val:.2f}>今收{current_close:.2f}，季線即將下彎"
            ),
        })
    elif all_above and deduct_bullish:
        result.update({
            "triggered": True, "direction": "good",
            "detail": (
                f"連{_CONFIRM_DAYS}天實體站上60MA({ma60_current:.2f})，"
                f"近收盤{recent_str}，"
                f"扣抵值{deduct_val:.2f}<今收{current_close:.2f}，季線即將走平上揚"
            ),
        })
    else:
        result["detail"] = (
            f"60MA({ma60_current:.2f})，今收{current_close:.2f}，"
            f"扣抵值{deduct_val:.2f}，未滿足連{_CONFIRM_DAYS}天同側確認"
        )
    return result


# ── 指標二：籌碼板塊挪移 ───────────────────────────────────────────────────
def _det_seed(text: str, mod: int) -> int:
    """跨進程確定性 seed。內建 hash() 未設 PYTHONHASHSEED 時每個進程都不同,
    會讓 mock 序列在排程/看板間漂移,破壞「確定性 mock」宣稱;改用 md5。"""
    return int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16) % mod


def _load_real_market_chip() -> pd.DataFrame | None:
    """嘗試從 paths.LATEST_FUT_CHIP + paths.LATEST_MARGIN 建立真實大盤籌碼 DataFrame。

    只有「最新一筆」真實資料可用（無歷史序列歸檔），因此只取最後一行，
    其餘 9 行仍用 mock 填充，使連 3 天判斷不因真實資料少一筆而失效。
    真實資料行的 margin_ratio 欄位標為 mock（融資維持率歷史序列尚未歸檔）；
    foreign_net_futures 若有 latest_futures_chip.json 則取真實口數。
    回 None 表示真實檔案不存在，呼叫端 fallback 純 mock。
    """
    try:
        import paths  # lazy import 防止非 Streamlit 環境循環
        fut_path: Path = paths.LATEST_FUT_CHIP
        if not fut_path.exists():
            return None
        fut = json.loads(fut_path.read_text(encoding="utf-8"))
        real_oi: int = int(fut.get("foreign_net_oi", 0))
        real_date: str = fut.get("date", tz_utils.taiwan_now().date().strftime("%Y-%m-%d"))
    except Exception:  # noqa: BLE001
        return None

    # 建9筆 mock 歷史 + 最後1筆填入真實期貨部位(is_mock 標記供 _check_chip_market 排除)
    today = tz_utils.taiwan_now().date()
    rows = []
    for i in range(9, 0, -1):
        dt = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        seed = _det_seed(dt, 10000)
        rows.append({
            "date": dt,
            "margin_ratio": round(155 + (seed % 20) - 10, 1),   # mock（融資維持率歷史未歸檔）
            "foreign_net_futures": int(-36000 + (seed % 6000) - 3000),  # mock 歷史
            "is_mock": True,
        })
    rows.append({
        "date": real_date,
        "margin_ratio": round(155 + (_det_seed(real_date, 20)) - 10, 1),  # mock（歷史未歸檔）
        "foreign_net_futures": real_oi,   # ← 真實外資台指期留倉口數
        "is_mock": False,  # 期貨口數為真實;此列 margin_ratio 仍為 mock(僅供顯示,不觸發訊號)
    })
    return pd.DataFrame(rows)


def _mock_market_chip() -> pd.DataFrame:
    """大盤籌碼 mock（確定性;is_mock=True → 不納入共振計算,僅供介面演示）。

    接通真實資料時替換本函數，呼叫端（detect_trend_reversal）與判斷邏輯
    （_check_chip_market）無須改動 — 僅替換資料來源。
    """
    today = tz_utils.taiwan_now().date()
    rows = []
    for i in range(10, 0, -1):
        dt = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        # 固定 seed 使 mock 資料穩定（非正式環境）
        seed = _det_seed(dt, 10000)
        rows.append({
            "date": dt,
            "margin_ratio": round(155 + (seed % 20) - 10, 1),
            "foreign_net_futures": int(-36000 + (seed % 6000) - 3000),
            "is_mock": True,
        })
    return pd.DataFrame(rows)


def _mock_stock_chip(ticker: str) -> pd.DataFrame:
    """個股籌碼 mock（集保所持股分級，週度資料;is_mock=True → 不納入共振計算）。

    接通集保 API 後替換本函數；欄位規格不變。
    """
    today = tz_utils.taiwan_now().date()
    base_seed = _det_seed(ticker, 1000)
    rows = []
    for i in range(6, 0, -1):
        wk = (today - timedelta(weeks=i)).strftime("%Y-%m-%d")
        drift = i * 0.3
        rows.append({
            "week_end": wk,
            "large_holder_pct": round(58 - drift + (base_seed % 3) * 0.1, 2),
            "small_holder_pct": round(20 + drift * 0.7 + (base_seed % 2) * 0.05, 2),
            "is_mock": True,
        })
    return pd.DataFrame(rows)


def _has_mock_rows(chip_df: pd.DataFrame) -> bool:
    """判斷籌碼 DataFrame 是否混有 mock 列(外部傳入的真實資料無 is_mock 欄 → False)。"""
    return "is_mock" in chip_df.columns and bool(chip_df["is_mock"].any())


def _check_chip_market(chip_df: pd.DataFrame) -> dict:
    """大盤籌碼判斷：融資維持率 + 外資期貨淨部位。

    混有 mock 列時一律 triggered=False(標 is_mock=True):亂數序列有相當機率湊出
    「連 3 天重空/大幅回補」,若計入共振會污染「絕對買進/賣出」訊號。
    """
    result: dict = {"triggered": False, "direction": None, "detail": "大盤籌碼資料不足"}
    if chip_df is None or len(chip_df) < _CONFIRM_DAYS:
        return result
    if not {"margin_ratio", "foreign_net_futures"}.issubset(chip_df.columns):
        return result

    if _has_mock_rows(chip_df):
        note = "籌碼歷史序列為模擬值(mock),不納入共振計算(待接通期交所/集保真實歷史)"
        try:
            if not bool(chip_df["is_mock"].iloc[-1]):
                note += (f";最新真實外資期貨淨部位 "
                         f"{int(chip_df['foreign_net_futures'].iloc[-1]):+,} 口")
        except (KeyError, TypeError, ValueError):
            pass
        result.update({"is_mock": True, "detail": note})
        return result

    latest_margin   = float(chip_df["margin_ratio"].iloc[-1])
    futures_series  = chip_df["foreign_net_futures"].tolist()
    last3_net       = futures_series[-_CONFIRM_DAYS:]

    # 轉壞：融資維持率 < 160% 且外資淨空單連 3 天超過 35,000 口
    short_counts = [abs(v) for v in last3_net if v < 0]
    heavy_short  = (len(short_counts) == _CONFIRM_DAYS and
                    all(v >= _SHORT_THRESHOLD for v in short_counts))
    if latest_margin < _MARGIN_WARN and heavy_short:
        result.update({
            "triggered": True, "direction": "bad",
            "detail": (
                f"融資維持率{latest_margin:.1f}%(跌破{_MARGIN_WARN:.0f}%)，"
                f"外資期淨空單連{_CONFIRM_DAYS}天>{_SHORT_THRESHOLD:,}口"
                f"（最新{abs(futures_series[-1]):,}口）"
            ),
        })
        return result

    # 好轉：融資維持率貼近斷頭潮尾聲，或外資期 3 天大幅回補
    near_climax = latest_margin <= _MARGIN_CLIMAX + 5
    buyback_3d  = (len(futures_series) >= _CONFIRM_DAYS and
                   (futures_series[-1] - futures_series[-_CONFIRM_DAYS]) >= _BUYBACK_DELTA)
    if near_climax or buyback_3d:
        parts = []
        if near_climax:
            parts.append(f"融資維持率{latest_margin:.1f}%(接近斷頭潮尾聲{_MARGIN_CLIMAX:.0f}%)")
        if buyback_3d:
            delta = futures_series[-1] - futures_series[-_CONFIRM_DAYS]
            parts.append(f"外資期{_CONFIRM_DAYS}天回補{delta:+,}口")
        result.update({
            "triggered": True, "direction": "good",
            "detail": "；".join(parts),
        })
        return result

    result["detail"] = (
        f"融資維持率{latest_margin:.1f}%，"
        f"外資期最新{futures_series[-1]:+,}口，未達觸發條件"
    )
    return result


def _check_chip_stock(chip_df: pd.DataFrame) -> dict:
    """個股籌碼判斷：大戶 vs 散戶持股比率連 N 週同向。

    混有 mock 列時一律 triggered=False(標 is_mock=True),理由同 _check_chip_market。
    """
    result: dict = {"triggered": False, "direction": None, "detail": "個股籌碼資料不足"}
    if chip_df is None or len(chip_df) < _CHIP_WEEKS:
        return result
    if not {"large_holder_pct", "small_holder_pct"}.issubset(chip_df.columns):
        return result

    if _has_mock_rows(chip_df):
        result.update({
            "is_mock": True,
            "detail": "集保持股分級為模擬值(mock),不納入共振計算(待接通集保真實 API)",
        })
        return result

    large = chip_df["large_holder_pct"].tail(_CHIP_WEEKS).tolist()
    small = chip_df["small_holder_pct"].tail(_CHIP_WEEKS).tolist()

    # 嚴格單調（相鄰兩週同向）
    large_down = all(large[i] > large[i + 1] for i in range(len(large) - 1))
    small_up   = all(small[i] < small[i + 1] for i in range(len(small) - 1))
    large_up   = all(large[i] < large[i + 1] for i in range(len(large) - 1))
    small_down = all(small[i] > small[i + 1] for i in range(len(small) - 1))

    if large_down and small_up:
        result.update({
            "triggered": True, "direction": "bad",
            "detail": (
                f"大戶(1000張↑)連{_CHIP_WEEKS}週下滑"
                f"({large[0]:.1f}%→{large[-1]:.1f}%)，"
                f"散戶(10張↓)連{_CHIP_WEEKS}週上升"
                f"({small[0]:.1f}%→{small[-1]:.1f}%)"
            ),
        })
    elif large_up and small_down:
        result.update({
            "triggered": True, "direction": "good",
            "detail": (
                f"大戶(1000張↑)連{_CHIP_WEEKS}週上升"
                f"({large[0]:.1f}%→{large[-1]:.1f}%)，"
                f"散戶(10張↓)連{_CHIP_WEEKS}週下降"
                f"({small[0]:.1f}%→{small[-1]:.1f}%)"
            ),
        })
    else:
        result["detail"] = (
            f"大戶{large[-1]:.1f}%，散戶{small[-1]:.1f}%，"
            f"未形成連{_CHIP_WEEKS}週同向趨勢"
        )
    return result


# ── 指標三：半導體龍頭週K線結構 ────────────────────────────────────────
def _check_one_weekly(wdf: pd.DataFrame, symbol: str) -> dict | None:
    """單一標的週K結構分析；資料不足回 None。

    使用 iloc[-1] 代表最新（可能未完成的）週棒。中線策略對單日誤差不敏感，
    此近似可接受；若需嚴格已完成週，呼叫端改傳 wdf.iloc[:-1]。
    """
    needed = _WEEKLY_LOOKBACK + 2  # 1(本週) + 4(回看) + 1(緩衝)
    if len(wdf) < needed or "Close" not in wdf.columns:
        return None

    close = float(wdf["Close"].iloc[-1])
    open_ = float(wdf["Open"].iloc[-1]) if "Open" in wdf.columns else close
    low   = float(wdf["Low"].iloc[-1])  if "Low"  in wdf.columns else close

    # 前 N 週的高低（不含本週：iloc[-1]）
    ref_lows  = wdf["Low"].iloc[-(1 + _WEEKLY_LOOKBACK):-1].tolist() if "Low"  in wdf.columns else []
    ref_highs = wdf["High"].iloc[-(1 + _ENGULF_LOOKBACK):-1].tolist() if "High" in wdf.columns else []

    body         = abs(close - open_)
    lower_shadow = min(open_, close) - low  # ≥0，下引線長度

    direction: Literal["bad", "good"] | None = None
    detail = ""

    # 轉壞：收盤跌破過去 N 週全部週低（週線破位）
    if ref_lows and close < min(ref_lows):
        direction = "bad"
        detail = (
            f"{symbol} 週收{close:.2f}"
            f"跌破過去{_WEEKLY_LOOKBACK}週最低({min(ref_lows):.2f}，週線破位)"
        )
    else:
        # 好轉：強力吞噬前 N 週
        if ref_highs and close > max(ref_highs):
            direction = "good"
            detail = (
                f"{symbol} 週收{close:.2f}"
                f"強力吞噬前{_ENGULF_LOOKBACK}週高點({max(ref_highs):.2f})"
            )
        # 好轉（疊加）：長下影線（≥ N 倍實體，含十字星例外保護）
        elif body > 0.001 * close and lower_shadow >= _LOWER_SHADOW_X * body:
            direction = "good"
            detail = (
                f"{symbol} 週收{close:.2f}"
                f"留{lower_shadow:.2f}下影線(≥{_LOWER_SHADOW_X:.0f}×實體{body:.2f})"
            )

    return {"direction": direction, "detail": detail or f"{symbol} 無明確週線訊號"}


def _check_semicon_weekly() -> dict:
    """TSM + NVDA 週K共振。

    轉壞條件：兩標的『同時』週線破位（雙重確認，減少假訊號）。
    好轉條件：任一標的出現吞噬或長下影線（單強訊號即足，因好轉通常先於共識）。
    """
    result: dict = {"triggered": False, "direction": None, "detail": "", "sub": {}}

    sigs: list[dict] = []
    for sym in _SEMICON_LEADERS:
        wdf = _fetch_weekly_df(sym)
        sig = _check_one_weekly(wdf, sym)
        if sig is not None:
            sigs.append(sig)
            result["sub"][sym] = sig

    if not sigs:
        result["detail"] = "週K資料抓取失敗（yfinance 連線或代號問題）"
        return result

    bad_count  = sum(1 for s in sigs if s["direction"] == "bad")
    good_count = sum(1 for s in sigs if s["direction"] == "good")

    if bad_count == len(_SEMICON_LEADERS):
        result.update({
            "triggered": True, "direction": "bad",
            "detail": "；".join(s["detail"] for s in sigs if s["direction"] == "bad"),
        })
    elif good_count >= 1:
        result.update({
            "triggered": True, "direction": "good",
            "detail": "；".join(s["detail"] for s in sigs if s["direction"] == "good"),
        })
    else:
        result["detail"] = "；".join(s["detail"] for s in sigs)

    return result


# ── 主入口 ────────────────────────────────────────────────────────────
def detect_trend_reversal(
    symbol: str,
    is_market: bool = True,
    chip_df: "pd.DataFrame | None" = None,
) -> dict:
    """中線行情翻轉偵測。

    Parameters
    ----------
    symbol    : Yahoo Finance 代號。台股大盤如 "^TWII"；個股如 "2330.TW"；
                美股費半如 "^SOX"；美股個股如 "NVDA"。
    is_market : True=大盤模式（融資維持率+外資期貨）；
                False=個股模式（集保大戶/散戶持股分級）。
    chip_df   : 外部傳入的籌碼 DataFrame；None 時使用 mock 資料（供開發/演示）。
                接通真實 API 後由呼叫端傳入，不需改動本函數。

    Returns
    -------
    dict
        signal      : "絕對買進" / "絕對賣出" / "觀望續抱"（≥2 指標同向觸發）
        confidence  : 同向觸發指標數（0~3）
        indicators  : {"ma60": {...}, "chip": {...}, "semicon": {...}}
                      各含 triggered(bool), direction("good"/"bad"/None), detail(str);
                      chip 混有 mock 資料時 triggered 恆為 False 並帶 is_mock=True
                      （mock 不納入共振,此時訊號僅由兩個真實指標決定）
        chip_is_mock: 籌碼指標是否因 mock 被排除（供 UI/LINE 明示）
        symbol      : 輸入代號
        is_market   : 輸入模式
        error       : 異常訊息或 None
    """
    out: dict = {
        "symbol":      symbol,
        "is_market":   is_market,
        "signal":      "觀望續抱",
        "confidence":  0,
        "indicators":  {},
        "error":       None,
    }

    try:
        # 指標一：60MA 慣性翻轉
        daily_df   = _fetch_daily_df(symbol)
        out["indicators"]["ma60"] = _check_ma60(daily_df)

        # 指標二：籌碼板塊挪移（大盤模式先嘗試真實外資期貨檔案，個股仍用 mock）
        if chip_df is None:
            if is_market:
                chip_df = _load_real_market_chip() or _mock_market_chip()
            else:
                chip_df = _mock_stock_chip(symbol)
        chip_sig = (_check_chip_market(chip_df) if is_market
                    else _check_chip_stock(chip_df))
        out["indicators"]["chip"] = chip_sig
        out["chip_is_mock"] = bool(chip_sig.get("is_mock"))  # 供 UI/LINE 明示 mock 狀態

        # 指標三：半導體龍頭週K（跨市場領先指標，大盤與個股皆適用）
        out["indicators"]["semicon"] = _check_semicon_weekly()

        # 共振：≥2 個指標同方向觸發
        all_sigs   = list(out["indicators"].values())
        triggered  = [s for s in all_sigs if s.get("triggered")]
        directions = [s["direction"] for s in triggered]
        bad_n      = directions.count("bad")
        good_n     = directions.count("good")

        out["confidence"] = max(bad_n, good_n)
        if bad_n >= 2:
            out["signal"] = "絕對賣出"
        elif good_n >= 2:
            out["signal"] = "絕對買進"

    except Exception as exc:  # noqa: BLE001 — 單次查詢失敗不崩整頁
        out["error"] = str(exc)

    return out
