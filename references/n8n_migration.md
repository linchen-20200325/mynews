# n8n 遷移藍圖：盯盤 bot 收訊 × 每日觸發 × 集中監控

> 領域專屬規範（§6 漸進揭露）。動 n8n 前讀本檔；核心規矩仍在 `CLAUDE.md`。
> 對應任務：把散在 NAS（`scripts/nas_line_bot.py`、`scripts/nas_trigger.py`）與 GitHub 排程的自動化，**編排面**集中到 n8n。

---

## 0. 先讀：n8n 治什麼、治不了什麼（紅線）

**核心觀念：n8n 是「控制台」，不是「執行引擎」。** 集中的是**編排與可視性**，不是把運算搬進去。

| 面向 | n8n 治得了 | n8n 治不了 |
|---|---|---|
| 你自己那支 webhook 進程死掉不復活 | ✅ 用容器 `restart: unless-stopped` 根治 | — |
| 收訊 / 觸發 / 監控散在三處、看不到全局 | ✅ 集中一個面板 | — |
| **LINE 平台 outage**（2026-07-28 Messaging API 全球掛） | — | ❌ **任何 host 都擋不住**；LINE 發不出就是發不出（見 GOTCHAS 第 0 步） |
| 報告主流程 `update_data.py`（~2000 行、10+ SSOT 模組） | — | ❌ **紅線：不進 n8n**（理由見下） |
| NAS 代理抓資料的依賴 | — | ❌ 依舊需要 NAS 代理 |

**紅線理由——報告運算為何留在 GitHub Actions：**
搬進 n8n 只有兩種下場：(a) 全砍重寫成節點 → Gemini 結構化輸出+驗證+降級、8 個 fetcher 各自容錯、決策大腦/共振、以及 `GOTCHAS.md` 累積的硬化**全歸零**；(b) 用 Execute Command 節點跑 `python update_data.py` → 等於沒搬，還多壓一層 n8n 依賴。**兩者皆純虧。** GitHub Actions 免費、版控、與 code 同源——把**觸發**交給 n8n、把**執行**留在 Actions，才是正解。

---

## 1. 架構：before → after

```
【before】
LINE ──webhook──▶ NAS: nas_line_bot.py ──(讀改寫)──▶ GitHub watchlist.json
                    （進程死＝bug #1）
NAS: nas_trigger.py ──06:00 workflow_dispatch──▶ GitHub Actions daily_update.yml
GitHub schedule（06:40/07:30 備援） ─────────────▶ 同上
healthchecks.io ◀──心跳── update_data.py（F1/A3）

【after】
LINE ──webhook──▶ n8n Webhook（容器常駐、自動重啟）──(讀改寫 sha 樂觀鎖)──▶ watchlist.json
n8n Schedule（06:00）──workflow_dispatch──▶ GitHub Actions daily_update.yml
GitHub schedule（06:40/07:30 備援） ───────────▶ 同上（保留，不動）
n8n Monitor ──讀 Actions run 結果 / 接 workflow_run webhook──▶ LINE 告警（集中一處）
```

**不變的東西**：`watchlist.json`（清單正本，仍在 repo）、報告推播側程式、GitHub schedule 備援、程式端的交易日/資料齊備守門。

---

## 2. 前置：n8n 怎麼跑（可靠度真源）＋ 要建的 Credentials

### 2.1 Hosting（治「進程死」的關鍵，不是 n8n 本身）
用 Docker，`restart: unless-stopped` + 具名 volume 持久化 + HTTPS 對外（反代或 Cloudflare Tunnel）：

```yaml
# docker-compose.yml（跑在 NAS 或任何 24h 開機主機）
services:
  n8n:
    image: docker.n8n.io/n8nio/n8n
    restart: unless-stopped          # ← 這行才是根治「死了不復活」
    ports: ["5678:5678"]
    environment:
      - N8N_HOST=your.domain
      - N8N_PROTOCOL=https
      - WEBHOOK_URL=https://your.domain/   # LINE 要打的外網位址由此決定
      - GENERIC_TIMEZONE=Asia/Taipei       # ← Schedule 節點的 06:00 才是台灣時間
      - TZ=Asia/Taipei
      # 若指令解析改用 Code 節點的 require('crypto') 才需要下一行；用 Crypto 節點則免
      # - NODE_FUNCTION_ALLOW_BUILTIN=crypto
    volumes:
      - n8n_data:/home/node/.n8n
volumes:
  n8n_data:
```

