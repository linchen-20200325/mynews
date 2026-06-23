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
  WATCH_BOT_PORT     監聽埠,預設 8080(對外經 Cloudflare Tunnel / 路由器轉發到此埠)
  WATCH_ALLOW_USER   (選填)只接受此 userId 的指令,其餘忽略(防陌生人亂改清單)

啟動:
  LINE_WATCH_TOKEN=xxx LINE_WATCH_SECRET=yyy GITHUB_TOKEN_FILE=/path/token \\
    python3 scripts/nas_line_bot.py
LINE Developers Console → Messaging API → Webhook URL 填 https://<你的網域>/callback,
並開啟「Use webhook」。

註:本檔在 NAS 常駐執行,刻意只用 stdlib + 專案的 watchlist.py(加/刪/解析的 SSOT 純邏輯,
    避免規則在兩端各寫一份而漂移)。GitHub/LINE I/O 屬本服務專屬,就地實作。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 讓本檔(在 scripts/ 底下)能 import 專案根目錄的 watchlist.py(加/刪/解析 SSOT)。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import watchlist  # noqa: E402 — 需先補 sys.path 才能匯入

GITHUB_API = "https://api.github.com"
LINE_REPLY_ENDPOINT = "https://api.line.me/v2/bot/message/reply"
WATCHLIST_PATH = "watchlist.json"  # 對應 paths.WATCHLIST(repo 內路徑)


def _log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _gh_cfg() -> tuple[str, str, str]:
    """回 (token, repo, branch)。token 取自 GITHUB_TOKEN 或 GITHUB_TOKEN_FILE。"""
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


def gh_load() -> tuple[dict, str | None]:
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


def gh_save(doc: dict, message: str, sha: str | None) -> bool:
    """把清單寫回 repo 內 watchlist.json(經 watchlist.dumps 統一格式)。回成功與否。"""
    token, repo, branch = _gh_cfg()
    url = f"{GITHUB_API}/repos/{repo}/contents/{WATCHLIST_PATH}"
    content = watchlist.dumps(doc)  # 蓋台灣時區更新時間,與排程端格式一致(SSOT)
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
    """用第二個 bot 的 token reply 一則文字。"""
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
    """解析一則使用者訊息 → 回覆文字(必要時改 watchlist 並寫回 repo)。"""
    low = (text or "").strip().lower()
    if low in ("id", "我的id", "myid"):
        return f"你的 userId:\n{user_id}\n(設成 repo Secret 的 LINE_WATCH_TO 即可只推給你)"

    action, arg = watchlist.parse_command(text)
    if action == "list":
        doc, _ = gh_load()
        return watchlist.format_list(doc)
    if action in ("add", "remove"):
        doc, sha = gh_load()
        if action == "add":
            ticker = watchlist.normalize_ticker(arg)
            name = arg.replace(ticker, "").strip() if ticker else ""
            changed, msg = watchlist.add_stock(doc, arg, name)
        else:
            changed, msg = watchlist.remove_stock(doc, arg)
        if changed and not gh_save(doc, f"watchlist: {action} {arg.strip()}", sha):
            return "清單更新失敗(寫回 repo 出錯),請稍後再試。"
        return msg + "\n\n" + watchlist.format_list(doc)
    return watchlist.help_text()


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
        allow = os.environ.get("WATCH_ALLOW_USER", "").strip()
        for ev in events:
            if ev.get("type") != "message" or ev.get("message", {}).get("type") != "text":
                continue
            user_id = ev.get("source", {}).get("userId", "")
            text = ev.get("message", {}).get("text", "")
            _log(f"收到訊息 userId={user_id}:{text!r}")
            if allow and user_id and user_id != allow:
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
