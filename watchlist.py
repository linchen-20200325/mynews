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


# ── per-user 多使用者結構(每個 userId 一份獨立清單)────────────────────────────
# 結構演進:舊扁平 ``{"stocks":[...]}`` → 新 ``{"users":{"U…":{"stocks":[...]}}}``。
# 兩格式並存相容:排程端對舊格式維持單一推播;首位下指令者觸發「無損遷移」把既有
# 扁平清單併入其名下。per-user 函式只負責解析出對應 bucket,實際增刪/排版仍復用上方
# add_stock / remove_stock / format_list 純邏輯(SSOT,規則只定義一次)。
def is_per_user(doc: dict) -> bool:
    """doc 是否已是 per-user 結構(含 users 鍵)。"""
    return isinstance(doc.get("users"), dict)


def user_ids(doc: dict) -> list[str]:
    """取所有有清單的 userId;非 per-user → 空。"""
    return list((doc.get("users") or {}).keys())


def user_stocks(doc: dict, user_id: str) -> list[dict]:
    """取某 userId 的個股清單;無此人或非 per-user → 空清單。"""
    return ((doc.get("users") or {}).get(user_id) or {}).get("stocks", []) or []


def tickers_for(doc: dict, user_id: str) -> list[str]:
    """取某 userId 清單內所有代號(已去空白)。"""
    return [str(s.get("ticker", "")).strip()
            for s in user_stocks(doc, user_id) if s.get("ticker")]


def ensure_user_bucket(doc: dict, user_id: str) -> dict:
    """確保 doc 為 per-user 結構,就地建立並回傳該 userId 的 bucket ``{"stocks":[...]}``。

    無損遷移:首次寫入時把舊扁平頂層 stocks 併入「此使用者」名下(避免既有清單遺失),
    併入後移除頂層 stocks;重複代號不重覆加入。
    """
    users = doc.get("users")
    if not isinstance(users, dict):
        users = doc["users"] = {}
    legacy = doc.pop("stocks", None)
    bucket = users.setdefault(user_id, {"stocks": []})
    if not isinstance(bucket.get("stocks"), list):
        bucket["stocks"] = []
    if isinstance(legacy, list) and legacy:
        existing = {str(s.get("ticker")) for s in bucket["stocks"]}
        for s in legacy:
            if str(s.get("ticker")) not in existing:
                bucket["stocks"].append(s)
    return bucket


def add_stock_for(doc: dict, user_id: str, ticker: str, name: str = "") -> tuple[bool, str]:
    """加入一檔到某 userId 的清單(復用 add_stock);觸發無損遷移。"""
    return add_stock(ensure_user_bucket(doc, user_id), ticker, name)


def remove_stock_for(doc: dict, user_id: str, ticker: str) -> tuple[bool, str]:
    """從某 userId 的清單移除一檔(復用 remove_stock);觸發無損遷移。"""
    return remove_stock(ensure_user_bucket(doc, user_id), ticker)


def format_list_for(doc: dict, user_id: str) -> str:
    """把某 userId 的清單排成 LINE 回覆(復用 format_list);唯讀,不遷移。"""
    return format_list((doc.get("users") or {}).get(user_id) or {"stocks": []})


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