### 2.2 要在 n8n 建立的 Credentials（**絕不寫進 workflow JSON**，硬規則 §4）
| Credential | 型別 | 內容 |
|---|---|---|
| `LINE Reply`（推 reply/push 用） | Header Auth | `Authorization: Bearer <盯盤 channel 的 access token>` |
| `LINE Channel Secret`（驗簽用） | n8n Variable / 環境變數 | 盯盤 channel 的 **Channel secret**（HMAC 金鑰） |
| `GitHub PAT` | Header Auth 或 GitHub API | `Authorization: Bearer <PAT，需 repo + workflow 權限>` |

> **金鑰紀律**：匯出 workflow JSON 時 n8n 會剝除 credential 值，但**節點參數裡的 URL/owner 仍在**；嚴禁把 token 直接貼進 Code/HTTP 節點。全部走 Credentials。

---

## 3. Phase 1 — 收訊 webhook（獨立，可與 P2 並行）

**目標**：n8n Webhook 接管 加/刪/清單…，退役 `nas_line_bot.py`，治掉 bug #1 的「進程死」面向。

### 3.1 節點流程
```
Webhook(Respond: Immediately, Raw Body: ON)
  └▶ Crypto(HMAC-SHA256 → base64) ─▶ IF(簽章相符?)
        ├─ false ─▶ NoOp（丟棄；LINE 已收 200）
        └─ true ──▶ Code(解析指令 mirror watchlist.py)
                      └▶ HTTP GET contents（拿 sha+內容）
                          └▶ Code(套用加/刪/清單，產生新 JSON)
                              └▶ HTTP PUT contents（帶 sha；409→重抓重試）
                                  └▶ HTTP POST LINE reply
```

### 3.2 逐節點設定
| 節點 | 型別 | 關鍵設定 |
|---|---|---|
| Webhook | `webhook` | HTTP `POST`；Path 例 `line-watch`；**Respond = Immediately**（LINE 只需 200，reply token 壽命短，先回再處理）；**Raw Body = ON**（驗簽要原始位元組） |
| Crypto | `crypto` | Action `HMAC`；Type `SHA256`；Value = 原始 body；Secret = Channel secret；Encoding `base64` |
| IF | `if` | `{{$json.hmac}}` **等於** header `x-line-signature`；相符才續 |
| Code(解析) | `code` | 見 3.4 |
| GitHub GET/PUT | `httpRequest` | 見 3.5 |
| LINE reply | `httpRequest` | 見 3.6 |

> ⚠️ **Raw Body 坑（必踩）**：LINE 簽章是對「原始 request body 位元組」做 HMAC。若 Webhook 先把 JSON 解析掉，重新 `JSON.stringify` 出來的字串**位元組不一定一致 → 驗簽必失敗**。務必開 Webhook 的 **Raw Body**，對原始字串驗簽，**驗完再解析**。

### 3.3 驗簽（優先用 Crypto 節點，免動環境變數）
- Crypto 節點：`HMAC / SHA256 / base64`，Secret 綁 Channel secret，Value 綁原始 body。
- 輸出的 hash 與 `x-line-signature` 標頭在 IF 節點比對。
- **替代法**（Code 節點內 `require('crypto')`）：需在 compose 加 `NODE_FUNCTION_ALLOW_BUILTIN=crypto`，較麻煩，不推薦。

