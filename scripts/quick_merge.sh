#!/usr/bin/env bash
#
# quick_merge.sh — 「跳 PR 直推」例外專用工具
#
# 用途:把當前 feature 分支以 squash 方式併入預設分支(main/master)並直推,
#       再清掉本地與遠端的該 feature 分支。
#
# ⚠️ 僅限符合 CLAUDE.md §4「跳 PR 直推例外」的純維護性改動使用:
#    1) STATE.md / CLAUDE.md / 註解 / typo
#    2) 版本字串 bump(不含程式邏輯)
#    3) 不影響功能行為的純文件改動
#    其他一律走 PR。
#
# 用法:
#    ./scripts/quick_merge.sh "commit message"
#
set -euo pipefail

# --- 參數檢查 ---
if [ "$#" -ne 1 ] || [ -z "${1:-}" ]; then
  echo "用法:$0 \"commit message\"" >&2
  exit 1
fi
COMMIT_MSG="$1"

# --- 偵測預設分支(兼容 main / master)---
DEFAULT_BRANCH=""
if git show-ref --verify --quiet refs/heads/main; then
  DEFAULT_BRANCH="main"
elif git show-ref --verify --quiet refs/heads/master; then
  DEFAULT_BRANCH="master"
else
  # 退而求其次:問遠端 HEAD 指向哪個分支
  DEFAULT_BRANCH="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')" || true
fi
if [ -z "$DEFAULT_BRANCH" ]; then
  echo "錯誤:找不到預設分支(main 或 master)。" >&2
  exit 1
fi

# --- 取得當前分支 ---
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# --- 安全檢查:不可在預設分支上執行 ---
if [ "$CURRENT_BRANCH" = "$DEFAULT_BRANCH" ]; then
  echo "錯誤:目前已在預設分支 '$DEFAULT_BRANCH' 上,沒有 feature 分支可併入。" >&2
  exit 1
fi

# --- 安全檢查:working tree 必須乾淨 ---
if [ -n "$(git status --porcelain)" ]; then
  echo "錯誤:working tree 不乾淨,請先 commit 或 stash。" >&2
  git status --short >&2
  exit 1
fi

echo "→ feature 分支:$CURRENT_BRANCH"
echo "→ 預設分支:  $DEFAULT_BRANCH"
echo "→ 開始 squash 併入並直推..."

# --- 切到預設分支並更新 ---
git checkout "$DEFAULT_BRANCH"
git pull origin "$DEFAULT_BRANCH"

# --- squash 併入 ---
git merge --squash "$CURRENT_BRANCH"
git commit -m "$COMMIT_MSG"

# --- 直推 ---
git push -u origin "$DEFAULT_BRANCH"

# --- 清理 feature 分支(本地 + 遠端)---
git branch -D "$CURRENT_BRANCH"
if git ls-remote --exit-code --heads origin "$CURRENT_BRANCH" >/dev/null 2>&1; then
  git push origin --delete "$CURRENT_BRANCH"
fi

echo "✅ 已 squash 併入 '$DEFAULT_BRANCH' 並推送,feature 分支 '$CURRENT_BRANCH' 已清除。"
