"""line_notify.py — LINE Messaging API 推播的單一真相源(SSOT)。

職責:
  - LINE API 端點常數與文字上限
  - 路由邏輯(_push_line_text):broadcast / multicast / push 自動選擇
  - 各類推播訊息組建(build_*_line_message)
  - 公開推播入口(notify_line / notify_line_intl_alert / notify_line_chip_events / notify_line_confluence)
  - 法人事件去重狀態讀寫(load_pushed_events / save_pushed_events)
  - 籌碼提示文字(chip_flow_hint / _futures_stance_line)
  - 個股盯盤訊息組建(build_watch_line_message)

零 Streamlit 相依;可被任何模組安全 import。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import numutil
import paths

# ---------------------------------------------------------------------------
# 常數
# ---------------------------------------------------------------------------

LINE_PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"
LINE_MULTICAST_ENDPOINT = "https://api.line.me/v2/bot/message/multicast"
LINE_BROADCAST_ENDPOINT = "https://api.line.me/v2/bot/message/broadcast"
LINE_TEXT_LIMIT = 4500  # 單則 text 上限 5000,留安全餘裕

# 對台股有「時間差領先」意義的市場(美股指數=隔夜、美股期貨/台指期夜盤=盤前)
LEAD_DROP_TYPES = ("隔夜領先", "盤前即時")

OKU = numutil.OKU  # 億元換算係數 SSOT 在 numutil


# ---------------------------------------------------------------------------
# 內部工具
# ---------------------------------------------------------------------------

def _save_json(path: Path, data: dict) -> None:
    """原子化寫入 JSON(目錄不存在時自動建立)。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


# ---------------------------------------------------------------------------
# 核心推播
# ---------------------------------------------------------------------------

def _push_line_text(text: str, token: str | None = None, to: str | None = None) -> None:
    """以 LINE Messaging API 推送一則文字(共用:戰略報告 / 國際盤預警 / 法人事件 / 個股盯盤)。

    依 LINE_TO 自動選端點,達成「群體發送」且向後相容:
      * LINE_TO = "broadcast"            → /broadcast,發給所有加官方帳號好友的人(免收集 ID)。
      * LINE_TO = 多個 ID(逗號/空白分隔) → /multicast,發給指定名單(最多 500)。
      * LINE_TO = 單一 ID(user/group/room)→ /push(原行為;群組 ID 即整群可見)。

    token/to 預設讀主 bot 的 LINE_CHANNEL_ACCESS_TOKEN / LINE_TO;傳入則用第二個 bot
    (個股盯盤)的 LINE_WATCH_TOKEN / LINE_WATCH_TO,讓兩個 bot 共用同一套推播邏輯。
    """
    token = token or os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    to_raw = (to if to is not None else os.environ["LINE_TO"]).strip()
    messages = [{"type": "text", "text": text}]

    if to_raw.lower() == "broadcast":
        endpoint, body = LINE_BROADCAST_ENDPOINT, {"messages": messages}
        mode = "broadcast(全體好友)"
    else:
        ids = [t for t in re.split(r"[,\s]+", to_raw) if t]
        if len(ids) > 1:
            endpoint, body = LINE_MULTICAST_ENDPOINT, {"to": ids, "messages": messages}
            mode = f"multicast({len(ids)} 人名單)"
        else:
            endpoint, body = LINE_PUSH_ENDPOINT, {"to": ids[0], "messages": messages}
            mode = "push(單一對象)"
    # 診斷:只印模式,不印任何實際 ID(避免外洩)
    print(f"  LINE 推播模式:{mode}", flush=True)

    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"LINE 回應非 200: {resp.status}")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"LINE 推播失敗 ({exc.code}): {body_text}") from exc


# ---------------------------------------------------------------------------
# 籌碼提示
# ---------------------------------------------------------------------------

