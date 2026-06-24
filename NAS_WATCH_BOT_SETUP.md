# 個股盯盤 LINE Bot 架設說明書(mynews)

> 版本:v1.0
> 適用:讓你在 LINE 上即時加/刪自選台股清單,並每天早上收到自選個股的「消息面 AI 總結 + 月營收」。
> 對應程式:`scripts/nas_line_bot.py`(NAS 常駐 webhook)、`update_data.py`(排程推播)、`watchlist.py`、`earnings_fetcher.py`。

---

## 一、運作架構(為什麼要在 NAS 跑一支)

整套主流程是 GitHub Actions「**單向排程推播**」,沒有任何能「**接收**」LINE 訊息的伺服器。
要在 LINE 上加/刪股票,必須有一台 24h 在線的程式接 LINE webhook —— 就跑在你的 NAS 上。

```
編輯清單(即時):
  你在 LINE 打「加 2330」
        │  LINE 平台 POST(帶 X-Line-Signature)
        ▼
  NAS:nas_line_bot.py(驗簽 → 改 watchlist.json)
        │  GitHub Contents API(寫回 repo,canonical 清單在 repo)
        ▼
  repo/watchlist.json ✅  → bot reply 回你目前清單

每天早上(推播):
  GitHub Actions 排程 → update_data.run_watch_section()
        │  讀 repo/watchlist.json → 逐檔抓真實新聞 + Gemini 總結 + 月營收
        ▼
  第二個 LINE bot push 給你(LINE_WATCH_TOKEN / LINE_WATCH_TO)
```

兩端共用同一份 `watchlist.json` 與 `watchlist.py` 的加/刪規則,不會漂移。

---

## 二、前置:開第二個 LINE bot,拿三樣東西