### 3.4 指令解析 Code（mirror `watchlist.py`）
> **SSOT 例外（必註明，同 `nas_line_bot.py` 舊角色）**：`watchlist.py` 仍是加/刪規則的**規格正本**；退役 `nas_line_bot.py` 後，此 Code 節點成為其**唯一鏡像**，兩者須手動同步。**份數不增（2 → 2）**。
> **匯入前務必對照 `watchlist.py` 的實際常數**（`ADD_KW/DEL_KW/LIST_KW`、`normalize_ticker` 的 regex、以及 id/授權/回饋/靜音/admin 等**完整指令面**），以下僅為結構範例、非權威關鍵字：

```js
// n8n Code 節點（Run Once for Each Item）— 結構示意，關鍵字/regex 以 watchlist.py 為準
const ev = $json.body.events?.[0];
if (!ev || ev.type !== 'message' || ev.message?.type !== 'text') return [];
const text = (ev.message.text || '').trim();
const low = text.toLowerCase();

// ↓ 這些常數請「照抄」watchlist.py，勿自行臆造
const ADD_KW  = ['新增','加入','加','add','+'];
const DEL_KW  = ['刪除','移除','刪','remove','del','-'];
const LIST_KW = ['清單','清单','list','ls'];
const TICKER_RE = /[0-9]{4,6}[A-Z]?/;               // 與 normalize_ticker 對齊

const normalize = (raw) => (String(raw||'').toUpperCase().match(TICKER_RE) || [''])[0];

let action = 'help', ticker = '';
if (ADD_KW.some(k => low.startsWith(k.toLowerCase())))       { action='add';    ticker=normalize(text); }
else if (DEL_KW.some(k => low.startsWith(k.toLowerCase())))  { action='remove'; ticker=normalize(text); }
else if (LIST_KW.includes(low))                             { action='list'; }

return [{ json: {
  action, ticker,
  replyToken: ev.replyToken,
  userId: ev.source?.userId || '',
} }];
// ⚠️ id / 授權(grant/revoke/allowlist) / 回饋 / 靜音(mute) 等指令未在此範例中——
//    若你有用到，務必比照同一 pattern 從 watchlist.py 補齊，否則退役後功能會默默消失。
```

### 3.5 GitHub 讀-改-寫（sha 樂觀鎖）
兩顆 HTTP Request 節點，中間夾一顆套用變更的 Code：

**GET**（拿目前內容與 sha）
```
GET  https://api.github.com/repos/<OWNER>/mynews/contents/watchlist.json?ref=main
Header: Authorization: Bearer <PAT>（走 Credential）,  Accept: application/vnd.github+json
→ 回傳 { content(base64), sha }
```
**Code（套用變更）**：`atob(content)` → `JSON.parse` → 依 action 加/刪 → `JSON.stringify` → `btoa`。
**PUT**（帶 sha 寫回）
```
PUT  https://api.github.com/repos/<OWNER>/mynews/contents/watchlist.json
Body: { "message":"watch: <action> <ticker>", "content":"<base64>", "sha":"<剛拿到的 sha>", "branch":"main" }
```
> ⚠️ **並行寫入坑**：兩個使用者同時改 → 後寫者 sha 過期 → GitHub 回 **409**。處理：捕捉 409 → **重抓 GET 拿新 sha → 重試一次**；再失敗就 reply「請稍後再試」。這就是樂觀鎖，別省。

### 3.6 LINE reply（reply token 坑）
```
POST https://api.line.me/v2/bot/message/reply
Header: Authorization: Bearer <盯盤 token>（走 LINE Reply Credential）
Body: { "replyToken": "{{$json.replyToken}}", "messages": [{ "type":"text", "text":"<結果字串>" }] }
```
> ⚠️ **reply token 單次、壽命短（約 1 分鐘）**。所以 Webhook 要 **Respond=Immediately** 先回 LINE 200，整條流程要**快**；GitHub 讀改寫別卡太久。真的慢，改用 push API（需 userId）當備援。

