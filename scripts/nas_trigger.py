#!/usr/bin/env python3
"""NAS 主力觸發:NAS 當「每日第一發送」,GitHub schedule 退為兜底備援。

GitHub 自家排程器在尖峰常把清晨班次整批丟棄(6/10、6/11、6/16 早上漏 LINE 主因);
故改由 24h 開機的 NAS 當主力:每天台灣 06:00(資料齊備時)以 workflow_dispatch 發第一槍 ——
dispatch 是 API 直發、不受 GitHub 排程丟棄影響,且繞過 update_data.py 的 schedule 去重守門,
保證完整跑並推 LINE。GitHub schedule(06:40、07:30)只在 NAS 沒開機/沒網路時補位。

模式維持 backup(先查再發):NAS 是當天最早的班次,先查今日通常無成功/進行中 → 直接發;
但若有殘留的手動 run 或前一班仍在跑,先查就能避免撞車雙推。等於「最早且唯一」的第一發。
(TRIGGER_MODE=always 則不查、每天硬發;一般用預設 backup 即可。)

Token:fine-grained PAT,僅授權本 repo 的「Actions: Read and write」。
  存成單獨檔案(chmod 600)或放環境變數,切勿進 git。

用法(擇一提供 token):
  GITHUB_TOKEN=github_pat_xxx       python3 nas_trigger.py
  GITHUB_TOKEN_FILE=/path/to/token  python3 nas_trigger.py

Synology DSM > 控制台 > 任務排程 > 新增 > 排定的任務 > 使用者定義指令碼,
每天 06:00(主力第一發),指令:
  GITHUB_TOKEN_FILE=/volume1/homes/<you>/.mynews_gh_token \\
    /usr/bin/python3 /volume1/.../scripts/nas_trigger.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

OWNER = "linchen-20200325"
REPO = "mynews"
WORKFLOW = "daily_update.yml"
REF = "main"  # dispatch 讀此分支的 workflow 檔;job 內亦 checkout main
RETRIES = 4
API = f"https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}"
# 進行中的執行狀態(任一存在就別補,避免和 GitHub 撞車雙推)
_PENDING = {"queued", "in_progress", "requested", "waiting", "pending"}


def _log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    path = os.environ.get("MYNEWS_TRIGGER_LOG")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass  # 記不了 log 不該害觸發失敗


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    fp = os.environ.get("GITHUB_TOKEN_FILE", "").strip()
    if fp and os.path.isfile(fp):
        with open(fp, encoding="utf-8") as fh:
            return fh.read().strip()
    _log("ERROR 找不到 token:請設 GITHUB_TOKEN 或 GITHUB_TOKEN_FILE")
    sys.exit(2)


def _headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# 註:本檔在 NAS 上單檔執行、刻意零專案相依(只用 stdlib),故不共用 tz_utils,
#     UTC+8 邏輯在此自帶——這是經評估的 SSOT 例外,非疏漏。
def _today_tw() -> str:
    """台灣(UTC+8)今日 YYYY-MM-DD;報告日期亦以台灣時區計,兩邊一致。"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


def _run_date_tw(created_at: str) -> str:
    """GitHub run 的 created_at(UTC ISO)→ 台灣日期 YYYY-MM-DD。"""
    dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d")


def _should_skip(token: str) -> bool:
    """今日已有成功、或有正在跑的執行 → 回 True(不補)。查詢失敗則保守回 False(照補)。"""
    url = f"{API}/runs?per_page=30"
    req = urllib.request.Request(url, headers=_headers(token), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            runs = json.loads(resp.read()).get("workflow_runs", [])
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        _log(f"WARN 查詢今日狀態失敗:{exc} — 保守起見照常補觸發")
        return False
    today = _today_tw()
    for r in runs:
        status = (r.get("status") or "").lower()
        if status in _PENDING:
            _log(f"今日已有執行進行中(status={status})— NAS 不補,避免撞車")
            return True
        if r.get("created_at") and _run_date_tw(r["created_at"]) == today \
                and r.get("conclusion") == "success":
            _log(f"今日({today})已有成功執行 — NAS 不補")
            return True
    _log(f"今日({today})尚無成功/進行中執行 — NAS 補觸發")
    return False


def _dispatch(token: str) -> int:
    url = f"{API}/dispatches"
    body = json.dumps({"ref": REF}).encode()
    headers = {**_headers(token), "Content-Type": "application/json"}
    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 204:  # GitHub 成功觸發回 204 No Content
                    _log(f"OK 已觸發 {WORKFLOW}(ref={REF})")
                    return 0
                _log(f"WARN 非預期狀態 {resp.status}")
        except urllib.error.HTTPError as exc:
            # 4xx 多為 token 權限/路徑錯,重試無益 → 立即停機(CLAUDE.md §5)
            if 400 <= exc.code < 500:
                _log(f"ERROR HTTP {exc.code}(401/403=token 權限不足;"
                     "404=repo/workflow/ref 不存在)— 不重試")
                return 1
            _log(f"WARN HTTP {exc.code}(伺服器端,將重試)")
        except (urllib.error.URLError, TimeoutError) as exc:
            _log(f"WARN 連線問題:{exc}(將重試)")
        if attempt < RETRIES:
            time.sleep(min(2 ** attempt, 16))  # 退避 2/4/8/16 秒
    _log(f"ERROR 重試 {RETRIES} 次仍失敗")
    return 1


def main() -> int:
    token = _token()
    mode = os.environ.get("TRIGGER_MODE", "backup").strip().lower()
    if mode == "backup" and _should_skip(token):
        return 0  # 真備援:今日已成功/進行中 → 不補
    return _dispatch(token)


if __name__ == "__main__":
    sys.exit(main())
