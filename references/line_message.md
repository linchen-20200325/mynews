# references/line_message.md — LINE 推播訊息規範

> 動到 LINE 推播文字時讀本檔。所有 LINE 發送**唯一走 `line_notify.py`**（SSOT），嚴禁直接 urllib/requests 推 LINE。

## 兩隻 bot
- **主 bot**（`LINE_CHANNEL_ACCESS_TOKEN`/`LINE_TO`）：純推播、**無 webhook**（收不到回話）。①國際盤 ②共振 ③法人事件 ④戰略報告。
- **盯盤 bot**（`LINE_WATCH_TOKEN`/`LINE_WATCH_TO`）：**有 webhook**（`nas_line_bot.py` 常駐 NAS），收「加/刪/清單/回饋/靜音」。⑤個股盯盤。

## 硬規則
- **截斷唯一走 `_clip()`/`_finalize()`**（DRY），別在各 builder 內 inline 截斷。
- **①國際盤是每日心跳載體**（F1 dead-man + A3 gap 自檢靠它偵測系統存活）→ **①永不可被靜音**（`muted_types` 雙保險濾掉①）。
- 每則主 bot 訊息結尾附看板連結（`config.DASHBOARD_URL`，未設不顯示）+ `MORNING_TAGLINE`（晨報型定位、非盤中即時）。
- 平靜日①走壓縮版（F5a）；大跌/警戒才展開完整研判。
- `ai_ok is False`（Gemini 降級）→ 明示「AI 研判暫離線，以下為真實報價」，事實層（Yahoo 報價/程式算大跌）獨立活著。

## 正本/鏡像紀律
- 盯盤指令純邏輯在 `watchlist.py`（SSOT 正本）；`nas_line_bot.py` **逐字鏡像**（NAS 零相依例外）。改一邊必同步另一邊，附「正本/鏡像逐項一致」測試。