### 3.7 可匯入骨架 JSON（**匯入後須照 3.2–3.6 補參數**）
> 誠實標記：n8n 節點參數 shape 隨版本而異，**手寫 JSON 不保證一鍵匯入即用**。建議以上面的 spec 逐節點建立最穩；此骨架僅供起手。
```json
{
  "name": "LINE Watch Bot (P1)",
  "nodes": [
    { "name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [260, 300],
      "parameters": { "httpMethod": "POST", "path": "line-watch", "responseMode": "onReceived", "options": { "rawBody": true } } },
    { "name": "HMAC", "type": "n8n-nodes-base.crypto", "typeVersion": 1, "position": [480, 300],
      "parameters": { "action": "hmac", "type": "SHA256", "dataPropertyName": "body", "secret": "={{ $vars.LINE_CHANNEL_SECRET }}", "encoding": "base64" } },
    { "name": "IF sig", "type": "n8n-nodes-base.if", "typeVersion": 2, "position": [700, 300], "parameters": {} },
    { "name": "Parse", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [920, 300], "parameters": { "mode": "runOnceForEachItem", "jsCode": "// 見 3.4" } },
    { "name": "GH Get", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4, "position": [1140, 300], "parameters": {} },
    { "name": "Apply", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [1360, 300], "parameters": { "jsCode": "// 見 3.5" } },
    { "name": "GH Put", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4, "position": [1580, 300], "parameters": {} },
    { "name": "LINE Reply", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4, "position": [1800, 300], "parameters": {} }
  ],
  "connections": {
    "Webhook":   { "main": [[{ "node": "HMAC", "type": "main", "index": 0 }]] },
    "HMAC":      { "main": [[{ "node": "IF sig", "type": "main", "index": 0 }]] },
    "IF sig":    { "main": [[{ "node": "Parse", "type": "main", "index": 0 }], []] },
    "Parse":     { "main": [[{ "node": "GH Get", "type": "main", "index": 0 }]] },
    "GH Get":    { "main": [[{ "node": "Apply", "type": "main", "index": 0 }]] },
    "Apply":     { "main": [[{ "node": "GH Put", "type": "main", "index": 0 }]] },
    "GH Put":    { "main": [[{ "node": "LINE Reply", "type": "main", "index": 0 }]] }
  }
}
```

### 3.8 退役 `nas_line_bot.py` checklist（**驗收通過後才做**）
1. n8n webhook 實測加/刪/清單全綠（見 3.9）。
2. LINE console → 該 channel 的 Webhook URL 改指 `https://your.domain/webhook/line-watch`。
3. 觀察 1–2 天雙軌無誤後，NAS 停 `nas_line_bot.py`（先停、別急刪）。
4. repo 內 `scripts/nas_line_bot.py` 標 deprecated 或移除，並在本檔記錄 SSOT 鏡像已轉至 n8n Code 節點。

### 3.9 驗收（沙箱做不到，你在 n8n 端做）
- LINE 傳「清單」→ 秒回目前清單。
- 傳「加 2330」→ 回成功，GitHub `watchlist.json` 出現 2330（看 commit）。
- 傳「刪 2330」→ 回成功，檔案移除。
- 兩支手機同時加不同股 → 都成功（驗 sha 樂觀鎖 / 409 重試）。
- 亂簽章打 webhook → 被 IF 擋掉、無副作用。

---

## 4. Phase 2 — 每日觸發（獨立，可與 P1 並行）

**目標**：n8n Schedule 取代 `nas_trigger.py` 的 06:00 `workflow_dispatch`。

### 4.1 節點
```
Schedule Trigger(Cron 0 6 * * *, TZ Asia/Taipei)
  └▶ HTTP POST .../actions/workflows/daily_update.yml/dispatches  body {"ref":"main"}
```

### 4.2 可匯入 JSON（簡單、可靠度高）
```json
{
  "name": "Daily Dispatch (P2)",
  "nodes": [
    { "name": "06:00 TW", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [300, 300],
      "parameters": { "rule": { "interval": [{ "field": "cronExpression", "expression": "0 6 * * *" }] } } },
    { "name": "Dispatch daily_update", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4, "position": [560, 300],
      "parameters": {
        "method": "POST",
        "url": "https://api.github.com/repos/<OWNER>/mynews/actions/workflows/daily_update.yml/dispatches",
        "sendHeaders": true,
        "headerParameters": { "parameters": [ { "name": "Accept", "value": "application/vnd.github+json" } ] },
        "sendBody": true,
        "specifyBody": "json",
        "jsonBody": "{\n  \"ref\": \"main\"\n}",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth"
      } }
  ],
  "connections": {
    "06:00 TW": { "main": [[{ "node": "Dispatch daily_update", "type": "main", "index": 0 }]] }
  }
}
```