def _futures_stance_line(chip: dict | None, fut: dict | None) -> str:
    """台指期留倉:外資期貨偏多/偏空一行白話(前一交易日盤後庫存)。與現貨同向時點出雙重訊號。"""
    if not fut or fut.get("foreign_net_oi") is None:
        return ""
    net = fut["foreign_net_oi"]
    stance = fut.get("stance", "中性")
    lots = f"{abs(net) / 1e4:.1f}萬口" if abs(net) >= 10000 else f"{abs(net):,}口"
    base = f"📐 外資台指期:{stance}(淨{'多' if net >= 0 else '空'}{lots},前日盤後留倉)"
    days = (chip or {}).get("days") or []
    tot = days[0].get("total", 0) if days else 0
    if net < 0 and tot < 0:
        return base + ";與現貨同步偏空,賣壓較一致。"
    if net > 0 and tot > 0:
        return base + ";與現貨同步偏多。"
    return base + "。"


def chip_flow_hint(chip: dict | None, fut: dict | None = None) -> str:
    """真實三大法人買賣超(現貨流量)+ 台指期留倉(期貨部位)→ 白話籌碼提示。無資料回空字串。"""
    days = (chip or {}).get("days") or []
    text = ""
    if days:
        latest = days[0]
        f, t, tot = (latest.get("foreign", 0) / OKU,
                     latest.get("trust", 0) / OKU,
                     latest.get("total", 0) / OKU)
        streak = 0
        for d in days:
            x = d.get("total", 0)
            if x == 0 or (x < 0) != (tot < 0):
                break
            streak += 1
        side = "賣超" if tot < 0 else ("買超" if tot > 0 else "持平")
        text = f"💰 法人籌碼:外資{f:+.0f}億、投信{t:+.0f}億,三大法人{side}{abs(tot):.0f}億"
        if tot < 0 and streak >= 2:
            text += f";已連{streak}日站賣方,留意獲利了結賣壓。"
        elif tot < 0:
            text += ";由買轉賣,留意獲利了結。"
        elif tot > 0 and streak >= 2:
            text += f";連{streak}日買超,暫無獲利了結跡象。"
        else:
            text += "。"
    fut_line = _futures_stance_line(chip, fut)
    if fut_line:
        text = (text + "\n" + fut_line) if text else fut_line
    return text


# ---------------------------------------------------------------------------
# 戰略報告
# ---------------------------------------------------------------------------

def build_line_message(report: dict, chip_hint: str = "") -> str:
    """把報告整理成一則精簡的 LINE 文字訊息(標題、法人籌碼提示、盲點/領先指標)。"""
    lines = [
        f"🌐 全球政經戰略報告 {report.get('report_date', '')}",
        f"主題:{report.get('topic', '')}",
    ]
    if chip_hint:
        lines += ["", chip_hint]
    kpi = report.get("strategic_analysis", {}).get("blind_spots_and_kpi", "").strip()
    if kpi:
        lines += ["", "🎯 盲點與領先指標:", kpi[:400] + ("..." if len(kpi) > 400 else "")]
    lines += ["", f"(白話文來源:{report.get('dictionary_source', '—')})"]
    msg = "\n".join(lines)
    if len(msg) > LINE_TEXT_LIMIT:
        msg = msg[:LINE_TEXT_LIMIT] + "\n...(訊息過長已截斷)"
    return msg


def notify_line(report: dict, chip_hint: str = "") -> None:
    """透過 LINE Messaging API push 推送報告摘要(可附帶法人籌碼提示)。"""
    _push_line_text(build_line_message(report, chip_hint))


# ---------------------------------------------------------------------------
# 國際盤預警
# ---------------------------------------------------------------------------

def lead_market_drops(intl: dict) -> list[dict]:
    """取『時間差領先』市場(美股指數/美股期貨/台指期夜盤)的大跌清單。"""
    return [d for d in intl.get("drops", []) if d.get("lead_type") in LEAD_DROP_TYPES]