在**電腦瀏覽器**進 [LINE Developers Console](https://developers.line.biz/console/)(用你的 LINE 帳號登入):

1. 建立(或沿用)一個 **Provider** → 在底下建立新的 **Messaging API channel**(名字例如「個股盯盤」)。
2. 取得三樣東西:

| 要的東西 | 在哪 | 設到哪 |
|---|---|---|
| `LINE_WATCH_TOKEN` | **Messaging API** 分頁 → 最下面 **Channel access token (long-lived)** → **Issue** | repo Secrets + NAS 環境變數 |
| `LINE_WATCH_SECRET` | **Basic settings** 分頁 → **Channel secret** | **只放 NAS**(驗簽用,不進 repo) |
| 你的 `userId` | 加 bot 好友後傳「**id**」給它,它會回你(最簡單) | repo Secret `LINE_WATCH_TO` |

3. 在 **Messaging API** 分頁用手機 LINE 掃 **QR code** 加這個 bot 為好友(不加好友收不到推播)。
4. **Basic settings** 把「Auto-reply messages / 歡迎訊息」關掉(避免官方罐頭訊息蓋掉 bot 回覆)。

---

## 三、NAS 準備

### 3-1. 放程式
NAS 上若已 clone 本 repo(跑 `nas_trigger.py` 那台),直接 `git pull` 即可;否則 clone 一份。
`nas_line_bot.py` 會自動把 repo 根目錄加進 import path,找得到 `watchlist.py`。

### 3-2. 準備一個有寫入權的 GitHub Token(改 watchlist.json 用)
GitHub → Settings → Developer settings → **Fine-grained tokens** → 只授權本 repo 的
**Contents: Read and write**。存成檔案並鎖權限:
```bash
echo 'github_pat_xxx' > /volume1/homes/<you>/.mynews_gh_token
chmod 600 /volume1/homes/<you>/.mynews_gh_token
```
> 可與 `nas_trigger.py` 共用同一把 token,但那把目前只授權 Actions;要改 watchlist 需再勾 **Contents: write**。

### 3-3. 先手動試跑
```bash
LINE_WATCH_TOKEN='xxx' \
LINE_WATCH_SECRET='yyy' \
GITHUB_TOKEN_FILE='/volume1/homes/<you>/.mynews_gh_token' \
WATCH_BOT_PORT=8080 \
  /usr/bin/python3 /volume1/.../scripts/nas_line_bot.py
```
看到 `個股盯盤 webhook 啟動,監聽 :8080` 即正常。先別關,進下一步把它接上外網。

可選環境變數:
- `WATCH_ALLOW_USER`:**bootstrap 授權名單**(設成你自己的 userId 即可)。日常加人**不靠它**——
  改用 LINE 指令 `授權`(見下),寫進 repo 的授權名單、即時生效、免重啟。留空 = 不限制(對外開放不建議)。
- `WATCH_ADMIN_USER`:**管理員**(可下 `授權`/`撤銷`/`名單`)。未設則沿用 `WATCH_ALLOW_USER`。
  一般只要設 `WATCH_ALLOW_USER='<你的userId>'`,你就同時是管理員,其餘人用 LINE `授權` 加。
- `WATCH_TECH_MONTHS`:技術面回看月數(預設 `4`,約 80 個交易日,夠算 60MA/KD/RSI)。
- `WATCH_CHIP_DAYS`:籌碼面回看交易日數(預設 `6`,夠呈現外資連買/連賣天數)。
  > 技術面/籌碼面是「早上排程推播」(`update_data.py`)讀的,設在 NAS bot 不影響推播;
  > 要調整請設在 GitHub Actions 的環境(或留預設即可)。
- `GITHUB_REPO` / `GITHUB_BRANCH`:預設 `linchen-20200325/mynews` / `main`。

> **每人各自獨立清單(per-user)**:加/刪會作用在「傳訊者自己」名下,各人各一份、早上各推各的。
> 既有舊扁平清單會在「第一個下加/刪指令的人」那次無損遷移到他名下。

---

## 四、把 NAS 對外(LINE 要打得進來)

LINE webhook 必須是 **HTTPS 公開網址**。二選一:

### 方案 A:Cloudflare Tunnel(推薦,免開路由器埠、免固定 IP、自帶 HTTPS)
1. 有一個掛在 Cloudflare 的網域。
2. NAS 安裝 `cloudflared`(Synology 可用 Container Manager 跑官方 image)。
3. 建立 tunnel 並指向本機服務:
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create mynews-watch
   # 設定 config.yml:把 watch.<你的網域> 導到 http://localhost:8080
   cloudflared tunnel route dns mynews-watch watch.<你的網域>
   cloudflared tunnel run mynews-watch
   ```
4. Webhook URL = `https://watch.<你的網域>/callback`

### 方案 B:路由器埠轉發 + DDNS(Synology 內建)
1. Synology DSM → **控制台 → 外部存取 → DDNS** 設一個 `xxx.synology.me`,並啟用憑證(HTTPS)。
2. 路由器把外部 443 轉發到 NAS 的 `WATCH_BOT_PORT`(或用 DSM 反向代理把 `https://xxx.synology.me/callback` 導到 `localhost:8080`)。
3. Webhook URL = `https://xxx.synology.me/callback`
> 安全考量:務必設 `WATCH_ALLOW_USER` 限定只有你能改清單。

**驗證連線**:瀏覽器開 `https://.../callback`(GET)應看到 `mynews watch bot ok`。

---

## 五、回 LINE Console 設 Webhook

**Messaging API** 分頁 → **Webhook settings**:
1. **Webhook URL** 填上面的 `https://.../callback`。
2. 開啟 **Use webhook**。
3. 按 **Verify** → 應回 Success(失敗見第八節)。

---

## 六、設 repo Secrets(讓早上排程會推)

GitHub repo → Settings → Secrets and variables → Actions → **New repository secret**:
- `LINE_WATCH_TOKEN` = 第二個 bot 的 channel access token
- `LINE_WATCH_TO` = 收訊對象的 userId

> 設好後,隔天早上(或手動觸發 workflow)排程就會多推一則「📈 個股盯盤」
> (含每檔的消息面 + 一行技術面均線/KD/RSI + 一行籌碼面外資/投信買賣超張數 + 新月營收)。
> 沒設這兩個 → 程式 `watch_enabled()` 為偽,整段靜默略過,不影響現有早報。

### 想多人收到同一則推播(例:再加老公/父母)— 純改 Secret,免改程式

`LINE_WATCH_TO` 依內容自動選推播模式(`update_data.py` 的 `_push_line_text`):

| `LINE_WATCH_TO` | 行為 |
|---|---|
| 單一 userId | 只推一人 |
| `Uxxx,Uyyy`(逗號/空白分隔) | **multicast** 推給名單(最多 500 人) |
| `broadcast` | 推給所有加這個 bot 好友的人 |

**加一位收訊人**:① 對方把「個股盯盤」這個 bot 加好友 → ② 對方傳「**id**」給 bot,
它會回覆對方的 userId → ③ 把那串 userId 用逗號接到 `LINE_WATCH_TO` 後面即可。
> 提醒:能「收到推播」(`LINE_WATCH_TO`)和能「下指令改清單」(`WATCH_ALLOW_USER`,見上)
> 是兩件事 —— 要對方也能加/刪股票,記得把他的 userId 也加進 `WATCH_ALLOW_USER`。

---

## 七、Synology 開機自動啟動(讓 webhook 常駐)

DSM → **控制台 → 任務排程 → 新增 → 觸發的任務 → 使用者定義指令碼**:
- 一般:使用者 `root`(或具該路徑權限者)、事件 **開機**。
- 任務設定 → 執行指令:
  ```bash
  LINE_WATCH_TOKEN='xxx' \
  LINE_WATCH_SECRET='yyy' \
  GITHUB_TOKEN_FILE='/volume1/homes/<you>/.mynews_gh_token' \
  WATCH_ALLOW_USER='<你的userId>' \
  WATCH_BOT_PORT=8080 \
    /usr/bin/python3 /volume1/.../scripts/nas_line_bot.py >> /volume1/.../watch_bot.log 2>&1
  ```
> 開機就跑、長駐不退。改完設定後可在任務排程「執行」一次套用,或重開機。
> Cloudflare Tunnel(方案 A)也照樣設一個開機任務跑 `cloudflared tunnel run`。

---

## 八、驗收 & 疑難排解

**驗收**:用手機 LINE 傳給「個股盯盤」bot:
- `id` → 回你的 userId(**任何人都會回**,新朋友自助取得後貼給管理員授權)
- `加 2330 台積電` → 回「✅ 已加入 2330 台積電」+ 目前清單(需先被授權)
- `清單` → 列出**你自己**的清單(per-user)
- `刪 2330` → 回「🗑️ 已移除 2330」

**管理員指令**(只有 `WATCH_ADMIN_USER` / bootstrap 名單內的人能用,即時生效免重啟):
- `授權 <對方userId> [名字]` → 開通一個人(例:`授權 Uxxxx 老公`)
- `撤銷 <對方userId>` → 移除授權
- `名單` → 看目前授權了誰

> 加人流程:對方加好友 → 傳 `id` → 把那串貼給你 → 你打 `授權 那串 名字` → 對方立刻能用。

每傳一次,看 GitHub repo 的 `watchlist.json` 是否即時更新(commit 訊息 `watchlist: add 2330 ...`)。

| 症狀 | 多半原因 |
|---|---|
| Console **Verify 失敗** | NAS 沒對外 / 網址打錯 / `nas_line_bot.py` 沒在跑;先用瀏覽器 GET `/callback` 確認回 ok |
| 傳訊息 bot **不回** | Auto-reply 沒關;或 `LINE_WATCH_SECRET` 填錯導致驗簽失敗(看 NAS log「簽章驗證失敗」) |
| 回「**清單更新失敗**」 | `GITHUB_TOKEN` 沒有本 repo 的 **Contents: write**,或 repo/branch 設錯 |
| 早上**沒推**個股盯盤 | repo Secrets 的 `LINE_WATCH_TOKEN`/`LINE_WATCH_TO` 沒設,或當天清單為空 |
| **月營收**沒出現 | 該月公司還沒公告、或當天被擋(會自動略過、只推消息面);上櫃股目前未涵蓋 |

---

## 九、安全須知
- `LINE_WATCH_SECRET`、`GITHUB_TOKEN` 只放 NAS 環境變數/檔案(chmod 600),**切勿進 git**。
- 強烈建議設 `WATCH_ALLOW_USER`,避免陌生人對你的 webhook 亂改清單。
- 所有推播內容均為 AI/工具自動生成,**僅供參考,非投資建議**。
