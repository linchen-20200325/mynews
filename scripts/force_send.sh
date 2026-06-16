#!/bin/sh
# force_send.sh — 手動「強制補發」今日報告 + LINE(繞過去重)。
#
# 用途:GitHub 排程漏跑、或凌晨那班用了不完整數據時,白天資料齊備後強制重發一次。
# 與每日自動排程的差別:這裡用 TRIGGER_MODE=always —— 不先查「今日是否已成功」,
# 直接打 workflow_dispatch。dispatch 不受 update_data.py 的 schedule 去重限制,
# 必定完整跑並推 LINE(會以最新完整數據覆蓋今日報告)。
#
# 平日自動排程請維持預設 backup 模式(nas_trigger.py),不要用這支;
# 這支只給「今天已有報告、但要手動覆蓋補發」的場合。
#
# 用法:直接執行  sh scripts/force_send.sh
# Token:預設讀 $HOME/mynews/.mynews_gh_token;不同路徑可用環境變數覆寫:
#   GITHUB_TOKEN_FILE=/path/to/token sh scripts/force_send.sh
set -eu

# 腳本所在目錄 → nas_trigger.py 與本檔同一層,路徑不寫死、搬家也不壞
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Token 檔:預設 $HOME/mynews/.mynews_gh_token,可由環境變數覆寫
: "${GITHUB_TOKEN_FILE:=$HOME/mynews/.mynews_gh_token}"
export GITHUB_TOKEN_FILE

# python3:優先 Synology 的 /bin/python3,否則退回 PATH 上的 python3
if [ -x /bin/python3 ]; then
  PY=/bin/python3
else
  PY=$(command -v python3)
fi

echo "[force_send] 強制觸發 daily_update.yml(TRIGGER_MODE=always)…"
TRIGGER_MODE=always "$PY" "$DIR/nas_trigger.py"