def build_intl_alert_line_message(intl: dict) -> str:
    """把國際盤快報整理成一則精簡 LINE 文字(真實報價數字 + Gemini 美股/台股研判)。

    每天都推:有領先市場大跌(或 AI 判警戒)→『🚨 國際盤大跌預警』;平靜 →『🌅 國際盤快報』。
    """
    lead = lead_market_drops(intl)
    alarm = bool(lead) or intl.get("alert_level") == "警戒"
    title = "🚨 國際盤大跌預警" if alarm else "🌅 國際盤快報"
    lines = [f"{title} {intl.get('report_date', '')}"]

    root_cause = (intl.get("root_cause") or "").strip()
    if root_cause:
        lines.append(f"🔥 主因:{root_cause}")

    lines.append(f"警示級別:{intl.get('alert_level', '—')}")
    if intl.get("summary"):
        lines.append(intl["summary"])

    if lead:
        lines += ["", "📉 大跌(領先台股):"]
        for d in lead:
            lines.append(
                f"・{d.get('name', '')} {d.get('change_pct', 0):+.2f}%({d.get('lead_type', '')})"
            )
    others = [d for d in intl.get("drops", []) if d.get("lead_type") not in LEAD_DROP_TYPES]
    if others:
        lines.append(
            "・(同步盤)"
            + "、".join(f"{d.get('name', '')} {d.get('change_pct', 0):+.2f}%" for d in others)
        )

    interp = intl.get("interpretation", [])
    if interp:
        lines += ["", "🧭 利空原因:"]
        for it in interp[:2]:
            mk = it.get("market", "")
            cause = (it.get("cause", "") or "").strip()
            cause_s = cause[:80] + ("…" if len(cause) > 80 else "")
            lines.append(f"・{mk}:{cause_s}" if mk else f"・{cause_s}")

    us = intl.get("us_view", {})
    if us:
        lines += ["", f"🇺🇸 美股:{us.get('direction', '—')}"]
        focus = us.get("focus", [])
        if focus:
            lines.append("盯:" + "、".join(str(s) for s in focus[:2]))

    imp = intl.get("tw_impact", {})
    if imp:
        lines += ["", f"🇹🇼 台股:{imp.get('direction', '—')}"]
        reason = (imp.get("reason", "") or "").strip()
        if reason:
            lines.append(reason[:100] + ("…" if len(reason) > 100 else ""))
        sectors = imp.get("sectors", [])
        if sectors:
            lines.append("族群:" + "、".join(str(s) for s in sectors[:3]))

    lines += ["", "⚠️ 真實報價 + AI 研判,僅供參考,非投資建議"]
    msg = "\n".join(lines)
    if len(msg) > LINE_TEXT_LIMIT:
        msg = msg[:LINE_TEXT_LIMIT] + "\n...(訊息過長已截斷)"
    return msg


def notify_line_intl_alert(intl: dict) -> None:
    """國際盤快報 → 每天推一則 LINE(含美股/台股看法;大跌時標題自動升級)。"""
    _push_line_text(build_intl_alert_line_message(intl))


# ---------------------------------------------------------------------------
# 法人事件預告
# ---------------------------------------------------------------------------

def build_chip_events_line_message(events: list[dict], today: str) -> str:
    """把『進入窗口的可預測法人賣壓事件』整理成一則精簡 LINE 文字。"""
    lines = [f"📅 法人事件預告 {today}", "未來數日已知的籌碼/賣壓窗口:"]
    for e in events:
        td = e.get("trading_days_until", 0)
        when = "今日" if td == 0 else f"約 {td} 個交易日後"
        lines.append(f"・{e.get('title', '')}({e.get('date', '')},{when})")
        if e.get("detail"):
            lines.append(f"　{e['detail']}")
    lines += ["", "⚠️ 日期為慣例/曆法推算,實際以官方公告為準;僅供參考,非投資建議"]
    msg = "\n".join(lines)
    if len(msg) > LINE_TEXT_LIMIT:
        msg = msg[:LINE_TEXT_LIMIT] + "\n...(訊息過長已截斷)"
    return msg


def notify_line_chip_events(events: list[dict], today: str) -> None:
    """可預測法人事件進入窗口 → 推一則 LINE 預告(沿用 Messaging API push)。"""
    _push_line_text(build_chip_events_line_message(events, today))


def load_pushed_events() -> list[str]:
    """讀已推播過的法人事件 id 清單(防 LINE 洗版);無檔回空。"""
    try:
        return list(json.loads(
            paths.CHIP_PUSHED_STATE.read_text(encoding="utf-8")).get("ids", []))
    except Exception:  # noqa: BLE001 — 無檔/壞檔 → 視為尚未推過
        return []


def save_pushed_events(ids: list[str]) -> None:
    """寫回已推播事件 id 清單(只保留最近 60 筆,避免無限增長)。"""
    _save_json(paths.CHIP_PUSHED_STATE, {"ids": ids[-60:]})


# ---------------------------------------------------------------------------
# 多重賣壓共振
# ---------------------------------------------------------------------------

