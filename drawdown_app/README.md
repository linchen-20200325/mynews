# 🐻 熊市回撤 vs 修復分析 / Bear-market Drawdown vs Recovery

可直接部署到 **Streamlit Community Cloud** 的互動看板：以 **reclaim-prior-peak
（收復前高才算修復）** 定義切出歷史所有熊市，重現並延伸「跌得多深 → 要多久才站回
前高」的分析——V 型疊圖、深度—時間散布迴歸、以及完整統計檢定（含 p 值）。

> ⚠️ **`drawdown_core.py` 為「依規格重建」版本**：原版當時未提供，本引擎依書面定義
> 嚴格重建，**核心定義未更動**。請與你手上的原版 `diff` 確認指標語意一致。

---

## 功能 / Features（5 分頁，全 Plotly，中英雙語）

1. **① 熊市清單 / List** — 可排序表格：峰/谷/修復、drawdown、required_gain、下跌/修復/
   往返（日曆天＋交易天）、隱含修復 CAGR、資料來源標記；附 CSV 下載。
2. **② V 型疊圖 / V-shape** — 每次熊市一條 normalized（峰頂=100）對「距峰頂天數」折線，
   谷底標點，hover 顯示日期/跌幅，x 軸範圍 slider（完整往返 ↔ 前 N 天放大）。
3. **③ 深度 vs 時間 / Depth vs Time** — x=跌幅 |%|，y 可切修復天數/全程天數；疊 OLS 迴歸線，
   標 Pearson r（含信賴區間）、Spearman ρ、R²，每點標年份。
4. **④ 統計面板 / Stats** — 對 recovery / roundtrip / decline 各列 Pearson（r + CI + **p**）、
   Spearman（ρ + **p**）、OLS（截距 / 斜率 / R² / 斜率 **p**）；並提供 **leave-one-out**
   敏感度（multiselect 排除任意幾次熊市後重算）。
5. **⑤ 修復速度 / Recovery speed** — 隱含修復 CAGR 長條，排序 + 中位數水平線。

## 核心定義 / Definitions

| 名詞 | 定義 |
|------|------|
| 水下 underwater | 收盤 < 歷史新高（cummax，嚴格小於） |
| 峰頂 peak | 進入水下前的歷史新高（水下期間 ATH 不變） |
| 谷底 trough | 事件區間內最低收盤 |
| 修復 recovery | 谷底後首個「收盤 ≥ 峰頂」之日（收復前高） |
| drawdown | `trough/peak − 1`（≤ 0） |
| required_gain | `peak/trough − 1`（≥ 0） |
| 隱含修復 CAGR | `(peak/trough)^(252 / 修復交易天) − 1` |

## 側欄控制 / Controls

指數 ticker（^GSPC / ^TWII / ^IXIC / ^DJI / 自訂）、起始日（預設 **1970-01-01**）、
熊市門檻（預設 **−20%**，−5%~−60%）、**峰頂基準 收盤/盤中最低**、**天數單位 交易天/日曆天**、
**是否納入尚未修復（ongoing）**、信賴水準、備援資料來源。

## 本地執行 / Run locally

```bash
cd drawdown_app
pip install -r requirements.txt
streamlit run app.py
```

瀏覽器開 <http://localhost:8501>。

## 資料來源 / Data sources（防禦式多來源，依序回退）

1. **上傳 CSV**（側欄，含 `Date, Close`，可含 `Low`）——最高優先、離線可用。
2. **GitHub raw CSV URL**（側欄貼直鏈）。
3. **yfinance 線上**（`auto_adjust=False`，優先 `Close`、其次 `Adj Close`；快取 1 小時）。
4. **本地備援** `data/<ticker>.csv`（見 `data/README.md`）。

> API 逾時／限流／空表／全 NaN／重複或亂序日期／資料不足皆已防呆並友善提示，不會讓 App 崩潰。
> 峰頂基準選「盤中最低 low」時需資料含 `Low` 欄；缺 `Low` 會自動降級為收盤並提示。

## 部署到 Streamlit Community Cloud / Deploy

1. 將本專案推上 GitHub（本 app 位於 `drawdown_app/` 子資料夾）。
2. [share.streamlit.io](https://share.streamlit.io) → **New app** → 連結 GitHub repo 與分支。
3. **Main file path** 填 `drawdown_app/app.py`；Python 版本用預設（3.11+ 皆可）。
4. Deploy。相依由 `drawdown_app/requirements.txt` 自動安裝、主題由 `.streamlit/config.toml` 套用。

### 相依鎖版 / Pinning（重要）

`requirements.txt` 對原生套件釘**上限**，特別是 **`pyarrow<25`**：熊市清單用 `st.dataframe`
→ 必經 pyarrow C++ 核心，pyarrow 25.x 在 cp314 重繪會整站 **segfault**（本專案已 A/B 實證）。
`scipy` 提供 p 值；`drawdown_core` 在 scipy 缺席時會退回純 numpy（Fisher-z CI、p 值從缺）不硬崩。
**請勿解除 `pyarrow<25`。**

## 驗收 / Acceptance

1. `streamlit run app.py` **零錯誤啟動**。
2. 以 **^GSPC、起始 1970-01-01、門檻 −20%、收盤基準** 執行，熊市數 **約 8 次**，且
   「**recovery_days vs 跌幅**」的 **Pearson r ≈ +0.85**（需與原始分析一致）。
   > ⚠️ 此指標需**真實 ^GSPC** 資料驗證。開發沙箱 yfinance 被代理封鎖且為 cp311（非雲端
   > cp314），**無法在沙箱重現**；請於雲端執行，或放 `data/^GSPC.csv` 後於 Tab③ 檢視。
3. 熱路徑無逐列迴圈；抓取失敗自動回退備援並友善提示，而非崩潰。

## 檔案結構 / Layout

```
drawdown_app/
├── app.py                 # Streamlit 進入點（5 分頁、防禦式載入、Plotly）
├── drawdown_core.py       # 分析引擎（依規格重建；向量化 + scipy 統計、numpy 退路）
├── requirements.txt       # 鎖版（pyarrow<25、scipy 釘上限）
├── README.md
├── .streamlit/config.toml # 主題與伺服器設定
└── data/
    └── README.md          # 離線／備援 CSV 格式與來源
```

---
資料僅供研究，非投資建議 / For research only, not investment advice.
