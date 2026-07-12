#!/usr/bin/env python3
"""NAS 常駐 LINE webhook:讓你在 LINE 上即時編輯個股盯盤清單(加/刪/清單)。

為什麼要這支:整套主流程是 GitHub Actions「單向排程推播」,沒有任何能「接收」LINE
訊息的伺服器。要在 LINE 上加/刪股票,必須有一台常駐在線的程式接 LINE webhook ——
這支就跑在 24h 開機的 NAS 上(與 scripts/nas_trigger.py 同一台),收到你的訊息就改
watchlist.json(經 GitHub Contents API 寫回 repo),隔天早上排程讀清單推給你。

資料流:
  你在 LINE 打「加 2330」→ LINE 平台 POST 到本服務 → 驗簽 → 改 watchlist.json
  (GitHub API)→ reply 回你目前清單。canonical 清單在 repo,排程端讀的是同一份。

指令(傳給「個股盯盤」這個第二個 bot):
  加 2330 / 加 2330 台積電   刪 2330   清單   id(回你的 userId,拿去設 LINE_WATCH_TO)

設定(環境變數;切勿寫進程式或進版控):
  LINE_WATCH_TOKEN   第二個 bot 的 Channel access token(用來 reply)
  LINE_WATCH_SECRET  第二個 bot 的 Channel secret(驗 X-Line-Signature)
  GITHUB_TOKEN 或 GITHUB_TOKEN_FILE   具本 repo contents:write 的 PAT(改 watchlist.json)
  GITHUB_REPO        預設 linchen-20200325/mynews;GITHUB_BRANCH 預設 main
  WATCH_BOT_PORT     監聽埠,預設 8080(對外經 DSM 反向代理 / 路由器轉發到此埠)
  WATCH_ALLOW_USER   (選填)只接受這些 userId 的指令,其餘忽略(防陌生人亂改清單)。
                     可逗號/空白分隔多個(如你+家人共用同一份清單);留空 = 不限制。

啟動:
  LINE_WATCH_TOKEN=xxx LINE_WATCH_SECRET=yyy GITHUB_TOKEN_FILE=/path/token \\
    /bin/python3 nas_line_bot.py
LINE Developers Console → Messaging API → Webhook URL 填 https://<你的網域>/callback,
並開啟「Use webhook」。

註:本檔在 NAS 上單檔常駐執行,刻意零專案相依(只用 stdlib),故不 import 專案的
    watchlist.py,而是就地內嵌「加/刪/解析」純邏輯 —— 這是經評估的 SSOT 例外(同
    scripts/nas_trigger.py 自帶台灣時區的理由):NAS 端常只放單檔、無完整 repo。
    清單結構與下列純邏輯必須與 repo 的 watchlist.py 保持一致,改其一要同步另一。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GITHUB_API = "https://api.github.com"
LINE_REPLY_ENDPOINT = "https://api.line.me/v2/bot/message/reply"  # 刻意重複:NAS 單檔零相依,無法 import line_notify
WATCHLIST_PATH = "watchlist.json"  # 對應 repo 內 paths.WATCHLIST

# ── 內嵌的 watchlist 純邏輯(與 repo/watchlist.py 同步;見檔頭 SSOT 例外說明)──
_TICKER_RE = re.compile(r"[0-9]{4,6}[A-Z]?")  # 台股代號 4~6 碼,ETF/特別股可帶單一英文尾
_ADD_KW = ("新增", "加入", "加", "add", "+")
_DEL_KW = ("刪除", "移除", "刪", "remove", "del", "-")
_LIST_KW = ("清單", "清单", "list", "ls")


def normalize_ticker(raw: str) -> str:
    m = _TICKER_RE.search((raw or "").upper())
    return m.group(0) if m else ""


def parse_command(text: str):
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


def add_stock(doc: dict, ticker: str, name: str = ""):
    t = normalize_ticker(ticker)
    if not t:
        return False, f"看不懂代號「{ticker}」,請給 4~6 位數字代號(例:2330)。"
    for s in doc.get("stocks", []):
        if str(s.get("ticker")) == t:
            return False, f"{t} 已在清單內。"
    doc.setdefault("stocks", []).append({"ticker": t, "name": (name or "").strip()})
    return True, f"✅ 已加入 {t}{(' ' + name.strip()) if name.strip() else ''}。"


def remove_stock(doc: dict, ticker: str):
    t = normalize_ticker(ticker)
    before = len(doc.get("stocks", []))
    doc["stocks"] = [s for s in doc.get("stocks", []) if str(s.get("ticker")) != t]
    if len(doc["stocks"]) == before:
        return False, f"{t or ticker} 不在清單內。"
    return True, f"🗑️ 已移除 {t}。"


def format_list(doc: dict) -> str:
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


def help_text() -> str:
    return (
        "個股盯盤指令:\n"
        "・加 2330(把台積電加入清單)\n"
        "・刪 2330(從清單移除)\n"
        "・清單(列出目前盯盤清單)\n\n"
        "每天早上會推你清單內個股的消息面 AI 總結,有新月營收也會通知。"
    )


def dumps(doc: dict) -> str:
    """序列化(蓋台灣 UTC+8 更新時間);與 repo/watchlist.py 的 dumps 同格式。"""
    doc["updated_at"] = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime(
        "%Y-%m-%d %H:%M")
    return json.dumps(doc, ensure_ascii=False, indent=2)


# ── per-user 多使用者(每個 userId 一份獨立清單;與 repo/watchlist.py 同步)──────
def is_per_user(doc: dict) -> bool:
    return isinstance(doc.get("users"), dict)


def ensure_user_bucket(doc: dict, user_id: str) -> dict:
    """確保 per-user 結構並回傳此 userId 的 bucket;首位下指令者無損繼承舊扁平清單。"""
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


def add_stock_for(doc: dict, user_id: str, ticker: str, name: str = ""):
    return add_stock(ensure_user_bucket(doc, user_id), ticker, name)


def remove_stock_for(doc: dict, user_id: str, ticker: str):
    return remove_stock(ensure_user_bucket(doc, user_id), ticker)


def format_list_for(doc: dict, user_id: str) -> str:
    return format_list((doc.get("users") or {}).get(user_id) or {"stocks": []})


# ── 授權名單(存 watchlist.json 的 "allow";與 repo/watchlist.py 同步)──────────
# 管理員用 LINE 指令即時加/撤,bot 每次收訊即時讀 → 免重啟、免進 NAS。
_GRANT_KW = ("授權", "允許")
_REVOKE_KW = ("撤銷", "取消授權", "解除授權")
_ALLOWLIST_TEXT = ("名單", "授權名單", "授權清單")


def allow_list(doc: dict) -> list:
    return doc.get("allow") or []


def allowed_ids(doc: dict) -> set:
    return {str(a.get("id")) for a in allow_list(doc) if a.get("id")}


def grant(doc: dict, user_id: str, name: str = ""):
    uid = (user_id or "").strip()
    if not (uid.startswith("U") and len(uid) >= 10):
        return False, f"看不懂 userId「{user_id}」,請貼完整的 U 開頭那串。"
    lst = doc.setdefault("allow", [])
    if not isinstance(lst, list):
        lst = doc["allow"] = []
    for a in lst:
        if str(a.get("id")) == uid:
            return False, f"{(a.get('name') or uid)} 已在授權名單內。"
    lst.append({"id": uid, "name": (name or "").strip()})
    who = (name.strip() + " ") if name.strip() else ""
    return True, f"✅ 已授權 {who}{uid[:8]}…"


def revoke(doc: dict, user_id: str):
    uid = (user_id or "").strip()
    lst = doc.get("allow") or []
    before = len(lst)
    doc["allow"] = [a for a in lst if str(a.get("id")) != uid]
    if len(doc["allow"]) == before:
        return False, f"{uid[:8]}… 不在授權名單內。"
    return True, f"🗑️ 已撤銷 {uid[:8]}…"


def format_allow(doc: dict) -> str:
    lst = allow_list(doc)
    if not lst:
        return "授權名單目前是空的(此時以環境變數 WATCH_ALLOW_USER 為準)。"
    lines = [f"🔑 授權名單({len(lst)} 人):"]
    for a in lst:
        nm = (a.get("name") or "").strip()
        lines.append(f"・{(nm + '  ') if nm else ''}{a.get('id', '')}")
    lines.append("")
    lines.append("指令:授權 <userId> [名字] / 撤銷 <userId> / 名單")
    return "\n".join(lines)


def parse_admin(text: str):
    """解析管理員指令 → (action, arg);action ∈ {'grant','revoke','allowlist',''}。"""
    t = (text or "").strip()
    if t in _ALLOWLIST_TEXT:  # 先比對完整詞,避免「授權名單」被「授權」吃掉
        return "allowlist", ""
    low = t.lower()
    for kw in _GRANT_KW:
        if low.startswith(kw.lower()):
            return "grant", t[len(kw):].strip()
    for kw in _REVOKE_KW:
        if low.startswith(kw.lower()):
            return "revoke", t[len(kw):].strip()
    return "", t


def _split_id_name(arg: str):
    """把「U… 名字」拆成 (userId, name)。"""
    parts = (arg or "").split(None, 1)
    if not parts:
        return "", ""
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def _env_set(name: str) -> set:
    return {u for u in re.split(r"[,\s]+", os.environ.get(name, "")) if u}


def effective_allowed(doc: dict) -> set:
    """有效授權集合 = repo 授權名單 ∪ env WATCH_ALLOW_USER(bootstrap)。"""
    return allowed_ids(doc) | _env_set("WATCH_ALLOW_USER")


def is_user_allowed(doc: dict, user_id: str) -> bool:
    """可用『加/刪/清單』者:有效授權集合;集合為空 = 不限制(對外開放)。"""
    eff = effective_allowed(doc)
    return (not eff) or (user_id in eff)


def admin_ids() -> set:
    """管理員 = env WATCH_ADMIN_USER;未設則沿用 env WATCH_ALLOW_USER。"""
    return _env_set("WATCH_ADMIN_USER") or _env_set("WATCH_ALLOW_USER")


def is_admin(user_id: str) -> bool:
    return bool(user_id) and user_id in admin_ids()


# ── 基礎工具 ───────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _gh_cfg():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        fp = os.environ.get("GITHUB_TOKEN_FILE", "").strip()
        if fp and os.path.isfile(fp):
            token = Path(fp).read_text(encoding="utf-8").strip()
    repo = os.environ.get("GITHUB_REPO", "linchen-20200325/mynews").strip()
    branch = os.environ.get("GITHUB_BRANCH", "main").strip()
    return token, repo, branch


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_load():
    """讀 repo 內 watchlist.json,回 (清單 dict, sha)。不存在 → (空清單, None)。"""
    token, repo, branch = _gh_cfg()
    url = f"{GITHUB_API}/repos/{repo}/contents/{WATCHLIST_PATH}?ref={branch}"
    req = urllib.request.Request(url, headers=_gh_headers(token), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
        raw = base64.b64decode(payload.get("content", "")).decode("utf-8")
        doc = json.loads(raw)
        # 舊扁平(有 stocks)或 per-user(有 users)都認;否則視為空。
        if isinstance(doc, dict) and (
            isinstance(doc.get("stocks"), list) or isinstance(doc.get("users"), dict)
        ):
            return doc, payload.get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            _log(f"WARN 讀 watchlist 失敗 HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 — 無檔/壞檔 → 視為空清單
        _log(f"WARN 讀 watchlist 例外:{exc}")
    return {"stocks": [], "updated_at": ""}, None


def gh_save(doc: dict, message: str, sha) -> bool:
    """把清單寫回 repo 內 watchlist.json(經 dumps 統一格式)。回成功與否。"""
    token, repo, branch = _gh_cfg()
    url = f"{GITHUB_API}/repos/{repo}/contents/{WATCHLIST_PATH}"
    content = dumps(doc)
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=_gh_headers(token), method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status in (200, 201)
    except Exception as exc:  # noqa: BLE001
        _log(f"ERROR 寫 watchlist 失敗:{exc}")
        return False


def line_reply(reply_token: str, text: str) -> None:
    token = os.environ["LINE_WATCH_TOKEN"]
    body = json.dumps({
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:4900]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        LINE_REPLY_ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                _log(f"WARN LINE reply 非 200:{resp.status}")
    except Exception as exc:  # noqa: BLE001
        _log(f"WARN LINE reply 失敗:{exc}")


def handle_text(text: str, user_id: str) -> str:
    low = (text or "").strip().lower()
    # 「id」任何人都回(新朋友自助取得 userId,貼給管理員授權);不需被授權。
    if low in ("id", "我的id", "myid"):
        return (f"你的 userId:\n{user_id}\n"
                "(把這串貼給管理員,他用「授權 這串」就能開通你;"
                "開通後傳「加 2330」會建立你的專屬清單)")

    # 管理員指令:授權 / 撤銷 / 名單(寫回 repo,即時生效、免重啟)
    admin_action, admin_arg = parse_admin(text)
    if admin_action in ("grant", "revoke", "allowlist"):
        if not is_admin(user_id):
            return "（這是管理員指令,你沒有權限。需要的話請管理員幫你操作。）"
        if admin_action == "allowlist":
            doc, _ = gh_load()
            return format_allow(doc)
        doc, sha = gh_load()
        if admin_action == "grant":
            uid, name = _split_id_name(admin_arg)
            changed, msg = grant(doc, uid, name)
            commit = f"allow: grant {uid[:8]}"
        else:
            changed, msg = revoke(doc, admin_arg.strip())
            commit = f"allow: revoke {admin_arg.strip()[:8]}"
        if changed and not gh_save(doc, commit, sha):
            return "授權名單寫回 repo 失敗,請稍後再試。"
        return msg + "\n\n" + format_allow(doc)

    # 一般使用者指令(加/刪/清單):需被授權
    action, arg = parse_command(text)
    doc, sha = gh_load()
    if not is_user_allowed(doc, user_id):
        return ("你還沒被授權使用 🙅\n"
                "把下面這串你的 userId 貼給管理員,請他用「授權 這串」開通:\n"
                f"{user_id}")
    if action == "list":
        # per-user:列自己的清單;舊扁平格式(尚無人遷移)仍列共用清單。
        return format_list_for(doc, user_id) if is_per_user(doc) else format_list(doc)
    if action in ("add", "remove"):
        # 一律走 per-user:首位下指令者會把既有扁平清單無損遷移到自己名下。
        if action == "add":
            ticker = normalize_ticker(arg)
            name = arg.replace(ticker, "").strip() if ticker else ""
            changed, msg = add_stock_for(doc, user_id, arg, name)
        else:
            changed, msg = remove_stock_for(doc, user_id, arg)
        if changed and not gh_save(doc, f"watchlist: {action} {arg.strip()}", sha):
            return "清單更新失敗(寫回 repo 出錯),請稍後再試。"
        return msg + "\n\n" + format_list_for(doc, user_id)
    return help_text()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 靜音預設 access log(改用 _log)
        pass

    def _verify(self, body: bytes) -> bool:
        secret = os.environ.get("LINE_WATCH_SECRET", "").encode()
        digest = hmac.new(secret, body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode()
        got = self.headers.get("X-Line-Signature", "")
        return hmac.compare_digest(expected, got)

    def do_GET(self) -> None:  # 健檢用(瀏覽器/監控打一下回 200)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"mynews watch bot ok")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        # 先回 200 給 LINE(避免重送);驗簽不過則不處理事件。
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
        if not self._verify(body):
            _log("WARN 簽章驗證失敗,忽略此次 webhook")
            return
        try:
            events = json.loads(body or b"{}").get("events", [])
        except Exception:  # noqa: BLE001
            return
        # 授權判斷移到 handle_text(改讀 repo 授權名單,加人免重啟);此處只負責收事件。
        for ev in events:
            if ev.get("type") != "message" or ev.get("message", {}).get("type") != "text":
                continue
            user_id = ev.get("source", {}).get("userId", "")
            text = ev.get("message", {}).get("text", "")
            _log(f"收到訊息 userId={user_id}:{text!r}")
            try:
                reply = handle_text(text, user_id)
            except Exception as exc:  # noqa: BLE001 — 單則處理失敗不該拖垮服務
                _log(f"ERROR 處理訊息例外:{exc}")
                reply = "處理時發生錯誤,請稍後再試。"
            if ev.get("replyToken"):
                line_reply(ev["replyToken"], reply)


def main() -> int:
    for required in ("LINE_WATCH_TOKEN", "LINE_WATCH_SECRET"):
        if not os.environ.get(required):
            _log(f"ERROR 未設定 {required}")
            return 2
    if not _gh_cfg()[0]:
        _log("ERROR 未設定 GITHUB_TOKEN 或 GITHUB_TOKEN_FILE(改 watchlist 需寫入權)")
        return 2
    port = int(os.environ.get("WATCH_BOT_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    _log(f"個股盯盤 webhook 啟動,監聽 :{port}(LINE Webhook URL 指向本機 /callback)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("收到中斷,關閉服務")
    return 0


if __name__ == "__main__":
    sys.exit(main())
