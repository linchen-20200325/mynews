# CLAUDE.md — 開發規範 (Core Protocol)

本檔是本專案最高級別的協作規範,供 AI 助手與人類開發者共同遵循。

## 專案目標

每日自動產生一份「全球政經戰略報告」並以 Streamlit 呈現。核心價值是
**基於真實外電的深度分析**,而非泛泛而談或虛構資料。

## 架構與分工(雙模型)

| 階段 | 模型 | SDK | 職責 |
|------|------|-----|------|
| 分析 | Claude `claude-opus-4-8` | `anthropic` | 伺服器端 `web_search` 抓真實外電 → 四維度戰略分析 → JSON 主體 |
| 白話文 | Gemini `gemini-2.5-flash` | `google-genai` | 依分析內容產生最終版 `laymans_dictionary` |

> 兩個模型各用其官方 SDK,**不要**用 OpenAI 相容層或互相代換。

## 資料契約 (JSON Schema)

`latest_report.json` 與 `data/reports/<date>.json` 必須含以下頂層欄位:
`report_date`、`topic`、`raw_news`、`strategic_analysis`、`laymans_dictionary`,
另含 `dictionary_source`(`claude` 或 `gemini`)。
`strategic_analysis` 必含 `geo_military` / `supply_chain` / `macro_economy` /
`blind_spots_and_kpi` 四欄。詳見 `update_data.py` 的 `validate_report()`。

## 開發守則

1. **真實優先**:`raw_news` 只能填真實搜尋到的報導,嚴禁虛構標題/媒體/數據。
2. **JSON 穩健**:任何模型輸出都要經過 `clean_json_text()` 清理 + `json.loads()` +
   結構驗證;解析失敗一律以非零碼結束讓 CI 標紅。
3. **失敗隔離**:白話文(Gemini)失敗時回退 Claude 字典,不可讓整份報告失敗。
4. **金鑰只走環境變數**:`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` /
   `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO`,嚴禁硬編碼或進版控。
   (LINE 推播用 Messaging API push;LINE Notify 已停用,勿再採用。)
5. **快取友善**:大型 system prompt 維持穩定;每次變動的內容(日期、主題)放 user 訊息。
6. **改動需驗證**:Python 改動後至少 `python -m py_compile` 通過再提交。

## 常用指令

```bash
pip install -r requirements.txt
python -m py_compile update_data.py app.py   # 語法檢查
python update_data.py                         # 產生報告(需金鑰)
streamlit run app.py                          # 啟動看板
```

## 分支與提交

- 功能開發在指定的 feature 分支,提交訊息清楚描述變更。
- 自動化(GitHub Actions)以 `github-actions[bot]` 身分 commit 更新後的 JSON。
