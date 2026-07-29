#!/bin/sh
# redeploy_watch_bot.sh — 一鍵更新並重啟 NAS 上的「個股盯盤」LINE bot(scripts/nas_line_bot.py)。
#
# 為什麼要這支:bot 是 NAS 常駐 webhook(綁 8080 埠),每次改完程式都要「拉新碼 →
# 殺舊進程釋放埠 → 重跑 → 健檢」四步。手動容易漏掉「先殺舊的」而撞 Address already in use。
# 這支把四步封成一行,且是 fail-safe:git pull / 語法檢查任一失敗就中止,舊 bot 續跑不中斷。
#
# 一次性準備:把祕密環境變數放進一個 env 檔(勿進 git),例如 ../watch_bot.env:
#     LINE_WATCH_TOKEN='xxx'
#     LINE_WATCH_SECRET='yyy'
#     GITHUB_TOKEN_FILE='/volume1/homes/<you>/.mynews_gh_token'
#     WATCH_ALLOW_USER='<你的userId>'
#     WATCH_BOT_PORT='8080'
#   chmod 600 ../watch_bot.env
#
# 用法(在 repo 內):
#     sh scripts/redeploy_watch_bot.sh
#   或自訂路徑:
#     REPO_DIR=/volume1/xxx/mynews ENV_FILE=/volume1/xxx/watch_bot.env \
#       sh scripts/redeploy_watch_bot.sh
#
# 可用環境變數覆寫:REPO_DIR、ENV_FILE、PY(python 路徑)、WATCH_BOT_LOG。
set -eu

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/../watch_bot.env}"
PY="${PY:-/usr/bin/python3}"
BOT="$REPO_DIR/scripts/nas_line_bot.py"
LOG="${WATCH_BOT_LOG:-$REPO_DIR/watch_bot.log}"

# 找出正在跑的 bot PID:pgrep -f 優先(乾淨、自動排除自己);退而用 ps|grep,
# 並排除 grep 自身與本重部署腳本,避免誤殺(本腳本 argv 不含 nas_line_bot.py,但求穩)。
find_bot_pids() {
    {
        if command -v pgrep >/dev/null 2>&1; then
            pgrep -f 'nas_line_bot\.py' 2>/dev/null
        else
            ps aux 2>/dev/null | grep 'nas_line_bot\.py' \
                | grep -v -e '[ /]grep ' -e 'redeploy_watch_bot' | awk '{print $2}'
        fi
    } | grep -vxF -e "$$" -e "${PPID:-0}" || true
}

echo "== 1/5 更新程式(git pull --ff-only)=="
cd "$REPO_DIR"
git fetch origin main
git pull --ff-only origin main

echo "== 2/5 語法檢查(py_compile)=="
"$PY" -m py_compile "$BOT"

echo "== 3/5 載入祕密設定 =="
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
    echo "   已載入 $ENV_FILE"
else
    echo "   找不到 $ENV_FILE — 沿用目前 shell 已 export 的環境變數"
    echo "   (若 bot 需要 token 才能啟動,請先建立該檔,見本檔頂註解)"
fi
PORT="${WATCH_BOT_PORT:-8080}"

echo "== 4/5 停舊進程(釋放 :$PORT)並重啟 =="
OLD="$(find_bot_pids)"
if [ -n "${OLD:-}" ]; then
    echo "   停止舊進程 PID: $(echo "$OLD" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill $OLD 2>/dev/null || true
    # 最多等 ~5 秒讓它退出並釋放埠
    i=0
    while [ "$i" -lt 5 ]; do
        if [ -z "$(find_bot_pids)" ]; then break; fi
        sleep 1
        i=$((i + 1))
    done
    STILL="$(find_bot_pids)"
    if [ -n "${STILL:-}" ]; then
        echo "   仍在,強制 kill -9 $(echo "$STILL" | tr '\n' ' ')"
        # shellcheck disable=SC2086
        kill -9 $STILL 2>/dev/null || true
        sleep 1
    fi
else
    echo "   目前沒有在跑的 bot(首次部署?)"
fi
nohup "$PY" "$BOT" >> "$LOG" 2>&1 &
echo "   已啟動,PID $!;log → $LOG"

echo "== 5/5 健檢(http://127.0.0.1:$PORT/callback 應回 mynews watch bot ok)=="
ok=""
i=0
while [ "$i" -lt 6 ]; do
    sleep 1
    if command -v curl >/dev/null 2>&1; then
        body="$(curl -fsS "http://127.0.0.1:$PORT/callback" 2>/dev/null || true)"
    else
        body="$("$PY" -c "import urllib.request,sys;sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:$PORT/callback',timeout=3).read().decode())" 2>/dev/null || true)"
    fi
    case "$body" in
        *"mynews watch bot ok"*) ok="1"; break ;;
    esac
    i=$((i + 1))
done
if [ -n "$ok" ]; then
    echo "   ✅ bot 已上線,回應正常。"
    echo "完成。到 LINE 連點兩次「刪 XXXX」驗收:一則『已移除』、另一則『不在清單內』,不再有『寫回 repo 出錯』。"
else
    echo "   ❌ 健檢未通過。最後幾行 log:"
    tail -n 15 "$LOG" 2>/dev/null || true
    echo "   常見原因:8080 埠未釋放、缺 token 環境變數、python 路徑不對(用 PY=... 覆寫)。"
    exit 1
fi
