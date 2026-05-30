"""github_store.py — 透過 GitHub Contents API 把資料檔直接 commit 回 repo。

讓 Streamlit Cloud(檔案系統唯讀)也能「一鍵存檔」:把 JSON 內容經 API 寫回 repo,
取代手動下載再上傳。

需要的設定(環境變數或 Streamlit Secrets,切勿寫進程式或進版控):
  - GITHUB_TOKEN : 具該 repo `contents:write` 權限的權杖
                   (fine-grained PAT 限定本 repo,或 classic PAT 勾 repo)
  - GITHUB_REPO  : "owner/repo",預設 "linchen-20200325/mynews"
  - GITHUB_BRANCH: 目標分支,預設 "main"

安全:Token 等同寫入權限,只放 Secrets;本模組不記錄、不回傳 token。
"""

from __future__ import annotations

import base64
import json
import os

API = "https://api.github.com"
DEFAULT_REPO = "linchen-20200325/mynews"
DEFAULT_BRANCH = "main"


def _cfg(get_secret=None) -> dict:
    """彙整設定:優先環境變數,其次 get_secret(name) 回呼(Streamlit secrets)。"""
    def val(name: str, default: str = "") -> str:
        v = os.environ.get(name)
        if not v and get_secret:
            try:
                v = get_secret(name)
            except Exception:  # noqa: BLE001
                v = None
        return (str(v).strip() if v else default)

    return {
        "token": val("GITHUB_TOKEN"),
        "repo": val("GITHUB_REPO", DEFAULT_REPO),
        "branch": val("GITHUB_BRANCH", DEFAULT_BRANCH),
    }


def is_configured(get_secret=None) -> bool:
    return bool(_cfg(get_secret)["token"])


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def commit_file(
    path: str, content: str, message: str, get_secret=None
) -> tuple[bool, str]:
    """建立/更新 repo 內 ``path`` 檔案(同名即覆蓋)。回傳 (成功, 訊息/commit 連結)。"""
    import requests

    cfg = _cfg(get_secret)
    if not cfg["token"]:
        return False, "未設定 GITHUB_TOKEN(請在 Streamlit Secrets 加上)。"

    repo, branch = cfg["repo"], cfg["branch"]
    url = f"{API}/repos/{repo}/contents/{path}"
    headers = _headers(cfg["token"])

    # 取得既有檔案的 sha(更新時必填;不存在則略過)
    sha = None
    try:
        r = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception as exc:  # noqa: BLE001
        return False, f"讀取現有檔案失敗:{exc}"

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(url, headers=headers, data=json.dumps(payload), timeout=30)
    except Exception as exc:  # noqa: BLE001
        return False, f"寫入失敗:{exc}"

    if r.status_code in (200, 201):
        commit = r.json().get("commit", {})
        return True, commit.get("html_url", f"已 commit 到 {repo}@{branch}:{path}")
    if r.status_code == 401:
        return False, "GITHUB_TOKEN 無效或過期(401)。"
    if r.status_code == 403:
        return False, "權限不足(403):Token 需有此 repo 的 contents:write。"
    if r.status_code == 404:
        return False, f"找不到 repo/路徑(404):{repo} / {path};檢查 GITHUB_REPO 與 Token 範圍。"
    return False, f"GitHub 回應 {r.status_code}:{r.text[:200]}"
