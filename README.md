# 🌐 全球政經戰略每日看板 (mynews)

每天早上自動呼叫 Claude API,讓 AI **先上網搜尋真實外電**、再做四維度地緣政治 ×
全球宏觀戰略分析,輸出結構化 JSON,並由 Streamlit 前端呈現。

## 架構

```
GitHub Actions (每日定時)
        │
        ▼
update_data.py ── Claude (claude-opus-4-8 + 伺服器端 web_search)
        │              └─ 先搜尋真實新聞 → 四維度分析 → 輸出 JSON
        ▼
latest_report.json  ──►  app.py (Streamlit) 呈現
data/reports/<date>.json (歷史存檔)
```

## 檔案

| 檔案 | 用途 |
|------|------|
| `update_data.py` | 呼叫 AI 取得 JSON 並存檔的核心腳本 |
| `app.py` | Streamlit 前端,讀取 `latest_report.json` |
| `.github/workflows/daily_update.yml` | 每日定時執行 + 自動 commit/push |
| `requirements.txt` | 相依套件 |

## 設定步驟

1. **設定 API 金鑰**
   GitHub repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `ANTHROPIC_API_KEY`
   - Value: 你的 Anthropic API 金鑰

2. **(選填) 自訂主題**
   同頁 Variables 分頁新增 `REPORT_TOPIC`,例如「中東局勢與紅海航運」。
   未設定則使用預設的全球總經 + 地緣政治綜合主題。

3. **手動測試**
   Actions 分頁 → Daily Macro Intelligence Update → Run workflow。
   成功後 repo 會出現 `latest_report.json`。

## 本地執行

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
python update_data.py          # 產生 latest_report.json
streamlit run app.py           # 啟動看板
```

## 設計重點

- **真實資料**:透過 Claude 伺服器端 `web_search` 工具抓取真實外電,`raw_news`
  只填搜尋到的真實報導,避免憑空捏造新聞。
- **JSON 穩健性**:本地解析時會自動去除 markdown 圍欄、擷取大括號範圍、
  `json.loads` 後再做結構驗證,任一環節失敗即以非零碼結束讓 Actions 標記失敗。
- **可重試**:對 `pause_turn`(伺服器端工具迭代上限)會自動續跑。

> ⚠️ 本專案產出的內容由 AI 自動生成,僅供參考,非投資建議。
