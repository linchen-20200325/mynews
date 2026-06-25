"""vcp_signals.py — 個股 VCP(波動收縮型態)買點偵測的單一真相源。

VCP = Volatility Contraction Pattern(Mark Minervini):股價在高檔打底時出現
「一波比一波淺」的數段拉回(如 22%→13%→7%),且末段成交量明顯萎縮(量縮),
最後沿著前波高點(樞紐 pivot)放量突破 = 買點。

只服務「個股盯盤」LINE 推播:每檔在技術/籌碼面後,**偵測到才**多附一行
「🎯 VCP …」;沒成形就靜默略過。資料源沿用 ``tech_signals.fetch_daily_k``
(證交所 STOCK_DAY 日K,已含 volume),不重抓、不另立資料源(SSOT)。

判定全程用真實日K計算、零估算;本質為啟發式,刻意採保守門檻(寧可少報),
所有推播文字均附「僅供參考,非投資建議」。上櫃(TPEx)同 tech_signals 暫未涵蓋。

純資料/計算層,不畫 Streamlit 元件;早上排程批次跑一次,不需 st.cache。
"""

from __future__ import annotations

import os

import tech_signals  # 沿用其 fetch_daily_k(日K+volume)當唯一資料來源(SSOT)

_DEFAULT_MONTHS = 8        # 回看月數(≈160 個交易日,夠看出收縮序列+前波漲勢)
_ZIGZAG_PCT = 0.04        # ZigZag 反轉門檻:波段需 ≥4% 折返才記一個轉折點
_MIN_BARS = 50            # 少於此筆數不足以辨識型態
_MIN_CONTRACTIONS = 2     # 至少兩段收縮(Minervini 慣例 2~4 段)
_MAX_CONTRACTIONS = 4     # 只看最近最多 4 段
_LAST_TIGHT = 0.12        # 末段收縮幅度需 ≤12% 才算「夠緊」
_NEAR_PIVOT = 0.08        # 收盤距樞紐 ≤8%(在下方)才算「逼近樞紐、蓄勢待突破」
_BASE_NEAR_HIGH = 0.85    # 樞紐需 ≥區間最高價×0.85(打底在高檔,非破底下跌段)
_VOL_DRYUP = 0.85         # 末段(近5日)均量 ≤ 整段均量×0.85 視為量縮
_BREAKOUT_VOL = 1.3       # 突破日量 ≥ 近期均量×1.3 視為放量突破


def _zigzag(rows: list[dict], pct: float = _ZIGZAG_PCT) -> list[tuple[int, float, str]]:
    """ZigZag 轉折點:回 [(index, price, 'H'/'L'), ...](由舊到新)。

    以收盤價為準,自最後極值反向折返達 pct 即確認一個轉折;末端附當前極值為暫定轉折。
    """
    closes = [r["close"] for r in rows]
    if len(closes) < 3:
        return []
    pivots: list[tuple[int, float, str]] = []
    trend = 0          # 1=上升中, -1=下降中, 0=未定
    ext_i, ext_p = 0, closes[0]
    for i in range(1, len(closes)):
        c = closes[i]
        if trend >= 0:
            if c > ext_p:
                ext_i, ext_p = i, c
            elif c <= ext_p * (1 - pct):
                pivots.append((ext_i, ext_p, "H"))
                trend, ext_i, ext_p = -1, i, c
                continue
        if trend <= 0:
            if c < ext_p:
                ext_i, ext_p = i, c
            elif c >= ext_p * (1 + pct):
                pivots.append((ext_i, ext_p, "L"))
                trend, ext_i, ext_p = 1, i, c
    pivots.append((ext_i, ext_p, "H" if trend >= 0 else "L"))
    return pivots


def _contractions(pivots: list[tuple[int, float, str]]) -> list[float]:
    """由轉折序列取出各段拉回幅度(H→隨後 L 的跌幅 (H-L)/H),依時間排序。"""
    depths: list[float] = []
    for a, b in zip(pivots, pivots[1:]):
        (_, hp, ht), (_, lp, lt) = a, b
        if ht == "H" and lt == "L" and hp > 0 and lp < hp:
            depths.append((hp - lp) / hp)
    return depths


