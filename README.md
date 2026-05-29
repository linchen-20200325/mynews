# 🌐 全球政經戰略每日看板 (mynews)

每天早上自動執行的雙模型情報管線:**Claude 上網搜尋真實外電並做四維度地緣政治 ×
全球宏觀戰略分析**,再由 **Gemini 產生最終版白話文字典**,輸出結構化 JSON,
並由 Streamlit 前端呈現(含歷史報告瀏覽)。

## 架構

```
GitHub Actions (每日定時)
        │
        ▼
update_data.py
   ├─ [1] Claude (claude-opus-4-8 + 伺服器端 web_search)
   │        先搜尋真實新聞 → 四維度分析 → JSON 主體
   └─ [2] Gemini (gemini-2.5-flash)
            依分析內容產生「最終版白話文字典」(失敗則回退 Claude 字典)
        │
        ▼
latest_report.json          ──►  app.py (Streamlit) 呈現
data/reports/<date>.json    歷史存檔(側邊欄可瀏覽)
```

## 檔案

| 檔案 | 用途 |
|------|------|
| `update_data.py` | 雙模型核心:Claude 分析 + Gemini 白話文,輸出 JSON |
| `app.py` | Streamlit 前端,含歷史報告選擇器 |
| `.github/workflows/daily_update.yml` | 每日定時執行 + 自動 commit/push |
| `requirements.txt` | 相依套件 |
| `CLAUDE.md` | 開發規範(供 AI 與人類協作參考) |
| `STATE.md` | 專案當前狀態、待辦與已知問題 |

## 設定步驟

GitHub repo → Settings → Secrets and variables → Actions

**Secrets(必/選):**
- `ANTHROPIC_API_KEY`(必填)— Anthropic API 金鑰
- `GEMINI_API_KEY`(選填)— 設定後白話文改由 Gemini 產生;未設定則沿用 Claude 字典

**Variables(皆選填):**
- `GEMINI_MODEL` — 覆寫 Gemini 模型(預設 `gemini-2.5-flash`)
- `REPORT_TOPIC` — 自訂分析主題

設定完成後可在 Actions 分頁手動 **Run workflow** 測試。

## 本地執行

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
export GEMINI_API_KEY="..."      # 選填
python update_data.py            # 產生 latest_report.json
streamlit run app.py             # 啟動看板
```

## 設計重點

- **真實資料**:透過 Claude 伺服器端 `web_search` 抓真實外電,`raw_news` 只填真實報導。
- **雙模型分工**:Claude 負責「需要上網 + 深度推理」的戰略分析;Gemini 負責「最終版白話文」。
  兩者用各自官方 SDK(`anthropic` / `google-genai`)。
- **穩健 JSON**:本地解析會去除 markdown 圍欄、擷取大括號/中括號範圍、`json.loads` 後做結構驗證。
- **不因白話文失敗而整批失敗**:Gemini 出錯時自動回退使用 Claude 的字典,並於日誌標註 `dictionary_source`。

> ⚠️ 本專案產出內容由 AI 自動生成,僅供參考,非投資建議。
