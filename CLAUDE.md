# 核心開發與治理協議 (Core Protocol v2.0)

## §1 狀態與記憶管理 (State & Memory)
- **冷熱資料分離**：專案根目錄必須維持極簡 `STATE.md`。每次任務**僅限讀取此檔與目錄結構**來理解專案目標，嚴禁要求使用者重複解釋。
- **防幻覺機制**：對話超過 10 輪時，修改程式碼前**必須重新讀取目標檔**（不准信任記憶）。
- **主動壓縮**：階段任務完成時，主動提醒我執行 `/compact` 指令，保留核心決策並清理無用推理鏈。

## §2 精準讀寫與檢索 (Precision I/O)
- **大檔案防截斷**：讀取超過 500 行的檔案，強制使用 `offset` 與 `limit` 分段讀取；搜尋結果超過 2000 bytes 時，必須用 `grep` 進行二次精確驗證。
- **動工前大掃除**：重構前優先清理 Dead code 與 Unused imports，極大化釋放 Token 空間。
- **局部編輯**：閉嘴寫扣 (No-Yapping)。嚴禁整檔覆蓋，僅針對特定函數或行數進行精準替換。

## §3 規劃與多線程 (Plan & Parallel Execute)
- **嚴格三步法**：Explore Agent（唯讀探索環境） -> 提出 Plan（3 句話藍圖）與我確認 -> 獲准後才 Execute（動手改 code）。
- **並行處理**：若任務牽涉超過 5 個檔案，主動拆分成子任務並行處理，極致利用 API Context Cache 共享快取。

## §4 鋼鐵自省與交付 (Audit & Delivery)
- **強制驗證機制**：不准說 Done 就跑。修改後必須通過 Type check 與 Lint。完成後輸出簡短報告：[邏輯]、[邊界]、[效能]、[Debug]。
- **環境與效能**：限用 `.py` 腳本（禁 `.ipynb`），維護 `requirements.txt`。必須確保 `st.cache_data` 的正確使用以優化 Streamlit 效能。
- **PR 規範**：使用 `gh pr create` 建立請求，並隨附一鍵 Merge 指令：`gh pr merge <PR號碼> --merge --delete-branch`。嚴禁自動 Merge。

## §5 卡關救援 (Anti-Loop Protocol)
- 針對同一個報錯，若連續重試 2 次未果，**立即停機**。
- 啟動除錯協議，並交由我詢問其他 AI 進行雙重驗證。
