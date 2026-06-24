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
LINE_REPLY_ENDPOINT = "https://api.line.me/v2/bot/message/reply"
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
        if isinstance(doc, dict) and isinstance(doc.get("stocks"), list):
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
    if low in ("id", "我的id", "myid"):
        return f"你的 userId:\n{user_id}\n(設成 repo Secret 的 LINE_WATCH_TO 即可只推給你)"

    action, arg = parse_command(text)
    if action == "list":
        doc, _ = gh_load()
        return format_list(doc)
    if action in ("add", "remove"):
        doc, sha = gh_load()
        if action == "add":
            ticker = normalize_ticker(arg)
            name = arg.replace(ticker, "").strip() if ticker else ""
            changed, msg = add_stock(doc, arg, name)
        else:
            changed, msg = remove_stock(doc, arg)
        if changed and not gh_save(doc, f"watchlist: {action} {arg.strip()}", sha):
            return "清單更新失敗(寫回 repo 出錯),請稍後再試。"
        return msg + "\n\n" + format_list(doc)
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
        # 白名單可逗號/空白分隔多個 userId(你+家人共用清單);留空 = 不限制。
        allow_set = {u for u in re.split(r"[,\s]+", os.environ.get("WATCH_ALLOW_USER", "")) if u}
        for ev in events:
            if ev.get("type") != "message" or ev.get("message", {}).get("type") != "text":
                continue
            user_id = ev.get("source", {}).get("userId", "")
            text = ev.get("message", {}).get("text", "")
            _log(f"收到訊息 userId={user_id}:{text!r}")
            if allow_set and user_id and user_id not in allow_set:
                _log("非允許名單的 userId,忽略")
                continue
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