### 4.3 守門仍在程式端（**別搬進 n8n**）
`update_data.py` 的交易日 / 資料齊備 / schedule 去重守門**維持在程式**（GOTCHAS：「NAS 是觸發器不是執行器」）。n8n 只負責「發第一槍」，要不要真的跑由程式判定。

### 4.4 退役 `nas_trigger.py`
GitHub schedule 備援（06:40/07:30）**保留不動**；n8n 上線並連續數日觀察 dispatch 正常後，停用 NAS cron，repo 內腳本標 deprecated。

---

## 5. Phase 3 — 集中可視性（依賴 P1+P2 上線）

**目標**：真正的 single pane——一處看全局、失敗集中告警。

### 5.1 兩種監控（擇一或並用）
- **Pull（穩）**：Schedule（如每日 08:00 TW）→ HTTP GET `.../actions/workflows/daily_update.yml/runs?per_page=1` → 讀最新 run 的 `conclusion`；非 `success` → LINE 告警。
- **Push（即時）**：GitHub repo 設 webhook（`workflow_run` 事件）→ n8n Webhook 接 → 過濾 `conclusion=failure` → LINE 告警。

### 5.2 心跳（沿用，勿誤傷）
F1/A3 的 healthchecks.io 心跳**由 `update_data.py` 送**（跑完才 ping）。n8n 監控只**讀**狀態、**別去 ping 心跳 URL**（GOTCHAS：主動 ping ＝假活訊號）。

---

## 6. 安全與 SSOT 守則（彙整）
- **金鑰**：全走 n8n Credentials；匯出 JSON 前確認無明碼 token。
- **SSOT**：`watchlist.json` ＝清單正本（repo）；`watchlist.py` ＝加/刪規則規格正本；n8n Parse Code ＝其鏡像（唯一，退役 `nas_line_bot.py` 後份數不增）。路徑/時區/資料載入仍尊重 `paths.py`／`tz_utils.py`／`etf_data.py`——n8n 端不得重貼字面值。
- **紅線**：報告運算不進 n8n。

## 7. 回退方案（逐階可獨立 revert）
| 階段 | 回退動作 |
|---|---|
| P1 | LINE Webhook URL 指回 NAS，重啟 `nas_line_bot.py`（退役前別刪，故可秒退） |
| P2 | 停 n8n Schedule；GitHub schedule 備援本就在，NAS cron 重啟即復原 |
| P3 | 純監控、無副作用；停 workflow 即可，不影響主流程 |

## 8. 踩坑對照表（n8n × LINE × GitHub）
| 症狀 | 根因 | 對策 |
|---|---|---|
| 驗簽一直失敗 | 對解析後 JSON 重組字串驗簽、位元組不符 | Webhook 開 **Raw Body**，對原始字串 HMAC |
| reply「Invalid reply token」 | token 過期/重用 | Webhook Respond=Immediately，流程要快；慢則改 push |
| 寫檔偶發失敗回 409 | 兩人並行、sha 過期 | 重抓 sha 重試一次（樂觀鎖） |
| Code 節點 `require('crypto')` 報錯 | n8n 預設鎖 require | 改用 **Crypto 節點**；或設 `NODE_FUNCTION_ALLOW_BUILTIN=crypto` |
| Schedule 不在 06:00（台灣） | 容器 UTC 時區 | compose 設 `GENERIC_TIMEZONE=Asia/Taipei` + `TZ` |
| **bot 整條靜默、什麼都沒回** | **LINE 平台 outage**（非你的問題） | 先查 LINE API Status（GOTCHAS 第 0 步）；n8n 也救不了，等復原 |
| 容器重開後設定不見 | 沒掛 volume | 掛 `n8n_data` 具名 volume |
