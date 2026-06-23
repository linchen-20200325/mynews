"""watchlist.py — 個股盯盤清單(watchlist)的單一真相源(SSOT)。

清單存 ``watchlist.json``(路徑見 ``paths.WATCHLIST``),由兩端共用同一份結構:
  * scripts/nas_line_bot.py:接 LINE 訊息「加 2330 / 刪 2330 / 清單」即時編輯
    (NAS 端經 GitHub Contents API 寫回 repo)。
  * update_data.py:早上排程讀清單,逐檔抓消息面 + 月營收,推第二個 LINE bot。

清單結構:``{"stocks": [{"ticker": "2330", "name": "台積電"}, ...], "updated_at": "..."}``

設計成「純邏輯(parse/add/remove/format)與 I/O(load/save)分離」:純邏輯被兩端共用
(SSOT,避免新增/刪除規則在 NAS bot 與排程各寫一份而漂移),I/O 傳輸各自處理
(排程走本機檔、NAS bot 走 GitHub API)。

零相依(只用 stdlib + paths/tz_utils 兩個純 stdlib SSOT 模組),可被任何模組安全 import。
"""

from __future__ import annotations

import json
import re

import paths  # 檔案路徑 SSOT
import tz_utils  # 台灣時區 SSOT

# 台股代號:4~6 位數字,ETF/特別股可能帶單一英文尾(如 00940、2330、6770、00679B)。
_TICKER_RE = re.compile(r"[0-9]{4,6}[A-Z]?")

# 指令關鍵字(中英/符號皆收;startswith 比對,故較長的同義詞要排在前面避免被短的吃掉)。
_ADD_KW = ("新增", "加入", "加", "add", "+")
_DEL_KW = ("刪除", "移除", "刪", "remove", "del", "-")
_LIST_KW = ("清單", "清单", "list", "ls")


# ── 純邏輯(無 I/O,兩端共用)────────────────────────────────────────────────
def normalize_ticker(raw: str) -> str:
    """從使用者輸入抽出乾淨的台股代號;抽不到回空字串。"""
    m = _TICKER_RE.search((raw or "").upper())
    return m.group(0) if m else ""


def tickers(doc: dict) -> list[str]:
    """取清單內所有代號(已去空白)。"""
    return [
        str(s.get("ticker", "")).strip()
        for s in doc.get("stocks", [])
        if s.get("ticker")
    ]


def add_stock(doc: dict, ticker: str, name: str = "") -> tuple[bool, str]:
    """加入一檔;回 (是否有變動, 回覆訊息)。代號無效或已存在 → 不變動。"""
    t = normalize_ticker(ticker)
    if not t:
        return False, f"看不懂代號「{ticker}」,請給 4~6 位數字代號(例:2330)。"
    for s in doc.get("stocks", []):
        if str(s.get("ticker")) == t:
            return False, f"{t} 已在清單內。"
    doc.setdefault("stocks", []).append({"ticker": t, "name": (name or "").strip()})
    return True, f"✅ 已加入 {t}{(' ' + name.strip()) if name.strip() else ''}。"


def remove_stock(doc: dict, ticker: str) -> tuple[bool, str]:
    """移除一檔;回 (是否有變動, 回覆訊息)。不在清單內 → 不變動。"""
    t = normalize_ticker(ticker)
    before = len(doc.get("stocks", []))
    doc["stocks"] = [s for s in doc.get("stocks", []) if str(s.get("ticker")) != t]
    if len(doc["stocks"]) == before:
        return False, f"{t or ticker} 不在清單內。"
    return True, f"🗑️ 已移除 {t}。"


def format_list(doc: dict) -> str:
    """把清單排成一則 LINE 回覆文字。"""
    items = doc.get("stocks", [])
    if not items:
        return "目前盯盤清單是空的。傳「加 2330」加入第一檔。"
    lines = [f"📋 盯盤清單({len(items)} 檔):"]
    for s in items:
        nm = (s.get("name") or "").strip()
        lines.append(f"・{s.get('ticker')}{('  ' + nm) if nm else ''}")
    lines.append("")
    lines.append("指令:加 2330 / 刪 2330 / 清單")
    return "\n".join(lines)


def parse_command(text: str) -> tuple[str, str]:
    """解析使用者訊息 → (action, arg);action ∈ {'add','remove','list','help'}。"""
    t = (text or "").strip()
    low = t.lower()
    for kw in _ADD_KW:
        if low.startswith(kw.lower()):
            return "add", t[len(kw):].strip()
    for kw in _DEL_KW:
        if low.startswith(kw.lower()):
            return "remove", t[len(kw):].strip()
    if low in _LIST_KW:
        return "list", ""
    return "help", t


def help_text() -> str:
    """看不懂指令時的引導訊息。"""
    return (
        "個股盯盤指令:\n"
        "・加 2330(把台積電加入清單)\n"
        "・刪 2330(從清單移除)\n"
        "・清單(列出目前盯盤清單)\n\n"
        "每天早上會推你清單內個股的消息面 AI 總結,有新月營收也會通知。"
    )


# ── 本機檔 I/O(排程端用;NAS bot 走 GitHub API,不用這兩個)─────────────────
def load() -> dict:
    """讀本機 watchlist.json;無檔/壞檔 → 回空清單(不拋例外)。"""
    try:
        doc = json.loads(paths.WATCHLIST.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("stocks"), list):
            doc.setdefault("updated_at", "")
            return doc
    except Exception:  # noqa: BLE001 — 無檔/壞檔 → 視為空清單
        pass
    return {"stocks": [], "updated_at": ""}


def dumps(doc: dict) -> str:
    """序列化(蓋上台灣時區更新時間);回傳 JSON 字串。兩端寫入前都先過這裡,格式一致。"""
    doc["updated_at"] = tz_utils.taiwan_now().strftime("%Y-%m-%d %H:%M")
    return json.dumps(doc, ensure_ascii=False, indent=2)


def save(doc: dict) -> str:
    """寫回本機 watchlist.json;回傳寫入的 JSON 字串。"""
    payload = dumps(doc)
    paths.WATCHLIST.write_text(payload, encoding="utf-8")
    return payload
