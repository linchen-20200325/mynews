# data/ — 離線／備援價格資料

本資料夾放**真實**歷史價格 CSV，供兩種情境：

1. **離線／代理封鎖時的備援**：當 `yfinance` 線上抓取失敗（如沙箱代理封鎖、
   雲端暫時抓不到），app 會自動回退讀取本資料夾的 `<ticker>.csv`。
2. **可重現的驗收**：把真實 `^GSPC.csv` 放這裡，即可離線重現接受度指標
   （跌幅 vs 復原的 Pearson r），不受網路波動影響。

## 檔名規則

檔名＝**yfinance 代碼**，例：

| 代碼 | 檔名 | 說明 |
|------|------|------|
| `^GSPC` | `^GSPC.csv` | S&P 500 |
| `^TWII` | `^TWII.csv` | 台股加權指數 |
| `^IXIC` | `^IXIC.csv` | Nasdaq 綜合 |

## CSV 格式（最小）

必須含**日期欄**與**收盤欄**（欄名大小寫不拘，支援中英）：

```csv
Date,Close
1990-01-02,359.69
1990-01-03,358.76
...
```

- 日期欄接受 `Date` / `Datetime` / `日期`。
- 收盤欄接受 `Close` / `Adj Close` / `adj_close` / `收盤` / `收盤價`（優先 `Close`）。
- **選用 `Low` 欄**（`Low` / `最低`）：側欄「峰頂基準 = 盤中最低 low」時會用到；缺此欄時該選項自動降級為收盤。
- 其餘欄（Open/High/Volume）會被忽略，可留可刪。
- 收盤建議與分析目的一致；app 線上路徑用 `auto_adjust=False`（優先原始 `Close`、其次 `Adj Close`）。

## 從哪裡取得真實資料

- **yfinance 匯出**（有網路的機器上）：
  ```python
  import yfinance as yf
  yf.download("^GSPC", start="1990-01-01", auto_adjust=True)[["Close"]] \
      .to_csv("^GSPC.csv")
  ```
- Stooq、各交易所官方歷史資料下載頁亦可，整理成上表格式即可。

> ⚠️ 一律使用**真實**市場資料。切勿放入捏造／模擬數列充當真實行情
> （違反本專案「真實數據鐵律」）。此資料夾不放任何金鑰或機密。
