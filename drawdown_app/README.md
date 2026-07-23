# 🐻 空頭回撤 vs 復原分析 / Bear-market Drawdown vs Recovery

一個可獨立部署的 Streamlit 看板：以 **reclaim-prior-peak（收復前高才算復原）**
定義，切出歷史所有回撤事件，量化「跌得多深 → 要多久／多少漲幅才站回前高」，
並檢驗**跌幅與復原時間**之間的相關性（Pearson＋Fisher-z 信賴區間、Spearman、OLS）。

> ⚠️ **`drawdown_core.py` 為「依規格重建」版本**：原版當時未提供，本引擎依書面
> 定義嚴格重建。請與你手上的原版 `diff`；若指標語意有出入，以原版為準並回報對齊。

---

## 功能 / Features

5 個分頁（全 Plotly、中英雙語）：

1. **📉 水下曲線 / Underwater** — 收盤 vs 歷史新高(ATH)，回撤 % 填色。
2. **🐻 事件表 / Episodes** — 每次回撤的峰/谷/復原、drawdown、required_gain、
   下跌/復原/來回天數（**日曆天與交易天各一份**）、隱含復原 CAGR，可下載 CSV。
3. **🔗 跌幅 vs 復原 / DD vs Recovery** — 散點 + OLS 迴歸線，Pearson r（含信賴
   區間）、Spearman ρ、R²；X/Y 變數可自選。
4. **⏱️ 復原時間 / Recovery time** — 復原交易天分布與中位/平均/最長。
5. **🧪 資料與方法 / Data & Method** — 資料來源狀態、方法學說明。

## 核心定義 / Definitions

| 名詞 | 定義 |
|------|------|
| 水下 underwater | 收盤 < 歷史新高（cummax，嚴格小於） |
| 峰頂 peak | 進入水下前的歷史新高（水下期間 ATH 不變） |
| 谷底 trough | 事件區間內最低收盤 |
| 復原 recovery | 谷底後首個「收盤 ≥ 峰頂」之日（收復前高） |
| drawdown | `trough/peak − 1`（≤ 0） |
| required_gain | `peak/trough − 1`（≥ 0，收復所需漲幅） |
| 隱含復原 CAGR | `(peak/trough)^(252 / 復原交易天) − 1` |

## 本地執行 / Run locally

```bash
cd drawdown_app
pip install -r requirements.txt
streamlit run app.py
```

瀏覽器開 <http://localhost:8501>。側邊欄輸入代碼（預設 `^GSPC`）、期間、
納入門檻（|回撤| ≥ N%）與信賴水準即可。

## 資料來源 / Data sources（防呆多路回退）

依序嘗試，UI 會透明顯示走到哪一路：

1. **上傳 CSV** — 側邊欄上傳（含 `Date, Close`），離線可用、最高優先。
2. **線上 yfinance** — 依代碼線上抓取（快取 1 小時）。
3. **本地備援** — `data/<ticker>.csv`（見 `data/README.md`）。

> 若你的網路／代理封鎖 yfinance，放一份真實 `data/^GSPC.csv` 即可完整使用與驗收。

## 部署到 Streamlit Cloud / Deploy

1. 將本專案推上 GitHub（本 app 位於 `drawdown_app/` 子資料夾）。
2. [share.streamlit.io](https://share.streamlit.io) → **New app**。
3. 選 repo 與分支，**Main file path** 填 `drawdown_app/app.py`。
4. Deploy。相依由 `drawdown_app/requirements.txt` 自動安裝；主題由
   `drawdown_app/.streamlit/config.toml` 套用。

### 相依鎖版 / Pinning（重要）

`requirements.txt` 對原生套件釘**上限**，特別是 **`pyarrow<25`**：
事件表用 `st.dataframe` → 必經 pyarrow C++ 核心，而 pyarrow 25.x 在 Streamlit
Cloud（cp314）重繪時會整站 **segfault**（本專案已 A/B 實證）。若雲端相依漂版到
≥25，app 開站會以 `st.error` 明白告警。**請勿解除此上限。**

## 驗收 / Acceptance

- **`r ≈ 0.85`（真實 `^GSPC`）**：此指標需以**真實** S&P 500 歷史資料驗證。
  本開發沙箱代理封鎖 yfinance，無法在此重現；請在雲端執行，或放
  `data/^GSPC.csv` 後於 Tab 3 檢視（X=required_gain、Y=recovery_tdays 為預設對照，
  可切換 X/Y 找出最貼近 0.85 的配對；實際數值隨門檻與期間而定）。
- 啟動零錯誤、5 分頁皆可渲染、事件表與相關指標數值合理。

## 檔案結構 / Layout

```
drawdown_app/
├── app.py                 # Streamlit 入口（5 分頁、防呆載入、Plotly）
├── drawdown_core.py       # 分析引擎（依規格重建；向量化＋純 numpy 統計）
├── requirements.txt       # 鎖版（pyarrow<25、無 scipy）
├── README.md
├── .streamlit/
│   └── config.toml        # 主題與伺服器設定
└── data/
    └── README.md          # 離線／備援 CSV 格式與來源
```

---
資料僅供研究，非投資建議 / For research only, not investment advice.