def _is_decreasing(seq: list[float]) -> bool:
    """嚴格遞減(允許微幅 2% 容差,容許實務上接近相等的相鄰兩段)。"""
    return all(b <= a * 1.02 for a, b in zip(seq, seq[1:]))


def detect(rows: list[dict]) -> dict | None:
    """由日K判斷 VCP;回特徵 dict 或 None(不成形)。

    成形條件(全部需滿足):資料足量、≥2 段收縮且幅度遞減、末段夠緊、樞紐在高檔、
    收盤逼近或突破樞紐、末段量縮。另標 breakout(收盤站上樞紐且放量)。
    """
    rows = [r for r in rows if r.get("close")]
    if len(rows) < _MIN_BARS:
        return None
    pivots = _zigzag(rows)
    depths = _contractions(pivots)
    if len(depths) < _MIN_CONTRACTIONS:
        return None
    depths = depths[-_MAX_CONTRACTIONS:]
    if not _is_decreasing(depths) or depths[-1] > _LAST_TIGHT:
        return None

    # 樞紐 = 前一個「已確認」高點轉折價(前波壓力)。排除末端暫定轉折(那是當前極值,
    # 取它會變成「收X逼近樞紐X」的 gap≈0 怪句);收盤要對照的是先前那道壓力。
    highs_p = [p for p in pivots[:-1] if p[2] == "H"]
    if not highs_p:
        return None
    pivot = highs_p[-1][1]
    # 打底需在高檔:樞紐貼近整段最高價,排除下跌途中的假收縮
    hi_all = max((r["high"] for r in rows if r.get("high")), default=0)
    if hi_all <= 0 or pivot < hi_all * _BASE_NEAR_HIGH:
        return None

    close = rows[-1]["close"]
    gap = (close - pivot) / pivot  # >0=已站上樞紐, <0=在樞紐下方
    if gap < -_NEAR_PIVOT:         # 離樞紐還太遠 → 尚未蓄勢
        return None

    # 量縮:近 5 日均量 vs 全段均量
    vols = [r["volume"] for r in rows if r.get("volume")]
    vol_dryup = False
    breakout_vol = False
    if len(vols) >= 20:
        base_avg = sum(vols) / len(vols)
        recent_avg = sum(vols[-5:]) / 5
        vol_dryup = recent_avg <= base_avg * _VOL_DRYUP
        breakout_vol = vols[-1] >= recent_avg * _BREAKOUT_VOL
    if not vol_dryup and gap < 0:  # 還沒突破又沒量縮 → 不算 VCP 蓄勢
        return None

    breakout = gap >= 0 and breakout_vol
    return {
        "contractions": depths,
        "pivot": pivot,
        "close": close,
        "gap_pct": gap * 100,
        "vol_dryup": vol_dryup,
        "breakout": breakout,
    }


def signal_text(feat: dict | None) -> str | None:
    """把 VCP 特徵組成一行 LINE 白話文字;不成形回 None。"""
    if not feat:
        return None
    seq = "→".join(f"{d * 100:.0f}%" for d in feat["contractions"])
    pivot, close = feat["pivot"], feat["close"]
    if feat["breakout"]:
        return (f"🎯 VCP突破買點! 收{close:g}站上樞紐{pivot:g}"
                f"(收縮{seq}、放量)")
    tail = "、量縮" if feat["vol_dryup"] else ""
    return (f"🎯 VCP收縮成形 收{close:g}逼近樞紐{pivot:g}"
            f"(收縮{seq}{tail}),留意突破")


def signals_for(stocks: list[dict], months: int | None = None, log=print) -> dict[str, str]:
    """逐檔偵測 VCP;回 {ticker: 文字}。未成形/抓不到的代號不收錄(該檔靜默略過)。"""
    months = months or int(os.environ.get("WATCH_VCP_MONTHS", str(_DEFAULT_MONTHS)))
    out: dict[str, str] = {}
    for s in stocks:
        ticker = str(s.get("ticker", "")).strip()
        if not ticker:
            continue
        try:
            text = signal_text(detect(tech_signals.fetch_daily_k(ticker, months=months, log=log)))
            if text:
                out[ticker] = text
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不影響其他檔/其他面向
            log(f"  VCP {ticker} 偵測失敗:{exc}")
    return out
