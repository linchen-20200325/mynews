"""taiwan_map_data.py — 台灣就業人口 × 空屋率資料的單一真相源 (SSOT)。

真實資料接入說明
──────────────────────────────────────────────────────────────────
① 就業人口（勞保投保人數）
   來源：勞動部勞保局「勞工保險被保險人實際投保薪資、人數統計表」
   URL  : https://www.bli.gov.tw/0015094.html（選「縣市別」下載 Excel/CSV）
   欄位 : 縣市別、被保險人數  →  對應本模組 county / employment
   清洗 : 台/臺 統一用臺；去掉「合計」列；數字去千分位逗號轉 int

② 空屋率（低度使用住宅比率）
   來源：內政部不動產資訊平台「低度使用（用電）住宅統計」
   URL  : https://pip.moi.gov.tw/V3/E/SCRE0104.aspx（選縣市、下載 Excel）
   欄位 : 縣市別、低度使用住宅比率(%)  →  對應本模組 county / vacancy_rate
   清洗 : 同上臺/台正規化；去掉全國合計列

接入步驟：把上述兩份資料合併成 DataFrame，確認欄位對齊後
   替換 `_mock_df()` 的回傳值，`load_df()` 呼叫端無需異動。
──────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import pandas as pd


# 轉向分數定義：空屋率高 + 就業人口少 → 轉向潛力大
# 歸一化就業人口至 [0,1]，分數 = vacancy_rate / (employment_norm + 0.15)
# 分數 > 中位數 → 標為「就業轉向潛力區」
_TRANSITION_THRESHOLD_PERCENTILE = 60  # 第 60 百分位以上才標旗


def _mock_df() -> pd.DataFrame:
    """22 縣市 Mock 資料（勞保投保人數萬人 × 低度使用住宅比率%）。

    數值參考歷年統計趨勢合理虛擬；替換真實資料時只改此函數。
    縣市名稱使用官方「臺」字，與 taiwan_counties.geo.json 一致。
    """
    rows = [
        # 六都：就業人口多、空屋率低
        {"county": "臺北市",  "employment": 1_580_000, "vacancy_rate": 6.2},
        {"county": "新北市",  "employment": 1_430_000, "vacancy_rate": 8.5},
        {"county": "桃園市",  "employment":   920_000, "vacancy_rate": 9.1},
        {"county": "臺中市",  "employment":   980_000, "vacancy_rate": 10.3},
        {"county": "臺南市",  "employment":   490_000, "vacancy_rate": 12.8},
        {"county": "高雄市",  "employment":   720_000, "vacancy_rate": 11.6},
        # 准都/工業型：中高就業
        {"county": "新竹市",  "employment":   210_000, "vacancy_rate": 7.4},
        {"county": "新竹縣",  "employment":   230_000, "vacancy_rate": 9.8},
        {"county": "基隆市",  "employment":   130_000, "vacancy_rate": 13.5},
        # 中型縣：農工業混合
        {"county": "彰化縣",  "employment":   330_000, "vacancy_rate": 15.7},
        {"county": "苗栗縣",  "employment":   140_000, "vacancy_rate": 17.2},
        {"county": "南投縣",  "employment":    85_000, "vacancy_rate": 19.4},
        {"county": "嘉義市",  "employment":    90_000, "vacancy_rate": 14.6},
        {"county": "嘉義縣",  "employment":    95_000, "vacancy_rate": 21.3},
        {"county": "雲林縣",  "employment":   115_000, "vacancy_rate": 23.8},
        {"county": "屏東縣",  "employment":   145_000, "vacancy_rate": 20.1},
        # 宜花東：人口少、空屋率偏高
        {"county": "宜蘭縣",  "employment":    80_000, "vacancy_rate": 16.9},
        {"county": "花蓮縣",  "employment":    52_000, "vacancy_rate": 24.5},
        {"county": "臺東縣",  "employment":    33_000, "vacancy_rate": 27.3},
        # 離島：人口極少、空屋率最高
        {"county": "澎湖縣",  "employment":    24_000, "vacancy_rate": 29.1},
        {"county": "金門縣",  "employment":    18_000, "vacancy_rate": 31.6},
        {"county": "連江縣",  "employment":     5_000, "vacancy_rate": 35.2},
    ]
    return pd.DataFrame(rows)


def load_df() -> pd.DataFrame:
    """載入就業人口 × 空屋率 DataFrame，自動計算轉向分數與旗標。

    欄位說明
    ─────────
    county          : 縣市名稱（臺字）
    employment      : 勞保投保人數（人）
    vacancy_rate    : 低度使用住宅比率（%）
    employment_wan  : 就業人口（萬人，顯示用）
    transition_score: 轉向分數（空屋率高 + 就業人口少 → 分數高）
    is_transition   : bool，轉向潛力旗標（≥第60百分位）
    """
    df = _mock_df().copy()

    emp_max = df["employment"].max()
    emp_norm = df["employment"] / emp_max  # 0~1，越大越多
    df["transition_score"] = df["vacancy_rate"] / (emp_norm + 0.15)

    threshold = df["transition_score"].quantile(_TRANSITION_THRESHOLD_PERCENTILE / 100)
    df["is_transition"] = df["transition_score"] >= threshold
    df["employment_wan"] = (df["employment"] / 10_000).round(1)

    return df.reset_index(drop=True)