def build_confluence_line_message(conf: dict, today: str) -> str:
    """多重賣壓共振 → 一則白話 LINE(列出哪幾股力量 + 真實數字)。"""
    lines = [f"🔴 多重賣壓共振預警 {today}"]
    us = conf.get("us_drops", [])
    if us:
        lines.append("美股大跌:" + "、".join(
            f"{d.get('name', '')} {d.get('change_pct', 0):+.1f}%" for d in us[:3]))
    lines.append(f"共振力量({conf.get('count', 0)}/4):")
    for f in conf.get("forces", []):
        lines.append(f"・{f.get('detail', '')}")
    lines += ["", "→ 非單一利空,多股賣壓疊加,留意修正延續。",
              "⚠️ 真實數據判定,僅供參考,非投資建議"]
    msg = "\n".join(lines)
    return msg[:LINE_TEXT_LIMIT] if len(msg) > LINE_TEXT_LIMIT else msg


def notify_line_confluence(conf: dict, today: str) -> None:
    """推一則多重賣壓共振 LINE 預警。"""
    _push_line_text(build_confluence_line_message(conf, today))


# ---------------------------------------------------------------------------
# 個股盯盤
# ---------------------------------------------------------------------------

def build_watch_line_message(today: str, summaries: list[dict],
                             new_revenue: list[dict],
                             tech_lines: dict[str, str] | None = None,
                             chip_lines: dict[str, str] | None = None,
                             vcp_lines: dict[str, str] | None = None,
                             new_eps: list[dict] | None = None) -> str:
    """組個股盯盤的 LINE 文字:消息面逐檔(+技術面、籌碼面、VCP)+ 新月營收 + 新季報 EPS(若有)。"""
    tech_lines = tech_lines or {}
    chip_lines = chip_lines or {}
    vcp_lines = vcp_lines or {}
    lines = [f"📈 個股盯盤 {today}", ""]
    for s in summaries:
        ticker = str(s.get("ticker", "")).strip()
        name = (s.get("name") or "").strip()
        senti = s.get("sentiment", "中性")
        summary = (s.get("summary", "") or "").strip()
        head = f"【{name} {ticker}】".replace("  ", " ") if name else f"【{ticker}】"
        lines.append(f"{head} {senti}")
        lines.append(summary or "近期無重大消息。")
        tline = tech_lines.get(ticker)
        if tline:
            lines.append(tline)
        cline = chip_lines.get(ticker)
        if cline:
            lines.append(cline)
        vline = vcp_lines.get(ticker)
        if vline:
            lines.append(vline)
        lines.append("")
    if new_revenue:
        lines.append("🧾 新財報(月營收):")
        for r in new_revenue:
            yoy = r.get("yoy_pct")
            mom = r.get("mom_pct")
            yoy_s = f"年增 {yoy:+.1f}%" if isinstance(yoy, (int, float)) else "年增 —"
            mom_s = f"月增 {mom:+.1f}%" if isinstance(mom, (int, float)) else "月增 —"
            nm = (r.get("name") or "").strip()
            lines.append(
                f"・{nm} {r.get('ticker')}｜{r.get('period')} 營收 "
                f"{r.get('month_rev', 0) / OKU:.0f}億,{yoy_s}、{mom_s}"
            )
        lines.append("")
    if new_eps:
        lines.append("📊 新季報(EPS):")
        for e in new_eps:
            eps = e.get("eps")
            prior = e.get("prior_eps")
            ticker = e.get("ticker", "")
            period = e.get("period", "")
            eps_s = f"EPS {eps:+.2f}元" if isinstance(eps, (int, float)) else "EPS —"
            if isinstance(eps, (int, float)) and isinstance(prior, (int, float)):
                chg_s = f",較前期 {eps - prior:+.2f}元"
            else:
                chg_s = ""
            lines.append(f"・{ticker}｜{period} {eps_s}{chg_s}")
        lines.append("")
    lines.append("(僅供參考,非投資建議。指令:加/刪/清單)")
    msg = "\n".join(lines).rstrip()
    if len(msg) > LINE_TEXT_LIMIT:
        msg = msg[:LINE_TEXT_LIMIT] + "\n...(訊息過長已截斷)"
    return msg
