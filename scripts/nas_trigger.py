#!/usr/bin/env python3
"""NAS 主觸發:每日打 GitHub API 觸發 daily_update workflow(workflow_dispatch)。

GitHub 自家排程器在尖峰常把清晨班次整批丟棄(6/10、6/11 早上就是這樣漏報);
改由 24h 開機的 NAS 當「主觸發」最可靠,GitHub 內建排程退為備援。

防重複:NAS 走 workflow_dispatch → 一定完整跑並推一次 LINE;之後 GitHub schedule
班次(update_data.py 內建守門)看到今日報告已存在就自動略過,故不會雙推。

Token:fine-grained PAT,僅授權本 repo 的「Actions: Read and write」。
  存成單獨檔案(chmod 600)或放環境變數,切勿進 git。

用法(擇一提供 token):
  GITHUB_TOKEN=github_pat_xxx       python3 nas_trigger.py
  GITHUB_TOKEN_FILE=/path/to/token  python3 nas_trigger.py

Synology DSM > 控制台 > 任務排程 > 新增 > 排定的任務 > 使用者定義指令碼,
每天 05:25(比 GitHub cron 05:30 早 5 分鐘搶頭香),指令:
  GITHUB_TOKEN_FILE=/volume1/homes/<you>/.mynews_gh_token \\
    /usr/bin/python3 /volume1/.../scripts/nas_trigger.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

OWNER = "linchen-20200325"
REPO = "mynews"
WORKFLOW = "daily_update.yml"
REF = "main"  # dispatch 讀此分支的 workflow 檔;job 內亦 checkout main
RETRIES = 4


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


def main() -> int:
    token = _token()
    url = (f"https://api.github.com/repos/{OWNER}/{REPO}"
           f"/actions/workflows/{WORKFLOW}/dispatches")
    body = json.dumps({"ref": REF}).encode()
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
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


if __name__ == "__main__":
    sys.exit(main())
