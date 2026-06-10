"""每日全球政經戰略情報自動產生器(RSS 爬蟲 + Gemini)。

資料流:
  1. news_fetcher (RSS):從具公信力的新聞來源抓真實外電(標題/來源/連結/摘要)。
  2. Gemini (gemini-2.5-flash):讀取抓回來的新聞 →
       A. 戰略報告 (latest_report.json):四維度深度戰略分析 + 白話文字典。
          raw_news 直接採用爬蟲抓到的真實新聞,絕不虛構。
       B. 趨勢雷達 (latest_trends.json):依「資金/徵才/政策/技術」四訊號排名打分,
          回答「現在最紅的產業是什麼」。 [可用 ENABLE_TREND_RADAR=0 關閉]
       C. 台股觀察 (latest_stocks.json):從台灣財經新聞統計被提到最多次的台股標的,
          分利多/利空/觀望,並歸納未來趨勢與夕陽產業。 [可用 ENABLE_STOCK_PICKER=0 關閉]
       D. 房市觀察 (latest_housing.json):從房市新聞判讀預售/成屋冷熱 + 打房政策 +
          縣市標記(房價另由 housing_fetcher 走代理抓實價登錄)。 [可用 ENABLE_HOUSING=0 關閉]
  3. 可選:把摘要推播到 LINE (Messaging API)。

環境變數:
  - GEMINI_API_KEY                 (必填) Gemini 金鑰
  - GEMINI_MODEL                   (選填) Gemini 模型,預設 gemini-2.5-flash
  - GEMINI_MAX_TOKENS              (選填) 單次輸出 token 上限,預設 8192
  - REPORT_TOPIC                   (選填) 戰略報告的分析主題(單一)
  - REPORT_TOPICS                  (選填) 多主題戰略報告,以 ; 分隔(第一個為主報告)
  - NEWS_QUERIES                   (選填) 戰略報告抓新聞的關鍵字,以 ; 分隔
  - TREND_QUERIES                  (選填) 趨勢雷達抓新聞的關鍵字,以 ; 分隔
  - NEWS_TOPICS / TREND_TOPICS     (選填) Google News 動態分類頭條,以 , 分隔
                                          (WORLD/BUSINESS/TECHNOLOGY/NATION…)
  - NEWS_LANG / NEWS_REGION        (選填) Google News 語系/地區,預設 zh / TW
  - NEWS_MAX / NEWS_SINCE_HOURS    (選填) 抓新聞則數上限 / 回溯時數,預設 12 / 48
  - STOCK_QUERIES                  (選填) 台股觀察抓新聞的關鍵字,以 ; 分隔
  - ENABLE_TREND_RADAR             (選填) 設為 0/false/no 可關閉趨勢雷達
  - ENABLE_STOCK_PICKER            (選填) 設為 0/false/no 可關閉台股觀察
  - ENABLE_HOUSING                 (選填) 設為 0/false/no 可關閉房市觀察
  - HOUSING_MAX / HOUSING_SINCE_HOURS (選填) 房市抓新聞則數上限 / 回溯時數,預設 18 / 72
  - LINE_CHANNEL_ACCESS_TOKEN/LINE_TO (選填) 兩者皆設才推播
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import chip_calendar  # 法人籌碼:可預測賣壓事件行事曆(純規則,零網路零 AI)
import chip_fetcher  # 法人籌碼:抓證交所三大法人買賣超(事後驗證,真實數字)
import housing_fetcher
import index_fetcher  # 國際盤預警:抓美股指數/KOSPI/美股期貨真實漲跌幅
import news_fetcher

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# LINE Messaging API(LINE Notify 已於 2025 停用,改用 Messaging API push)
LINE_PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"
LINE_TEXT_LIMIT = 4500  # 單則 text 上限 5000,留安全餘裕

DEFAULT_TOPIC = (
    "全球政經與市場動態:聯準會利率與通膨、地緣政治與軍事衝突、美中科技戰、"
    "原油與股匯債走勢,及其對股市與基金的影響"
)

# 抓新聞用的預設關鍵字(可用 NEWS_QUERIES / TREND_QUERIES 覆寫)。
# 聚焦:國際政治、軍事、財經(尤其聯準會),以及會牽動股市/基金的訊息。
DEFAULT_NEWS_QUERIES = [
    "聯準會 利率 通膨",
    "美股 台股 盤勢",
    "地緣政治 軍事 衝突",
    "央行 貨幣政策 債市",
]
# 戰略報告的英文側(全球外電,避免只看到中文報導而漏掉國際原文)
DEFAULT_NEWS_QUERIES_EN = [
    "Federal Reserve interest rate inflation",
    "geopolitics military conflict war",
    "central bank monetary policy bond market",
    "global stock market outlook",
]
DEFAULT_TREND_QUERIES = [
    "類股 題材 資金流向",
    "AI 半導體 投資",
    "產業 趨勢 基金",
]
# 趨勢雷達的美股/全球面向(英文,讓熱門產業排名也反映美國市場)
DEFAULT_US_TREND_QUERIES = [
    "US stock market sector trends",
    "AI semiconductor investment",
    "venture capital funding hot sectors",
]
# 前五個章節(報告/趨勢/台股/美股/人物)新聞回溯視窗 ~6 個月。
# 註:Google News RSS 實際只回傳近期新聞,拉長視窗只代表「不過濾較舊的」,
# 真正能回溯多久仍受 RSS 來源限制。
SIX_MONTHS_HOURS = 24 * 183
DEFAULT_STOCK_QUERIES = [
    "台股 個股 焦點",
    "台積電 聯發科 鴻海 台股",
    "台股 外資 法人 買超",
    "台股 類股 漲跌",
    "上市 上櫃 營收 財報",
]
# 台股觀察的英文側(國際媒體對台股/台積電等的報導)
DEFAULT_STOCK_QUERIES_EN = [
    "Taiwan stock market TWSE",
    "TSMC MediaTek Hon Hai Taiwan stocks",
    "Taiwan shares foreign investors",
]
DEFAULT_US_STOCK_QUERIES = [
    "US stock market today",
    "Nvidia Apple Microsoft Tesla stock",
    "Nasdaq S&P 500 Dow Jones",
    "US earnings revenue guidance",
    "Federal Reserve rate cut tech stocks",
]
# 美股觀察的中文側(台灣媒體對美股的報導,避免漏掉中文角度)
DEFAULT_US_STOCK_QUERIES_ZH = [
    "美股 個股 焦點",
    "輝達 蘋果 微軟 特斯拉 美股",
    "那斯達克 標普 道瓊",
    "美股 財報 營收",
    "聯準會 美股 科技股",
]
# 全球人物追蹤每日排程預設追蹤對象(中文;可用 FOCUS_TOPICS 以 ; 覆寫)
DEFAULT_FOCUS_TOPICS = [
    "川普",
    "黃仁勳",
]
# 台媒自家 RSS(直接來源,補 Google News 排名外的台灣報導);人物追蹤會「先過濾出有提到
# 該對象者」才納入,避免灌入無關新聞。抓不到的 feed 會自動略過(fetch_news 容錯)。
TW_MEDIA_FEEDS = {
    "自由時報 即時": "https://news.ltn.com.tw/rss/all.xml",
    "自由時報 財經": "https://ec.ltn.com.tw/rss/all.xml",
    "經濟日報": "https://money.udn.com/rssfeed/news/1001/5590?ch=money",
}

# Google News 分類頭條(不帶關鍵字的『動態』來源,確保不漏突發大事;
# 只取與主題相關的分類,避免娛樂/體育等離題內容)。可用 NEWS_TOPICS / TREND_TOPICS 覆寫。
DEFAULT_NEWS_TOPICS = ["WORLD", "BUSINESS"]
DEFAULT_TREND_TOPICS = ["BUSINESS", "TECHNOLOGY"]

_SECTION_LABELS = {
    "WORLD": "Google 世界頭條",
    "BUSINESS": "Google 財經頭條",
    "TECHNOLOGY": "Google 科技頭條",
    "NATION": "Google 國內頭條",
    "SCIENCE": "Google 科學頭條",
}

OUTPUT_LATEST = Path("latest_report.json")
ARCHIVE_DIR = Path("data/reports")
OUTPUT_REPORTS_MULTI = Path("latest_reports.json")
REPORTS_MULTI_ARCHIVE_DIR = Path("data/reports_multi")
OUTPUT_TRENDS = Path("latest_trends.json")
TRENDS_ARCHIVE_DIR = Path("data/trends")
OUTPUT_STOCKS = Path("latest_stocks.json")
STOCKS_ARCHIVE_DIR = Path("data/stocks")
OUTPUT_US_STOCKS = Path("latest_us_stocks.json")
US_STOCKS_ARCHIVE_DIR = Path("data/us_stocks")
OUTPUT_INTL_ALERT = Path("latest_intl_alert.json")
INTL_ALERT_ARCHIVE_DIR = Path("data/intl_alert")
OUTPUT_CHIP = Path("latest_chip.json")
CHIP_ARCHIVE_DIR = Path("data/chip")
CHIP_PUSHED_STATE = Path("data/chip_pushed.json")  # 法人事件 LINE 已推清單(防洗版)
OUTPUT_FOCUS = Path("latest_focus.json")
FOCUS_ARCHIVE_DIR = Path("data/focus")
OUTPUT_HOUSING = Path("latest_housing.json")
HOUSING_ARCHIVE_DIR = Path("data/housing")

# 全台 22 縣市(官方「臺」),房市分區標記只能用這些名稱
TAIWAN_COUNTIES = list(housing_fetcher.CITY_CODES.values())

# 報告所需的頂層欄位 — 解析後做最低限度的結構驗證
REQUIRED_TOP_LEVEL_KEYS = (
    "report_date",
    "topic",
    "raw_news",
    "strategic_analysis",
    "laymans_dictionary",
)
REQUIRED_ANALYSIS_KEYS = (
    "geo_military",
    "supply_chain",
    "macro_economy",
    "blind_spots_and_kpi",
)

# ---------------------------------------------------------------------------
# 系統提示語(維持穩定,利於模型快取)
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM_PROMPT = """\
你是一位兼具「全球宏觀首席策略官」與「後端資料工程師」的純資料生成器。
你會收到一批【已由爬蟲抓取的真實新聞】(含標題、來源、連結、摘要)。
你的任務是:【只根據這些真實新聞】與你的專業知識,進行四維度深度戰略分析,
並產生白話文字典,最後【嚴格且唯一】地輸出一份合法 JSON。

【分析聚焦】聚焦國際政治、軍事與財經(尤其聯準會/央行的利率與貨幣政策),
並務必說明這些事件對【股市、債市、原物料、基金與資產配置】的可能影響;
與此主題無關的新聞(娛樂、體育、地方社會等)可略過不分析。

【資料真實性】
1. 分析只能立基於使用者提供的新聞,嚴禁虛構任何未提供的事件、數據或來源。
2. 若提供的新聞不足以支撐某個維度,就誠實說明資訊有限,不要編造。

【強制輸出規範:Zero-Tolerance】
1. 你最終回覆的文字內容必須【只有】一個合法 JSON 物件,前後不得有任何其他文字或 ```json 標記。
2. 輸出必須能被 Python 的 json.loads() 直接解析。
3. strategic_analysis 四個欄位請用「專業口吻」撰寫,可自由使用專業術語。
4. 凡是在 strategic_analysis 中使用到的專業經濟/軍事/金融術語,都必須挑出來放進
   laymans_dictionary,並用像對高中生講話一樣的生活化白話文(日常譬喻)解釋,至少 6 個。

【JSON 結構定義 — 必須完全符合】
{
  "strategic_analysis": {
    "geo_military": "【地緣政治與軍事戰略】分析內容...",
    "supply_chain": "【原物料與供應鏈傳導】分析內容...",
    "macro_economy": "【總體經濟與貨幣定價】分析內容...",
    "blind_spots_and_kpi": "【全球大局觀與領先指標】分析內容(含台灣盲點 + 2~3 個應緊盯的領先指標)..."
  },
  "laymans_dictionary": [
    { "term": "專業術語", "explanation": "像對高中生解釋一樣的生活化白話文" }
  ]
}
"""

TREND_SYSTEM_PROMPT = """\
你是一位兼具「產業趨勢分析師」與「後端資料工程師」的純資料生成器。
你會收到一批【已由爬蟲抓取的真實新聞】做為佐證素材。
你的任務是:綜合這些新聞與你的專業知識,找出當前全球「最熱門、動能最強」的
3~5 個新興產業或主題(例如過去是『網路』、現在是『AI』這類等級的趨勢),
依四種訊號綜合評估、排名打分,並【嚴格且唯一】地輸出一份合法 JSON。

【四種訊號(用來判斷產業熱度,不只看新聞聲量)】
- funding   :資金流向 — 創投/私募募資輪次、估值、企業資本支出 (capex)
- hiring    :徵才動能 — 職缺數量趨勢、人才搶奪
- policy    :政策/法規動向 — 補貼、管制、國家戰略
- technology:技術動能 — 重大突破、專利、開源熱度

【含美股】提供的新聞同時包含台灣與美國(英文)市場;每個產業都要【務必】列出
代表性的美股個股 us_stocks(如 AI→NVDA、雲端→MSFT),以及台股代表個股 tw_stocks
(如 AI→2330 台積電)。個股要與該產業真正相關,沒有把握就少列、不要硬湊或虛構代號。

【真實性】evidence_news 請優先引用使用者提供的真實新聞,嚴禁虛構標題/媒體/數據。

【強制輸出規範:Zero-Tolerance】
1. 最終回覆只能有一個合法 JSON 物件,前後不得有任何其他文字或 ```json 標記。
2. 必須能被 Python json.loads() 解析。
3. heat_score 為 0~100 整數,代表綜合熱度;trends 依 heat_score 由高到低排序。
4. us_stocks / tw_stocks 各最多 3 檔;evidence_news 最多 3 則。

【JSON 結構定義 — 必須完全符合】
{
  "report_date": "YYYY-MM-DD",
  "trends": [
    {
      "rank": 1,
      "industry": "產業/主題名稱",
      "heat_score": 92,
      "signals": {
        "funding": "資金流向觀察...",
        "hiring": "徵才動能觀察...",
        "policy": "政策動向觀察...",
        "technology": "技術動能觀察..."
      },
      "leading_indicators": ["未來1-3個月該緊盯的具體領先指標", "..."],
      "us_stocks": [ { "name": "輝達(Nvidia)", "ticker": "NVDA" } ],
      "tw_stocks": [ { "name": "台積電", "ticker": "2330" } ],
      "evidence_news": [
        { "title": "新聞標題", "source": "媒體來源", "url": "連結(若有)" }
      ],
      "summary": "一句話總結為何上榜"
    }
  ]
}
"""


STOCK_SYSTEM_PROMPT = """\
你是一位兼具「台股研究員」與「後端資料工程師」的純資料生成器。
你會收到一批【已由爬蟲抓取的真實台灣財經新聞】(含標題、來源、連結、摘要)。
你的任務是:【只根據這些真實新聞】,整理出新聞中被提到的台股標的(個股或類股),
判斷各自目前偏多/偏空,並歸納未來趨勢與夕陽產業,最後【嚴格且唯一】輸出合法 JSON。

【做法】
1. 找出新聞中出現的台股個股/類股,估算每個標的「被幾則新聞提到」(mention_count,
   以提供的新聞為準),依此由高到低排序,優先列出最常被提到的。
2. 依新聞內容判斷每個標的目前的傾向 sentiment,只能填三種之一:
   - "利多":新聞偏正面(營收成長、訂單、題材發酵、外資買超等)
   - "利空":新聞偏負面(衰退、砍單、利空消息、法人賣超等)
   - "觀望":多空不明、消息中性或雜訊,建議再觀察
3. reason 用一句白話說明為何歸到該類(務必對應新聞內容,不可臆測)。
4. future_trends:從新聞歸納「未來看好、動能向上」的趨勢產業/題材。
5. sunset_industries:歸納「轉弱、結構性走下坡、夕陽」的產業(若新聞沒提到可給空陣列)。

【真實性】
- 只能根據提供的新聞;個股名稱要正確,ticker(股票代號)若新聞未明確提及就留空字串,
  嚴禁亂編代號或虛構新聞、數據。
- 你是中立的資訊整理,不是投資建議;不要喊買賣、不要給目標價。

【強制輸出規範:Zero-Tolerance】
1. 最終回覆只能有一個合法 JSON 物件,前後不得有任何其他文字或 ```json 標記。
2. 必須能被 Python json.loads() 解析。

【JSON 結構定義 — 必須完全符合】
{
  "report_date": "YYYY-MM-DD",
  "summary": "一句話總結今日台股新聞焦點",
  "stocks": [
    {
      "name": "個股或類股名稱",
      "ticker": "股票代號(沒有就空字串)",
      "sector": "所屬產業",
      "mention_count": 3,
      "sentiment": "利多",
      "reason": "依新聞說明偏多/偏空/觀望的原因",
      "evidence_news": [
        { "title": "新聞標題", "source": "媒體來源", "url": "連結(若有)" }
      ]
    }
  ],
  "future_trends": ["未來看好的趨勢產業/題材", "..."],
  "sunset_industries": ["轉弱或夕陽產業", "..."]
}
"""


US_STOCK_SYSTEM_PROMPT = """\
你是一位兼具「美股研究員」與「後端資料工程師」的純資料生成器。
你會收到一批【已由爬蟲抓取的真實美股相關財經新聞,內容多為英文原文】(含標題、來源、連結、摘要)。
你的任務是:【只根據這些真實新聞】,整理出新聞中被提到的美股標的(個股或類股),
判斷各自目前偏多/偏空,並歸納未來趨勢與夕陽產業,最後【嚴格且唯一】輸出合法 JSON。

【語言】輸入新聞是英文,但你的所有輸出一律使用【繁體中文】:summary、sector、reason、
future_trends、sunset_industries 都要中文;個股 name 用中文慣用名(可在後面用括號補英文,
例如「輝達(Nvidia)」);evidence_news 的 title 也請翻成繁體中文。ticker 保留原始英文代號。

【做法】
1. 找出新聞中出現的美股個股/類股,估算每個標的「被幾則新聞提到」(mention_count,
   以提供的新聞為準),依此由高到低排序,優先列出最常被提到的。
2. 依新聞內容判斷每個標的目前的傾向 sentiment,只能填三種之一:
   - "利多":新聞偏正面(營收成長、訂單、題材發酵、機構買超等)
   - "利空":新聞偏負面(衰退、砍單、利空消息、機構賣超等)
   - "觀望":多空不明、消息中性或雜訊,建議再觀察
3. reason 用一句白話說明為何歸到該類(務必對應新聞內容,不可臆測)。
4. future_trends:從新聞歸納「未來看好、動能向上」的趨勢產業/題材。
5. sunset_industries:歸納「轉弱、結構性走下坡、夕陽」的產業(若新聞沒提到可給空陣列)。

【真實性】
- 只能根據提供的新聞;個股名稱要正確,ticker(美股代號,如 NVDA、AAPL)若新聞未明確
  提及就留空字串,嚴禁亂編代號或虛構新聞、數據。
- 你是中立的資訊整理,不是投資建議;不要喊買賣、不要給目標價。

【輸出精簡(避免過長被截斷)】
- stocks 最多列 12 檔(以被提及次數最高者優先);每檔 evidence_news 最多 2 則。
- reason 控制在 60 字內;不要重複貼整段新聞內容。

【強制輸出規範:Zero-Tolerance】
1. 最終回覆只能有一個合法 JSON 物件,前後不得有任何其他文字或 ```json 標記。
2. 必須能被 Python json.loads() 解析。

【JSON 結構定義 — 必須完全符合】
{
  "report_date": "YYYY-MM-DD",
  "summary": "一句話總結今日美股新聞焦點",
  "stocks": [
    {
      "name": "個股或類股名稱",
      "ticker": "美股代號(沒有就空字串)",
      "sector": "所屬產業",
      "mention_count": 3,
      "sentiment": "利多",
      "reason": "依新聞說明偏多/偏空/觀望的原因",
      "evidence_news": [
        { "title": "新聞標題", "source": "媒體來源", "url": "連結(若有)" }
      ]
    }
  ],
  "future_trends": ["未來看好的趨勢產業/題材", "..."],
  "sunset_industries": ["轉弱或夕陽產業", "..."]
}
"""


INTL_ALERT_SYSTEM_PROMPT = """\
你是一位「全球股市策略分析師」兼純資料生成器,專長【利用時區時間差預判台股】。
你會收到兩份真實資料:
  (A)【真實指數/期貨報價漲跌幅】(已由程式抓自 Yahoo Finance,含當日漲跌%);
  (B)【真實財經新聞】(美股 + 韓股,多為英文原文,含標題/來源/連結/摘要)。

時區常識(供你判斷時間差):
  - 美股指數收盤約台灣時間清晨,對台股開盤是【隔夜領先】訊號;美股期貨是【盤前即時】風向。
  - 韓股 KOSPI 與台股近乎同步,屬【同步連動】半導體 peer 對照,非時間差領先。

你的任務:【只根據 (A) 的真實數字與 (B) 的真實新聞】,研判「美股/韓股是否出現突然大跌或重大利空」,
並推論「對今日/隔日台股的可能影響」,最後【嚴格且唯一】輸出合法 JSON。

【鐵則 — 數字真實性】
- 嚴禁竄改或自行編造任何漲跌幅數字;報價數字以程式提供的 (A) 為唯一依據,你的輸出【不要再列數字欄位】,
  只做文字研判。若要引用幅度,請照抄 (A) 給的值。
- 利空原因只能來自 (B) 的新聞;新聞沒提到就說「新聞未明確說明」,嚴禁臆測或虛構事件。
- 你是中立資訊整理,不是投資建議;不喊買賣、不給目標價/點位預測。

【輸出語言】一律繁體中文(evidence_news 的 title 也翻成繁中)。

【輸出精簡】interpretation 最多 5 條,每條 evidence_news 最多 2 則;文字精煉不重貼整段新聞。

【強制輸出規範:Zero-Tolerance】
1. 最終回覆只能有一個合法 JSON 物件,前後不得有任何其他文字或 ```json 標記。
2. 必須能被 Python json.loads() 解析。

【JSON 結構定義 — 必須完全符合】
{
  "report_date": "YYYY-MM-DD",
  "alert_level": "警戒|觀察|平靜",
  "summary": "一句話總結:美股/韓股是否大跌、台股要不要當心",
  "interpretation": [
    {
      "market": "美股|韓股|半導體類股…",
      "cause": "依新聞說明這波下跌/利空的原因(新聞沒提就寫『新聞未明確說明』)",
      "evidence_news": [ { "title": "新聞標題", "source": "媒體來源", "url": "連結(若有)" } ]
    }
  ],
  "tw_impact": {
    "direction": "偏空|偏多|中性",
    "reason": "依時間差與連動性,說明對台股(尤其半導體/電子)的可能影響",
    "sectors": ["可能受衝擊或受惠的台股族群", "..."]
  }
}
"""


FOCUS_TRANSLATE_SYSTEM_PROMPT = """\
你是一個精準的「中文 → 英文新聞檢索詞」轉換器。
使用者會給你一個中文的人物名、公司名或主題關鍵字(例如:川普、黃仁勳、輝達、AI 晶片)。
請輸出該對象在國際新聞中最常用的英文名稱與別名,供 Google News 英文檢索使用。

【規則】
1. query_en 填最通用、最常被國際媒體使用的英文主名(人名用全名,如 Donald Trump、
   Jensen Huang;公司用常用英文名,如 Nvidia)。
2. aliases 補 2~4 個常見的英文別名/頭銜/常見寫法(如 "President Trump"、"Jensen Huang Nvidia")。
3. zh_aliases 補 2~4 個常見的【中文別名/關聯詞】,供台灣中文新聞檢索用
   (例如 黃仁勳 → ["輝達","NVIDIA","輝達執行長"];川普 → ["特朗普","美國總統川普"]);
   若該對象就是公司,放公司中文名與相關代表人。
4. 若輸入本身已是英文,query_en 原樣保留並補別名;zh_aliases 補對應中文譯名。
5. note 用一句繁體中文說明這是誰/什麼。

【強制輸出規範:Zero-Tolerance】
只輸出一個合法 JSON,前後不得有任何其他文字或 ```json 標記,且能被 json.loads() 解析。

【JSON 結構 — 必須完全符合】
{
  "query_zh": "使用者輸入的中文原詞",
  "query_en": "最通用的英文檢索主名",
  "aliases": ["常見英文別名/頭銜", "..."],
  "zh_aliases": ["常見中文別名/關聯詞", "..."],
  "kind": "person|company|topic",
  "note": "一句話說明(繁體中文)"
}
"""


STOCK_QUERY_TRANSLATE_SYSTEM_PROMPT = """\
你是一個精準的「個股代號/名稱 正規化器」。
使用者會給你一檔股票,可能是中文名(台積電)、英文名(Nvidia)或代號(2330、NVDA)。
請判斷它是台股還是美股,並輸出供新聞檢索用的中英文名稱與別名。

【規則】
1. query_zh:該股最通用的中文名(台股用正式公司名如「台積電」;美股用慣用中文譯名如「輝達」)。
2. query_en:該股最常被國際/英文媒體使用的英文名(如 TSMC、Nvidia)。
3. ticker:股票代號(台股 4 碼數字如 2330;美股英文代號如 NVDA);無法確定就留空字串。
4. market:只能填 "台股" 或 "美股"(無法判斷時依代號/名稱合理推測,真的不行才填 "美股")。
5. aliases:2~4 個常見英文別名/簡稱(如 "Taiwan Semiconductor"、"NVDA")。
6. zh_aliases:2~4 個常見中文別名/關聯詞(如 台積電 → ["台積","TSMC","護國神山"];輝達 → ["NVIDIA","黃仁勳"])。
7. note:一句繁體中文說明這是哪家公司、做什麼的。

【強制輸出規範:Zero-Tolerance】
只輸出一個合法 JSON,前後不得有任何其他文字或 ```json 標記,且能被 json.loads() 解析。

【JSON 結構 — 必須完全符合】
{
  "query_zh": "中文公司名",
  "query_en": "英文公司名",
  "ticker": "代號(沒有就空字串)",
  "market": "台股",
  "aliases": ["英文別名", "..."],
  "zh_aliases": ["中文別名", "..."],
  "note": "一句話說明(繁體中文)"
}
"""


FOCUS_SYSTEM_PROMPT = """\
你是一位「全球財經情報分析師」兼純資料生成器。
你會收到一個【關注對象】(人物/公司/主題)與一批【已由爬蟲抓取的真實英文新聞】
(含標題、來源、連結、摘要)。請【只根據這些真實新聞】整理情報,最後【嚴格且唯一】輸出合法 JSON。

【任務】
1. summary:一句話總結這個對象近期的新聞焦點。
2. key_statements:這個對象「說了什麼、做了什麼」的重點(條列;新聞沒提到就給空陣列)。
3. affected_industries:由其言行/事件衍伸、可能受影響的產業。
4. stocks:可能受影響的具體個股,【台股與美股都要找】(務必對應新聞,不可臆測):
   - market 只能填 "台股" 或 "美股"
   - sentiment 只能填 "利多" / "利空" / "觀望"
   - ticker:美股代號(如 NVDA)或台股代號(如 2330);新聞沒明確就留空字串
   - reason:一句話說明「為何此事件會牽動這檔股票」+ 偏多/偏空/觀望的理由

【語言】輸入新聞多為英文,但你的所有輸出一律使用【繁體中文】(個股 name 用中文慣用名,
可用括號補英文,如「輝達(Nvidia)」);evidence_news 的 title 也請翻成繁體中文。ticker 保留英文/數字代號。

【真實性】只能根據提供的新聞,嚴禁虛構新聞、數據或代號;你是中立的資訊整理,
不是投資建議,不要喊買賣、不要給目標價。

【輸出精簡(避免過長被截斷)】
- stocks 台股、美股合計最多 12 檔(關聯最明確者優先);每檔 evidence_news 最多 2 則。
- key_statements 最多 6 點;reason 控制在 60 字內;頂層 evidence_news 最多 8 則。

【強制輸出規範:Zero-Tolerance】
只輸出一個合法 JSON,前後不得有任何其他文字或 ```json 標記,且能被 json.loads() 解析。

【JSON 結構 — 必須完全符合】
{
  "report_date": "YYYY-MM-DD",
  "query_zh": "關注對象(中文)",
  "query_en": "英文檢索主名",
  "summary": "一句話總結",
  "key_statements": ["他說了/做了什麼的重點", "..."],
  "affected_industries": ["受影響產業", "..."],
  "stocks": [
    {
      "name": "個股名稱(中文,可附英文)",
      "ticker": "代號(沒有就空字串)",
      "market": "台股",
      "sector": "所屬產業",
      "sentiment": "利多",
      "reason": "依新聞說明此事件如何牽動本檔 + 偏多/偏空原因",
      "evidence_news": [
        { "title": "新聞標題(翻成繁體中文)", "source": "媒體來源", "url": "連結(若有)" }
      ]
    }
  ],
  "evidence_news": [
    { "title": "新聞標題(翻成繁體中文)", "source": "媒體來源", "url": "連結(若有)" }
  ]
}
"""


STOCK_QUERY_SYSTEM_PROMPT = """\
你是一位「個股研究員」,輸出風格像券商深度個股報告。
你會收到一檔【目標個股】(中英文名+代號+市場)與一批【已由爬蟲抓取的真實新聞】
(含標題、來源、連結、摘要;可能中英文混雜)。請產出一份結構化「個股健診」,
最後【嚴格且唯一】輸出合法 JSON。

【資料來源規範(最重要,務必遵守)】
- 以提供的真實新聞為主幹;允許動用你的【產業/財務常識】補充新聞未涵蓋的數字與背景。
- 但每一個具體數字(股價、EPS、毛利率、本益比、營收年增、法人買賣超、殖利率、目標價等)
  【都必須在該數字後標註來源】:取自提供的新聞標〔新聞〕;取自你既有知識/推估標〔AI估算〕。
- 〔AI估算〕僅供示意,可能過期或有誤;請在 data_notes 統一提醒「AI 估算數字非即時、僅供參考」。
- 單日精確籌碼(法人買賣超張數、融資餘額)若無新聞佐證,寧可只講方向(買超/賣超),不要硬掰精確值。
- 【嚴禁亂編股票代號】:ticker 不確定一律留空。你是中立資訊整理,非投資建議,不喊買賣、不保證漲跌。

【健診結構 — 對應 JSON 欄位】
1. 新聞相關性:relevance_level 高/中/低;relevance_points 2~5 點,說明哪則/哪類新聞如何關係到本檔(只依新聞)。
2. 股價與籌碼動向(price_chip 物件):
   - price_action:近期股價/盤面表現(位階、震盪、量能),數字標〔來源〕。
   - chip_flow:籌碼/法人動向(外資/投信/自營/融資的方向),數字標〔來源〕,無佐證只講方向。
   - technical:技術面位置(均線、乖離、強弱),白話即可。
3. 基本面與推升動能:
   - operating_performance:營運/接單/獲利白話判讀(好/持平/轉弱+依據),數字標〔來源〕。
   - catalysts:推升動能/題材陣列(2~5 個),元素 {title 題材, detail 一句說明}。
   - rally_nature 短期消息面/基本面可持續/資料不足判斷;rally_reason 一句理由。
4. 護城河與競爭:
   - is_leader 龍頭/前段班/中後段/資料不足;leader_reason 判斷依據。
   - competitors 2~4 個 {name, ticker(不確定留空), note 一句競爭點}。
   - moat_level 高/中/低/資料不足;moat_reason 護城河來源(製程/專利/規模/生態系/客戶綁定/特許)。
   - supply_chain:本檔所屬產業鏈上中下游代表個股(不分台股/美股都填),
     物件含 upstream/midstream/downstream,元素 {name, ticker, role 供應角色};
     美股→盡量對應台股(代號 4 碼);台股→該產業鏈台股(可含本檔);ticker 不確定留空,真找不到該段才留空陣列。
5. 估值與風險:
   - valuation 物件:level 偏高/合理/偏低/資料不足;logic 估值研判白話(本益比區間、EPS 推算、同業定錨),數字標〔來源〕;
     peer_note 同業/產業常態本益比區間的概念(可用常識)。
   - risks:2~4 點主要風險(估值、籌碼、產業循環、政策等)。
6. watch_points:後續觀察指標 2~4 項(如月營收、毛利率、外資買賣超轉向)。
7. long_term_view:綜合上述的中立結論,2~3 句,點出觀察重點與主要風險(不喊買賣、不給目標價)。
8. data_notes:一句話統一說明本報告哪些數字來自新聞、哪些為 AI 估算,且 AI 估算非即時僅供參考。

【語言】輸出一律繁體中文;evidence_news 的 title 也翻成繁中;ticker 保留原始代號。

【輸出精簡(避免被截斷)】
relevance_points≤5;catalysts≤5;competitors≤4;供應鏈每段≤5;risks≤4;watch_points≤4;
evidence_news≤8;各敘述≤90 字。

【強制輸出規範:Zero-Tolerance】
只輸出一個合法 JSON,前後不得有任何其他文字或 ```json 標記,且能被 json.loads() 解析。

【JSON 結構 — 必須完全符合】
{
  "report_date": "YYYY-MM-DD",
  "query_zh": "中文公司名",
  "query_en": "英文公司名",
  "ticker": "代號",
  "market": "台股",
  "summary": "一句話總結這檔股近期新聞焦點",
  "relevance_level": "高",
  "relevance_points": ["新聞如何直接關係到本檔", "..."],
  "price_chip": {
    "price_action": "近期股價/盤面/量能(數字標〔新聞〕或〔AI估算〕)",
    "chip_flow": "外資/投信/自營/融資方向(無佐證只講方向)",
    "technical": "均線/乖離/強弱的白話"
  },
  "operating_performance": "依新聞判讀的營運績效白話",
  "catalysts": [ { "title": "題材", "detail": "一句說明" } ],
  "rally_nature": "短期消息面",
  "rally_reason": "上漲性質的研判理由",
  "is_leader": "龍頭",
  "leader_reason": "市場地位的判斷依據",
  "competitors": [
    { "name": "競爭對手公司名", "ticker": "代號(不確定留空)", "note": "一句競爭點" }
  ],
  "moat_level": "高",
  "moat_reason": "護城河來源",
  "supply_chain": {
    "upstream": [ { "name": "公司名", "ticker": "代號", "role": "供應角色" } ],
    "midstream": [],
    "downstream": []
  },
  "valuation": {
    "level": "偏高",
    "logic": "估值研判白話(本益比/EPS 推算,數字標來源)",
    "peer_note": "同業常態本益比區間的概念"
  },
  "risks": ["主要風險點", "..."],
  "watch_points": ["後續觀察指標", "..."],
  "long_term_view": "是否適合長期持有的中立研判(含風險)",
  "data_notes": "本報告數字來源說明:〔新聞〕為新聞所載,〔AI估算〕為模型推估、非即時僅供參考",
  "evidence_news": [
    { "title": "新聞標題(翻成繁體中文)", "source": "媒體來源", "url": "連結(若有)" }
  ]
}
"""


HOUSING_SYSTEM_PROMPT = """\
你是一位兼具「房地產市場研究員」與「後端資料工程師」的純資料生成器。
你會收到【實價登錄各縣市每坪均價與歷年趨勢(政府真實統計)】+【真實台灣房市新聞】。
你的任務是:【只根據這些真實資料】判讀房市冷熱與打房政策,把新聞講到的縣市標出來,
並從【購屋者(買方)】角度做一份綜合總結,最後【嚴格且唯一】輸出合法 JSON。

【判讀重點】
1. 預售屋市場(presale_market)與成屋/中古屋市場(resale_market)目前各自偏
   「熱絡 / 持平 / 冷清」,並用一句白話說明依據(務必對應新聞,不可臆測)。
2. 打房政策(policy):整理新聞提到的政府打房/信用管制措施(如央行選擇性信用管制、
   平均地權條例、囤房稅 2.0、限貸令等),每項用白話說明對買賣方的影響。
   注意區分:純物價/油電氣價格不是房市政策,除非新聞明確連結到房貸/購屋負擔,否則不要列入。
3. 分區(regions):把新聞中明確提到的『縣市』各標一個冷熱傾向與 0~100 熱度分,
   county 欄位只能填以下名稱之一(沒提到的縣市不要硬填):
   臺北市、新北市、桃園市、臺中市、臺南市、高雄市、基隆市、新竹市、新竹縣、苗栗縣、
   彰化縣、南投縣、雲林縣、嘉義市、嘉義縣、屏東縣、宜蘭縣、花蓮縣、臺東縣、澎湖縣、金門縣、連江縣。
4. 買方綜合總結(ai_summary,物件):綜合『房價水準+歷年走勢+冷熱+政策+新聞』,
   專為想買房的人寫,各欄都用白話、可被資料佐證,不喊買賣、不預測具體漲跌幅:
   - future_trend:未來房市趨勢研判。【務必同時權衡多空兩面、不可只看短期動能】:
     * 偏多factors(資金面/短期):低利、資金充沛、營建成本、通膨保值、AI/產業帶動就業。
     * 偏空factors(結構面/長期):台灣人口已連年負成長與少子化(長期自住需求縮減)、
       餘屋/空屋與待售存量、新屋大量供給 vs 需求、家戶購屋負擔能力(房價所得比偏高)、
       升息或信用管制風險。請明確點出「短期資金動能」與「長期人口/供給結構」可能背離,
       並說明你的研判是建立在哪一邊。
   - structural_factors:條列影響長期的結構性因子(字串陣列),至少涵蓋人口/少子化、
     餘屋或供給、購屋負擔能力等;每點一句、可被常識或資料佐證,不可虛構具體數字。
   - policy_shift:房市政策的轉變方向(趨嚴打炒房?或鬆綁?對房貸成數/利率/稅負的影響)。
   - buyer_impact:政策與趨勢對『買方』整體是「偏好 | 中性 | 偏壞」三選一。
   - buyer_advice:給買方的 2~3 句具體觀察(何時/哪類產品/哪些區相對有利,務必對應資料;
     可提醒人口外流區與供給過剩區的長期保值風險)。
   - regulations:列出與購屋相關、現行或修法中的法規/措施名稱(字串陣列,如
     平均地權條例、囤房稅2.0、央行選擇性信用管制、新青安房貸等;沒有就空陣列)。
   - overview:2~3 句整體收尾(兼顧短期與長期,不可報喜不報憂)。

【真實性】
- 房價數字一律以附上的實價登錄統計為準,嚴禁自行編造價格或漲跌幅。判讀要對應資料。
- 你是中立的資訊整理,不是投資建議;不要喊買賣、不要保證漲跌。

【強制輸出規範:Zero-Tolerance】
1. 最終回覆只能有一個合法 JSON 物件,前後不得有任何其他文字或 ```json 標記。
2. 必須能被 Python json.loads() 解析。heat_score 為 0~100 整數。

【JSON 結構定義 — 必須完全符合】
{
  "report_date": "YYYY-MM-DD",
  "overall_sentiment": "熱絡|持平|冷清",
  "overall_summary": "一句話總結目前台灣房市氛圍",
  "presale_market": { "sentiment": "熱絡|持平|冷清", "note": "依據新聞的白話說明" },
  "resale_market":  { "sentiment": "熱絡|持平|冷清", "note": "依據新聞的白話說明" },
  "policy": [ { "title": "政策/措施名稱", "impact": "白話說明對市場/買賣方的影響" } ],
  "regions": [
    { "county": "縣市名稱", "sentiment": "熱絡|持平|冷清", "heat_score": 70, "note": "該區新聞重點" }
  ],
  "ai_summary": {
    "future_trend": "未來房市趨勢研判(同時權衡短期資金動能與長期人口/供給結構)...",
    "structural_factors": ["人口負成長與少子化使長期自住需求縮減", "餘屋/新增供給與待售存量", "房價所得比偏高、購屋負擔吃緊"],
    "policy_shift": "房市政策的轉變方向...",
    "buyer_impact": "偏好|中性|偏壞",
    "buyer_advice": "給買方的具體觀察...",
    "regulations": ["相關法規/措施名稱", "..."],
    "overview": "整體收尾(兼顧短期與長期)..."
  },
  "evidence_news": [ { "title": "新聞標題", "source": "媒體來源", "url": "連結(若有)" } ]
}
"""


def format_house_price_block(prices: dict | None) -> str:
    """把實價登錄各縣市每坪均價整理成給模型的參考區塊(供判讀,不可被竄改成預測)。"""
    counties = (prices or {}).get("counties") or {}
    if not counties:
        return "(本次未附實價登錄房價,請僅依新聞判讀冷熱與政策)"
    lines = [f"實價登錄季別:{prices.get('season', '—')}(單位:萬元/坪)"]
    for county, info in counties.items():
        resale = (info.get("resale") or {}).get("avg_ping_wan")
        presale = (info.get("presale") or {}).get("avg_ping_wan")
        parts = []
        if resale is not None:
            parts.append(f"成屋約 {resale}")
        if presale is not None:
            parts.append(f"預售約 {presale}")
        if parts:
            lines.append(f"  {county}:" + "、".join(parts))
    return "\n".join(lines)


def _news_date(item: dict) -> str:
    """取新聞日期 YYYY-MM-DD(published 為 ISO8601,取前 10 碼);無則空字串。"""
    p = str(item.get("published") or "").strip()
    return p[:10] if len(p) >= 10 and p[4:5] == "-" else ""


def _dedupe_news(news: list[dict]) -> list[dict]:
    """合併多來源新聞後去重(同連結或同標題只留第一筆,保留原順序)。"""
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    out: list[dict] = []
    for n in news:
        url = (str(n.get("url") or "").split("?")[0]).strip()
        title = " ".join(str(n.get("title") or "").lower().split())
        if url and url in seen_url:
            continue
        if title and title in seen_title:
            continue
        if url:
            seen_url.add(url)
        if title:
            seen_title.add(title)
        out.append(n)
    return out


def news_span(news: list[dict]) -> dict:
    """整批新聞的時間跨度與則數:{news_count, first_seen, last_seen}。"""
    dates = sorted(d for d in (_news_date(n) for n in news) if d)
    out: dict = {"news_count": len(news)}
    if dates:
        out["first_seen"] = dates[0]
        out["last_seen"] = dates[-1]
    return out


def _expand_match_keys(keys: list[str]) -> list[str]:
    """把『輝達(Nvidia)』這類中英對照名拆成可比對的子鍵(輝達、Nvidia),提高命中率。"""
    out: set[str] = set()
    for k in keys:
        k = str(k or "").strip()
        if len(k) >= 2:
            out.add(k)
        # 依括號/斜線/頓號等切出中英文各別名稱(不切空白,保留多字英文名完整)
        for part in re.split(r"[()()\[\]/、,，]+", k):
            part = part.strip()
            if len(part) >= 2:
                out.add(part)
    return [k.lower() for k in out]


def news_matches(keys: list[str], item: dict) -> bool:
    """單則新聞的標題+摘要是否提到任一關鍵字(供台媒整站 feed 過濾出相關報導)。"""
    norm = _expand_match_keys(keys)
    hay = (str(item.get("title", "")) + " " + str(item.get("summary", ""))).lower()
    return any(k in hay for k in norm)


def mention_window(keys: list[str], news: list[dict]) -> dict:
    """從真實新聞統計關鍵字命中的則數與最初/最近見報日期(供『說過幾次 + 首見/最近』)。

    以標的名稱/代號等關鍵字在標題+摘要做不分大小寫子字串比對;全部由真實新聞算出,
    不交給模型臆測。回傳 {news_count, first_seen?, last_seen?}。
    """
    norm = _expand_match_keys(keys)
    dates: list[str] = []
    count = 0
    for n in news:
        hay = (str(n.get("title", "")) + " " + str(n.get("summary", ""))).lower()
        if any(k in hay for k in norm):
            count += 1
            d = _news_date(n)
            if d:
                dates.append(d)
    out: dict = {"news_count": count}
    if dates:
        dates.sort()
        out["first_seen"] = dates[0]
        out["last_seen"] = dates[-1]
    return out


def format_news_block(news: list[dict]) -> str:
    """把抓到的新聞排版成餵給模型的文字區塊。"""
    if not news:
        return "(本次未抓到任何新聞)"
    lines = []
    for i, item in enumerate(news, 1):
        lines.append(f"[{i}] {item.get('title', '')}")
        meta = " | ".join(
            part
            for part in (
                f"來源:{item.get('source', '')}" if item.get("source") else "",
                f"時間:{item.get('published', '')}" if item.get("published") else "",
            )
            if part
        )
        if meta:
            lines.append(f"    {meta}")
        if item.get("url"):
            lines.append(f"    連結:{item['url']}")
        if item.get("summary"):
            lines.append(f"    摘要:{item['summary']}")
    return "\n".join(lines)


def build_analysis_user_prompt(news: list[dict], topic: str, today: str) -> str:
    return (
        f"今天的日期是 {today}。分析主題:『{topic}』。\n"
        f"以下是爬蟲抓到的真實新聞,請只根據這些新聞做四維度戰略分析並輸出 JSON:\n\n"
        f"{format_news_block(news)}"
    )


def build_trend_user_prompt(news: list[dict], today: str) -> str:
    return (
        f"今天的日期是 {today}。\n"
        f"請參考以下真實新聞(同時含台灣與美國市場),找出當前全球最熱門、動能最強的 "
        f"3~5 個新興產業或主題,依資金、徵才、政策、技術四種訊號綜合評估與排名打分,"
        f"並【每個產業都列出代表性的美股(us_stocks)與台股(tw_stocks)個股】,嚴格輸出 JSON。"
        f"report_date 請填 {today}。\n\n"
        f"{format_news_block(news)}"
    )


def build_stock_user_prompt(news: list[dict], today: str) -> str:
    return (
        f"今天的日期是 {today}。\n"
        f"請根據以下真實台灣財經新聞,整理出被提到的台股標的,統計提及次數並由高到低排序,"
        f"判斷各自偏利多/利空/觀望並說明原因,另歸納未來趨勢產業與夕陽產業,嚴格輸出 JSON。"
        f"report_date 請填 {today}。\n\n"
        f"{format_news_block(news)}"
    )


def build_us_stock_user_prompt(news: list[dict], today: str) -> str:
    return (
        f"今天的日期是 {today}。\n"
        f"請根據以下真實美股相關財經新聞,整理出被提到的美股標的,統計提及次數並由高到低排序,"
        f"判斷各自偏利多/利空/觀望並說明原因,另歸納未來趨勢產業與夕陽產業,嚴格輸出 JSON。"
        f"report_date 請填 {today}。\n\n"
        f"{format_news_block(news)}"
    )


def format_quotes_block(quotes_doc: dict) -> str:
    """把真實指數/期貨報價整理成餵給 Gemini 的文字(數字為唯一依據,Gemini 不得竄改)。"""
    quotes = quotes_doc.get("quotes", {})
    if not quotes:
        return "(本次未取得任何指數報價)"
    lines = []
    for sym, q in quotes.items():
        flag = " ⚠️大跌" if q.get("is_drop") else ""
        lines.append(
            f"- {q.get('name', sym)}({sym}/{q.get('lead_type', '')}):"
            f"{q.get('change_pct', 0):+.2f}%（最新 {q.get('last')}，前收 {q.get('prev')}）{flag}"
        )
    thr = quotes_doc.get("threshold", index_fetcher.DEFAULT_DROP_THRESHOLD)
    return f"【真實指數/期貨報價,大跌門檻 {thr}%】\n" + "\n".join(lines)


def build_intl_alert_user_prompt(quotes_doc: dict, news: list[dict], today: str) -> str:
    return (
        f"今天的日期是 {today}。\n"
        f"請依下列『真實報價』與『真實新聞』,研判美股/韓股是否突然大跌或有重大利空,"
        f"並推論對台股(尤其半導體/電子)的可能影響,嚴格輸出 JSON。"
        f"數字一律以報價為準、不可竄改;利空原因只能引用新聞。report_date 請填 {today}。\n\n"
        f"{format_quotes_block(quotes_doc)}\n\n"
        f"{format_news_block(news)}"
    )


def build_focus_user_prompt(term_zh: str, query_en: str, news: list[dict], today: str) -> str:
    return (
        f"今天的日期是 {today}。\n"
        f"關注對象(中文):{term_zh};英文檢索主名:{query_en}。\n"
        f"請根據以下真實英文新聞,整理這個對象近期說了什麼/做了什麼、衍伸哪些產業,"
        f"以及可能牽動哪些【台股與美股】個股(務必兩個市場都找),全部用繁體中文輸出,"
        f"嚴格輸出 JSON。report_date 請填 {today}。\n\n"
        f"{format_news_block(news)}"
    )


def build_stock_query_user_prompt(
    term_zh: str, query_en: str, ticker: str, market: str,
    news: list[dict], today: str,
) -> str:
    tag = f"{term_zh}" + (f"({ticker})" if ticker else "") + (f"／{market}" if market else "")
    return (
        f"今天的日期是 {today}。\n"
        f"目標個股:{tag};英文名:{query_en}。\n"
        f"請產出券商深度報告風格的『個股健診』,全部用繁體中文輸出,嚴格輸出 JSON,涵蓋:\n"
        f"① 新聞相關性(高/中/低 + 條列依據,只依新聞);\n"
        f"② 股價與籌碼動向(盤面/量能、外資投信自營/融資方向、技術面位置);\n"
        f"③ 基本面與推升動能(營運績效、題材 catalysts、上漲屬短期消息面或基本面可持續);\n"
        f"④ 護城河與競爭(是否龍頭、競爭對手、技術門檻、本檔所屬產業鏈上中下游代表個股,美股→對應台股);\n"
        f"⑤ 估值與風險(本益比/EPS 推算與同業區間、主要風險點)、後續觀察指標、長期持有研判。\n"
        f"可動用產業/財務常識補數字,但每個數字須標〔新聞〕或〔AI估算〕,代號不確定一律留空,不喊買賣不給目標價。\n"
        f"report_date 請填 {today}。\n\n"
        f"{format_news_block(news)}"
    )


def format_house_history_block(history: dict | None, top_n: int = 8) -> str:
    """把歷年每坪均價整理成精簡趨勢區塊(取成屋成交量較大的代表縣市,控制 token)。"""
    counties = (history or {}).get("counties") or {}
    years = (history or {}).get("years") or []
    if not counties or len(years) < 2:
        return "(本次未附歷年房價,請依當期房價與新聞研判趨勢)"
    # 以最新年成屋均價挑代表縣市(六都通常在內),避免塞滿 22 縣市 × 多年
    def latest(c):
        r = (counties[c].get("resale") or {})
        return r.get(years[-1]) or 0
    picked = sorted(counties, key=latest, reverse=True)[:top_n]
    lines = [f"歷年每坪均價(萬元/坪,年份 {years[0]}→{years[-1]};成屋):"]
    for c in picked:
        r = counties[c].get("resale") or {}
        seq = "、".join(f"{y}:{r[y]}" for y in years if y in r)
        if seq:
            lines.append(f"  {c}:{seq}")
    return "\n".join(lines)


def build_housing_user_prompt(news: list[dict], prices: dict | None, today: str,
                             history: dict | None = None) -> str:
    return (
        f"今天的日期是 {today}。\n"
        f"請綜合以下真實資料:判讀預售/成屋冷熱、整理打房政策、標出新聞提到的縣市,"
        f"並從『買方』角度做綜合總結(未來趨勢/政策轉變/對買方好壞/相關法規)。"
        f"嚴格輸出 JSON。report_date 請填 {today}。\n\n"
        f"【實價登錄當期房價】\n{format_house_price_block(prices)}\n\n"
        f"【實價登錄歷年趨勢】\n{format_house_history_block(history)}\n\n"
        f"【房市新聞】\n{format_news_block(news)}"
    )


# ---------------------------------------------------------------------------
# JSON 防護式清理與解析
# ---------------------------------------------------------------------------

def clean_json_text(text: str) -> str:
    """去除 markdown 圍欄,並擷取最外層的 JSON 值(物件或陣列)。"""
    text = text.strip()

    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()

    if text and text[0] not in "{[":
        candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if candidates:
            start = min(candidates)
            closer = "}" if text[start] == "{" else "]"
            end = text.rfind(closer)
            if end > start:
                text = text[start:end + 1]

    return text.strip()


def validate_report(data: dict) -> None:
    """戰略報告的最低限度結構驗證。"""
    missing = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in data]
    if missing:
        raise ValueError(f"JSON 缺少頂層欄位: {missing}")
    if not isinstance(data["raw_news"], list):
        raise ValueError("raw_news 必須是陣列")
    analysis = data["strategic_analysis"]
    if not isinstance(analysis, dict):
        raise ValueError("strategic_analysis 必須是物件")
    missing_analysis = [k for k in REQUIRED_ANALYSIS_KEYS if k not in analysis]
    if missing_analysis:
        raise ValueError(f"strategic_analysis 缺少欄位: {missing_analysis}")
    if not isinstance(data["laymans_dictionary"], list):
        raise ValueError("laymans_dictionary 必須是陣列")


def validate_trends(data: dict) -> None:
    """趨勢雷達的最低限度結構驗證。"""
    if "report_date" not in data:
        raise ValueError("缺少 report_date")
    if not isinstance(data.get("trends"), list) or not data["trends"]:
        raise ValueError("trends 必須是非空陣列")


def validate_stocks(data: dict) -> None:
    """台股觀察的最低限度結構驗證。"""
    if "report_date" not in data:
        raise ValueError("缺少 report_date")
    if not isinstance(data.get("stocks"), list) or not data["stocks"]:
        raise ValueError("stocks 必須是非空陣列")


def validate_us_stocks(data: dict) -> None:
    """美股觀察的最低限度結構驗證(與台股同契約)。"""
    if "report_date" not in data:
        raise ValueError("缺少 report_date")
    if not isinstance(data.get("stocks"), list) or not data["stocks"]:
        raise ValueError("stocks 必須是非空陣列")


def validate_intl_alert(data: dict) -> None:
    """國際盤預警的最低限度結構驗證(報價必須是非空字典:無真實數字就不該成立)。"""
    if "report_date" not in data:
        raise ValueError("缺少 report_date")
    if not isinstance(data.get("quotes"), dict) or not data["quotes"]:
        raise ValueError("quotes 必須是非空字典(真實報價)")
    if not isinstance(data.get("tw_impact"), dict):
        raise ValueError("tw_impact 必須是物件")


def validate_focus(data: dict) -> None:
    """全球人物追蹤的最低限度結構驗證(允許 stocks 為空:該對象未必對應到個股)。"""
    if "report_date" not in data:
        raise ValueError("缺少 report_date")
    if not isinstance(data.get("stocks"), list):
        raise ValueError("stocks 必須是陣列")


def validate_stock_query(data: dict) -> None:
    """個股健診的最低限度結構驗證。"""
    if "report_date" not in data:
        raise ValueError("缺少 report_date")
    if not isinstance(data.get("relevance_points"), list):
        raise ValueError("relevance_points 必須是陣列")


def validate_housing(data: dict) -> None:
    """房市觀察的最低限度結構驗證。"""
    if "report_date" not in data:
        raise ValueError("缺少 report_date")
    if not isinstance(data.get("regions"), list):
        raise ValueError("regions 必須是陣列")


# ---------------------------------------------------------------------------
# Gemini:共用的「讀新聞 → JSON」呼叫
# ---------------------------------------------------------------------------

def get_gemini_keys() -> list[str]:
    """蒐集一把或多把 Gemini 金鑰(支援複數 key,呼叫失敗時可自動切換)。

    支援的環境變數:
      - GEMINI_API_KEY    單一,或以逗號/分號/換行分隔多把
      - GEMINI_API_KEYS   複數(同上分隔)
      - GEMINI_API_KEY_1 / GEMINI_API_KEY_2 ...  多把分開命名
    """
    keys: list[str] = []

    def add_many(raw: str) -> None:
        for part in re.split(r"[,;\n]+", raw or ""):
            p = part.strip()
            if p and p not in keys:
                keys.append(p)

    add_many(os.environ.get("GEMINI_API_KEY", ""))
    add_many(os.environ.get("GEMINI_API_KEYS", ""))
    for name, val in os.environ.items():
        if re.fullmatch(r"GEMINI_API_KEY_?\d+", name):
            add_many(val)
    return keys


def _build_gemini_config(types, system_instruction: str, max_tokens: int | None = None):
    """組 Gemini 生成設定:關閉 thinking、放大輸出上限,避免長 prompt 思考吃光額度。

    舊版 SDK 沒有 ThinkingConfig / max_output_tokens 也能用(逐項 try,失敗就略過該設定)。
    ``max_tokens`` 可覆寫單次輸出上限(供解析失敗時加大重試)。
    """
    kwargs = {
        "system_instruction": system_instruction,
        "response_mime_type": "application/json",
        "temperature": 0.7,
    }
    try:
        if max_tokens is None:
            max_tokens = int(os.environ.get("GEMINI_MAX_TOKENS", "8192"))
        kwargs["max_output_tokens"] = int(max_tokens)
    except (TypeError, ValueError):
        pass
    # 關閉 2.5 系列的 thinking(thinking 會占用輸出 token,長 prompt 易導致空回應)
    try:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except Exception:  # noqa: BLE001 — 舊 SDK 無 ThinkingConfig
        pass
    try:
        return types.GenerateContentConfig(**kwargs)
    except TypeError:
        # 某些舊版本不支援部分欄位,退回最小可用設定
        for k in ("thinking_config", "max_output_tokens"):
            kwargs.pop(k, None)
        return types.GenerateContentConfig(**kwargs)


def _resp_finish_info(resp) -> str:
    """從回應取出 finish_reason / 安全阻擋等診斷字串,供空回應時報錯。"""
    bits: list[str] = []
    try:
        for cand in (resp.candidates or []):
            fr = getattr(cand, "finish_reason", None)
            if fr is not None:
                bits.append(f"finish_reason={fr}")
    except Exception:  # noqa: BLE001
        pass
    try:
        fb = getattr(resp, "prompt_feedback", None)
        if fb:
            bits.append(f"prompt_feedback={fb}")
    except Exception:  # noqa: BLE001
        pass
    return "; ".join(bits) or "無候選內容"


def _gemini_generate_text(types, genai, model, keys, system_instruction, user_content, max_tokens):
    """以多把金鑰逐一嘗試取得非空回應文字;全部失敗則丟出最後錯誤。"""
    config = _build_gemini_config(types, system_instruction, max_tokens)
    last_exc: Exception | None = None
    for key in keys:
        try:
            client = genai.Client(api_key=key)
            resp = client.models.generate_content(
                model=model, contents=user_content, config=config,
            )
        except Exception as exc:  # noqa: BLE001 — 金鑰/額度/網路錯誤 → 換下一把
            last_exc = exc
            continue
        text = (resp.text or "").strip()
        if not text:
            # 空回應多為 thinking 吃光 token(MAX_TOKENS)或安全阻擋;附診斷再換下一把
            last_exc = RuntimeError(f"Gemini 回傳空內容({_resp_finish_info(resp)})")
            continue
        return text
    raise last_exc or RuntimeError("所有 Gemini 金鑰皆呼叫失敗")


def call_gemini_for_json(system_instruction: str, user_content: str) -> dict:
    """以 Gemini 讀取內容並回傳解析後的 JSON dict;多把金鑰會逐一嘗試。

    若輸出疑似被 token 上限截斷(JSON 解析失敗),會自動加大輸出上限再重試一次。
    """
    from google import genai
    from google.genai import types

    model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    keys = get_gemini_keys()
    if not keys:
        raise RuntimeError("未設定 GEMINI_API_KEY")

    try:
        base_budget = int(os.environ.get("GEMINI_MAX_TOKENS", "8192"))
    except (TypeError, ValueError):
        base_budget = 8192
    # 第一輪用預設上限;若解析失敗(疑似被截斷)再用更大上限重試一次。
    budgets = [base_budget]
    if base_budget < 32768:
        budgets.append(32768)

    last_exc: Exception | None = None
    for i, budget in enumerate(budgets):
        try:
            text = _gemini_generate_text(
                types, genai, model, keys, system_instruction, user_content, budget
            )
        except Exception as exc:  # noqa: BLE001 — 整輪失敗(含空回應/MAX_TOKENS)→ 換更大上限
            last_exc = exc
            continue

        json_text = clean_json_text(text)
        try:
            # strict=False:容許字串值內含原始換行/tab 等控制字元
            # (Gemini 偶爾會在長敘述裡塞真換行,strict 模式會誤判為 Invalid control character)
            return json.loads(json_text, strict=False)
        except json.JSONDecodeError as exc:
            last_exc = exc
            # 還有更大的預算就重試(多半是輸出過長被截斷);否則才報錯
            if i + 1 < len(budgets):
                continue
            raise ValueError(
                f"JSON 解析失敗(輸出可能被截斷,可調高 GEMINI_MAX_TOKENS):{exc}\n"
                f"--- 原始內容前 500 字 ---\n{json_text[:500]}"
            ) from exc

    raise RuntimeError(f"所有 Gemini 金鑰皆呼叫失敗,最後錯誤:{last_exc}")


def normalize_dictionary(raw) -> list:
    """把模型回傳的白話文字典整理成乾淨的 [{term, explanation}] 陣列。"""
    if isinstance(raw, dict) and isinstance(raw.get("laymans_dictionary"), list):
        raw = raw["laymans_dictionary"]
    if not isinstance(raw, list):
        raise ValueError("laymans_dictionary 格式不是陣列")
    cleaned = [
        {"term": str(d.get("term", "")), "explanation": str(d.get("explanation", ""))}
        for d in raw
        if isinstance(d, dict) and d.get("term")
    ]
    if not cleaned:
        raise ValueError("laymans_dictionary 為空")
    return cleaned


def get_macro_analysis(news: list[dict], topic: str, today: str) -> dict:
    """Gemini 讀新聞 → 四維度分析 + 白話文字典。"""
    data = call_gemini_for_json(
        ANALYSIS_SYSTEM_PROMPT, build_analysis_user_prompt(news, topic, today)
    )
    analysis = data.get("strategic_analysis")
    if not isinstance(analysis, dict):
        raise ValueError("strategic_analysis 必須是物件")
    missing = [k for k in REQUIRED_ANALYSIS_KEYS if k not in analysis]
    if missing:
        raise ValueError(f"strategic_analysis 缺少欄位: {missing}")
    return {
        "strategic_analysis": analysis,
        "laymans_dictionary": normalize_dictionary(data.get("laymans_dictionary")),
    }


def get_trend_radar(news: list[dict], today: str) -> dict:
    """Gemini 讀新聞 → 趨勢雷達。"""
    data = call_gemini_for_json(
        TREND_SYSTEM_PROMPT, build_trend_user_prompt(news, today)
    )
    data.setdefault("report_date", today)
    validate_trends(data)
    # 依 heat_score 排序(若模型沒排好)
    data["trends"].sort(key=lambda t: t.get("heat_score", 0), reverse=True)
    # 每個產業補上真實新聞統計(說過幾次 + 首見/最近)與美股/台股代表股欄位
    for t in data["trends"]:
        t.setdefault("us_stocks", [])
        t.setdefault("tw_stocks", [])
        t.update(mention_window([t.get("industry", "")], news))
    return data


def get_stock_picks(news: list[dict], today: str) -> dict:
    """Gemini 讀台灣財經新聞 → 台股標的(利多/利空/觀望)+ 趨勢/夕陽產業。"""
    data = call_gemini_for_json(
        STOCK_SYSTEM_PROMPT, build_stock_user_prompt(news, today)
    )
    data.setdefault("report_date", today)
    data.setdefault("future_trends", [])
    data.setdefault("sunset_industries", [])
    validate_stocks(data)
    # 依被提及次數由高到低排序(模型沒排好時補救)
    data["stocks"].sort(key=lambda s: s.get("mention_count", 0), reverse=True)
    for s in data["stocks"]:
        s.update(mention_window([s.get("name", ""), s.get("ticker", "")], news))
    return data


def get_us_stock_picks(news: list[dict], today: str) -> dict:
    """Gemini 讀美股財經新聞 → 美股標的(利多/利空/觀望)+ 趨勢/夕陽產業。"""
    data = call_gemini_for_json(
        US_STOCK_SYSTEM_PROMPT, build_us_stock_user_prompt(news, today)
    )
    data.setdefault("report_date", today)
    data.setdefault("future_trends", [])
    data.setdefault("sunset_industries", [])
    validate_us_stocks(data)
    # 依被提及次數由高到低排序(模型沒排好時補救)
    data["stocks"].sort(key=lambda s: s.get("mention_count", 0), reverse=True)
    for s in data["stocks"]:
        s.update(mention_window([s.get("name", ""), s.get("ticker", "")], news))
    return data


def build_intl_alert(today: str, *, quotes: dict | None = None) -> dict:
    """國際盤預警:真實指數/期貨報價(算大跌)+ 美韓新聞 → Gemini 解讀利空與台股影響。

    數字一律取自 index_fetcher 的真實報價(quotes/drops),Gemini 只負責文字研判(利空原因、
    對台股影響),不得竄改數字。可傳入既抓好的 quotes(供前端兩步流程重用,免重抓)。
    """
    quotes_doc = quotes or index_fetcher.fetch_index_quotes(log=print)
    qmap = quotes_doc.get("quotes", {})
    # 真實大跌清單(由報價計算,非 AI):跌幅由深到淺排序。
    drops = sorted(
        (
            {"symbol": sym, "name": q.get("name", sym),
             "change_pct": q.get("change_pct", 0), "lead_type": q.get("lead_type", "")}
            for sym, q in qmap.items() if q.get("is_drop")
        ),
        key=lambda d: d["change_pct"],
    )

    news = fetch_intl_alert_news()
    # Gemini 只做文字研判(利空原因/對台股影響);失敗(如配額 429)不得拖垮
    # 真實報價與大跌偵測 → 包成容錯:AI 掛了仍保留報價/大跌/LINE 預警,只把原因留白。
    try:
        gemini = call_gemini_for_json(
            INTL_ALERT_SYSTEM_PROMPT, build_intl_alert_user_prompt(quotes_doc, news, today)
        )
    except Exception as exc:  # noqa: BLE001 — AI 失敗 → 降級為純報價偵測
        print(f"  警告: 國際盤 Gemini 解讀失敗,改用純報價偵測(原因留白):{exc}",
              file=sys.stderr)
        gemini = {}
    ai_ok = bool(gemini)

    tw_impact = gemini.get("tw_impact")
    if not isinstance(tw_impact, dict):
        tw_impact = {"direction": "中性", "reason": "", "sectors": []}
    # 可預測法人賣壓事件(純規則行事曆;失敗不影響國際盤主體)
    try:
        upcoming_events = chip_calendar.upcoming_chip_events()
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 法人事件行事曆計算失敗:{exc}", file=sys.stderr)
        upcoming_events = []
    summary = gemini.get("summary", "")
    if not ai_ok:
        summary = (f"⚠️ AI 解讀暫時無法取得(配額/網路),以下為真實報價偵測到的大跌 {len(drops)} 項。"
                   if drops else "⚠️ AI 解讀暫時無法取得;目前真實報價未觸及大跌門檻。")
    result = {
        "report_date": today,
        "as_of": quotes_doc.get("as_of", ""),
        "threshold": quotes_doc.get("threshold", index_fetcher.DEFAULT_DROP_THRESHOLD),
        "quotes": qmap,                       # 真實報價(唯一數字來源)
        "drops": drops,                       # 真實大跌清單(程式算)
        "alert_level": gemini.get("alert_level") or ("警戒" if drops else "平靜"),
        "summary": summary,
        "interpretation": gemini.get("interpretation", []),
        "tw_impact": tw_impact,
        "ai_ok": ai_ok,                       # AI 解讀是否成功(供前端/LINE 標註「原因待補」)
        "upcoming_events": upcoming_events,    # 可預測法人賣壓事件(行事曆)
        "raw_news": news,
    }
    validate_intl_alert(result)
    return result


def translate_focus_query(term_zh: str) -> dict:
    """把中文關注對象轉成英文新聞檢索詞(Gemini)。"""
    data = call_gemini_for_json(
        FOCUS_TRANSLATE_SYSTEM_PROMPT,
        f"請把這個中文關鍵字轉成英文新聞檢索詞:「{term_zh}」",
    )
    data.setdefault("query_zh", term_zh)
    data.setdefault("aliases", [])
    data.setdefault("zh_aliases", [])
    if not data.get("query_en"):
        data["query_en"] = term_zh
    return data


def translate_stock_query(term: str) -> dict:
    """把使用者輸入的個股(中文/英文/代號)正規化成中英名+代號+市場(Gemini)。"""
    data = call_gemini_for_json(
        STOCK_QUERY_TRANSLATE_SYSTEM_PROMPT,
        f"請把這檔股票正規化成中英文名稱、代號與市場:「{term}」",
    )
    data.setdefault("query_zh", term)
    data.setdefault("ticker", "")
    data.setdefault("aliases", [])
    data.setdefault("zh_aliases", [])
    if data.get("market") not in ("台股", "美股"):
        data["market"] = "美股"
    if not data.get("query_en"):
        data["query_en"] = term
    return data


def get_stock_query_analysis(
    term_zh: str, query_en: str, ticker: str, market: str,
    news: list[dict], today: str,
) -> dict:
    """Gemini 讀該股新聞 → ①與新聞的直接相關性 ②營運績效 + 上漲是消息面還是基本面。"""
    data = call_gemini_for_json(
        STOCK_QUERY_SYSTEM_PROMPT,
        build_stock_query_user_prompt(term_zh, query_en, ticker, market, news, today),
    )
    data.setdefault("report_date", today)
    data.setdefault("query_zh", term_zh)
    data.setdefault("query_en", query_en)
    data.setdefault("ticker", ticker)
    data.setdefault("market", market)
    data.setdefault("relevance_points", [])
    data.setdefault("price_chip", {})
    data.setdefault("catalysts", [])
    data.setdefault("competitors", [])
    data.setdefault("supply_chain", {})
    data.setdefault("valuation", {})
    data.setdefault("risks", [])
    data.setdefault("watch_points", [])
    data.setdefault("data_notes", "")
    validate_stock_query(data)
    # 整體新聞跨度 + 本檔在這批新聞的真實命中統計(首見/最近/則數)
    data.update(news_span(news))
    data.update(mention_window(_uniq_queries([term_zh, query_en, ticker]), news))
    return data


def get_focus_analysis(term_zh: str, query_en: str, news: list[dict], today: str) -> dict:
    """Gemini 讀全球新聞 → 該對象說了什麼 + 衍伸產業 + 牽動的台股/美股個股。"""
    data = call_gemini_for_json(
        FOCUS_SYSTEM_PROMPT, build_focus_user_prompt(term_zh, query_en, news, today)
    )
    data.setdefault("report_date", today)
    data.setdefault("query_zh", term_zh)
    data.setdefault("query_en", query_en)
    data.setdefault("key_statements", [])
    data.setdefault("affected_industries", [])
    data.setdefault("stocks", [])
    validate_focus(data)
    # 整體新聞跨度(這批新聞都與該對象相關)+ 每檔個股的真實命中統計
    data.update(news_span(news))
    for s in data["stocks"]:
        s.update(mention_window([s.get("name", ""), s.get("ticker", "")], news))
    return data


def get_housing_analysis(news: list[dict], prices: dict | None, today: str,
                        history: dict | None = None) -> dict:
    """Gemini 讀房市新聞 + 實價登錄當期/歷年 → 冷熱 + 打房政策 + 縣市標記 + 買方總結。"""
    data = call_gemini_for_json(
        HOUSING_SYSTEM_PROMPT, build_housing_user_prompt(news, prices, today, history)
    )
    data.setdefault("report_date", today)
    data.setdefault("regions", [])
    data.setdefault("policy", [])
    # AI 買方總結:模型沒給時退回單句 overall_summary(包成 overview 維持結構一致)
    if not data.get("ai_summary"):
        data["ai_summary"] = {"overview": data.get("overall_summary", "")}
    validate_housing(data)
    # 只保留合法縣市名稱的分區標記,避免模型亂填
    data["regions"] = [
        r for r in data["regions"]
        if isinstance(r, dict) and r.get("county") in TAIWAN_COUNTIES
    ]
    return data


# ---------------------------------------------------------------------------
# 新聞抓取設定
# ---------------------------------------------------------------------------

def parse_queries(env_name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_name, "")
    queries = [q.strip() for q in raw.split(";") if q.strip()]
    return queries or default


def parse_topics(env_name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_name, "").replace(",", ";")
    topics = [t.strip().upper() for t in raw.split(";") if t.strip()]
    return topics or default


def section_feeds(topics: list[str], lang: str, region: str) -> dict[str, str]:
    """把 Google News 分類頭條組成 {名稱: RSS網址}。"""
    return {
        _SECTION_LABELS.get(t, f"Google {t}"): news_fetcher.google_news_topic_url(
            t, lang, region
        )
        for t in topics
    }


def fetch_bilingual_news(
    *,
    zh_queries: list[str],
    en_queries: list[str],
    zh_feeds: dict[str, str] | None = None,
    en_feeds: dict[str, str] | None = None,
    limit: int,
    since_hours: int,
) -> list[dict]:
    """同時抓中文(zh/TW)與英文(en/US)新聞並合併去重。

    讓每個章節不論報導是中文或英文都不漏(中文台媒 + 國際英文原文一起進來);
    呈現語言仍由各章節的 Gemini prompt 決定(多半輸出繁體中文)。
    """
    zh_news = news_fetcher.fetch_news(
        zh_queries, lang="zh", region="TW", feeds=zh_feeds,
        limit=limit, since_hours=since_hours,
    ) if zh_queries else []
    en_news = news_fetcher.fetch_news(
        en_queries, lang="en", region="US", feeds=en_feeds,
        limit=limit, since_hours=since_hours,
    ) if en_queries else []
    return _dedupe_news(zh_news + en_news)


def fetch_macro_news(topic: str, extra_query: str | None = None) -> list[dict]:
    """戰略報告新聞:中文(台媒)+ 英文(國際原文)雙語都抓並合併去重。"""
    zh_queries = parse_queries("NEWS_QUERIES", DEFAULT_NEWS_QUERIES)
    en_queries = parse_queries("NEWS_QUERIES_EN", DEFAULT_NEWS_QUERIES_EN)
    # 多主題時把該主題當額外關鍵字(短主題才適合當查詢);中英文兩邊都加。
    if extra_query and 0 < len(extra_query) <= 30:
        if extra_query not in zh_queries:
            zh_queries = [extra_query] + zh_queries
        if extra_query not in en_queries:
            en_queries = [extra_query] + en_queries
    topics = parse_topics("NEWS_TOPICS", DEFAULT_NEWS_TOPICS)
    # 中文側:動態分類頭條 + 財經官方 feed;英文側:美國同分類頭條。
    zh_feeds = {**news_fetcher.CREDIBLE_FEEDS, **section_feeds(topics, "zh", "TW")}
    en_feeds = section_feeds(topics, "en", "US")
    return fetch_bilingual_news(
        zh_queries=zh_queries,
        en_queries=en_queries,
        zh_feeds=zh_feeds,
        en_feeds=en_feeds,
        limit=int(os.environ.get("NEWS_MAX", "25")),
        since_hours=int(os.environ.get("NEWS_SINCE_HOURS", str(SIX_MONTHS_HOURS))),
    )


def fetch_trend_news() -> list[dict]:
    """抓產業趨勢新聞:台灣(中文)+ 美國(英文)兩邊都抓,讓趨勢雷達含到美股。"""
    lang = os.environ.get("NEWS_LANG", "zh")
    region = os.environ.get("NEWS_REGION", "TW")
    queries = parse_queries("TREND_QUERIES", DEFAULT_TREND_QUERIES)
    topics = parse_topics("TREND_TOPICS", DEFAULT_TREND_TOPICS)
    feeds = section_feeds(topics, lang, region)
    limit = int(os.environ.get("NEWS_MAX", "25"))
    since = int(os.environ.get("NEWS_SINCE_HOURS", str(SIX_MONTHS_HOURS)))
    tw_news = news_fetcher.fetch_news(
        queries, lang=lang, region=region, feeds=feeds, limit=limit, since_hours=since,
    )
    # 美股/全球面向:英文趨勢關鍵字 + 美國財經/科技頭條
    us_queries = parse_queries("US_TREND_QUERIES", DEFAULT_US_TREND_QUERIES)
    us_feeds = section_feeds(["BUSINESS", "TECHNOLOGY"], "en", "US")
    us_news = news_fetcher.fetch_news(
        us_queries, lang="en", region="US", feeds=us_feeds, limit=limit, since_hours=since,
    )
    # 合併並依連結/標題去重(保留台灣在前、美國補上)
    return _dedupe_news(tw_news + us_news)


def fetch_stock_news() -> list[dict]:
    """抓台股新聞:中文(台媒)+ 英文(國際對台股/台積電的報導)雙語都抓並去重。"""
    zh_queries = parse_queries("STOCK_QUERIES", DEFAULT_STOCK_QUERIES)
    en_queries = parse_queries("STOCK_QUERIES_EN", DEFAULT_STOCK_QUERIES_EN)
    # 中文側:財經分類頭條 + 中央社財經 feed;英文側:美國財經頭條。
    zh_feeds = {"中央社 財經": news_fetcher.CREDIBLE_FEEDS.get("中央社 財經", "")}
    zh_feeds = {k: v for k, v in zh_feeds.items() if v}
    zh_feeds.update(section_feeds(["BUSINESS"], "zh", "TW"))
    en_feeds = section_feeds(["BUSINESS"], "en", "US")
    return fetch_bilingual_news(
        zh_queries=zh_queries,
        en_queries=en_queries,
        zh_feeds=zh_feeds,
        en_feeds=en_feeds,
        limit=int(os.environ.get("STOCK_MAX", "60")),
        since_hours=int(os.environ.get("STOCK_SINCE_HOURS", str(SIX_MONTHS_HOURS))),
    )


def fetch_us_stock_news() -> list[dict]:
    """抓美股新聞:英文原文(覆蓋廣、即時)+ 中文(台媒對美股的報導)雙語都抓並去重。

    呈現給使用者的分析一律由 Gemini 翻成繁體中文(見 US_STOCK_SYSTEM_PROMPT)。
    """
    en_queries = parse_queries("US_STOCK_QUERIES", DEFAULT_US_STOCK_QUERIES)
    zh_queries = parse_queries("US_STOCK_QUERIES_ZH", DEFAULT_US_STOCK_QUERIES_ZH)
    en_feeds = section_feeds(["BUSINESS"], "en", "US")
    zh_feeds = {"中央社 財經": news_fetcher.CREDIBLE_FEEDS.get("中央社 財經", "")}
    zh_feeds = {k: v for k, v in zh_feeds.items() if v}
    return fetch_bilingual_news(
        zh_queries=zh_queries,
        en_queries=en_queries,
        zh_feeds=zh_feeds,
        en_feeds=en_feeds,
        limit=int(os.environ.get("US_STOCK_MAX", "40")),
        since_hours=int(os.environ.get("US_STOCK_SINCE_HOURS", str(SIX_MONTHS_HOURS))),
    )


DEFAULT_INTL_ALERT_QUERIES = [
    "US stock market selloff plunge today",
    "Nasdaq S&P 500 drop futures",
    "KOSPI Korea stocks Samsung SK Hynix",
    "semiconductor chip stocks selloff",
]


def fetch_intl_alert_news() -> list[dict]:
    """抓國際盤預警用新聞:美股財經頭條 + 美/韓大跌相關英文關鍵字,輔以台媒中文角度。"""
    en_queries = parse_queries("INTL_ALERT_QUERIES", DEFAULT_INTL_ALERT_QUERIES)
    en_feeds = section_feeds(["BUSINESS"], "en", "US")
    zh_feeds = {"中央社 財經": news_fetcher.CREDIBLE_FEEDS.get("中央社 財經", "")}
    zh_feeds = {k: v for k, v in zh_feeds.items() if v}
    return fetch_bilingual_news(
        zh_queries=parse_queries("INTL_ALERT_QUERIES_ZH", ["美股 大跌 重挫", "韓股 KOSPI 三星"]),
        en_queries=en_queries,
        zh_feeds=zh_feeds,
        en_feeds=en_feeds,
        limit=int(os.environ.get("INTL_ALERT_MAX", "40")),
        since_hours=int(os.environ.get("INTL_ALERT_SINCE_HOURS", "72")),
    )


def _uniq_queries(items: list[str]) -> list[str]:
    """關鍵字去重(保序、不分大小寫)。"""
    seen: set[str] = set()
    out: list[str] = []
    for q in items:
        q = (q or "").strip()
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            out.append(q)
    return out


def fetch_focus_news(
    query_en: str,
    aliases: list[str] | None = None,
    query_zh: str | None = None,
    zh_aliases: list[str] | None = None,
) -> list[dict]:
    """抓關注對象的全球新聞,三路合併去重:
    1) 英文名/別名 在 en/US 關鍵字檢索;
    2) 中文原名 + 中文別名(如黃仁勳→輝達)在 zh/TW 關鍵字檢索;
    3) 台媒整站 RSS(TW_MEDIA_FEEDS)直接來源,但「只保留有提到該對象者」,
       補足 Google News 排名外、卻確實報導該人物的台灣文章(如自由時報)。
    """
    en_queries = _uniq_queries([query_en] + list(aliases or []))
    zh_terms = _uniq_queries(([query_zh] if query_zh else []) + list(zh_aliases or []))
    if not en_queries and not zh_terms:
        return []

    since_hours = int(os.environ.get("FOCUS_SINCE_HOURS", str(SIX_MONTHS_HOURS)))
    # 1) + 2) 關鍵字雙語檢索
    keyword_news = fetch_bilingual_news(
        zh_queries=zh_terms,
        en_queries=en_queries,
        zh_feeds=None,
        en_feeds=None,
        limit=int(os.environ.get("FOCUS_MAX", "50")),
        since_hours=since_hours,
    )
    # 3) 台媒整站 RSS → 過濾出有提到該對象者(用中文名稱/別名比對)
    site_news = news_fetcher.fetch_news(
        [], lang="zh", region="TW", feeds=TW_MEDIA_FEEDS,
        limit=int(os.environ.get("FOCUS_SITE_MAX", "200")), since_hours=since_hours,
    )
    site_hits = [n for n in site_news if zh_terms and news_matches(zh_terms, n)]
    return _dedupe_news(keyword_news + site_hits)


def fetch_stock_query_news(
    query_en: str,
    ticker: str = "",
    aliases: list[str] | None = None,
    query_zh: str | None = None,
    zh_aliases: list[str] | None = None,
) -> list[dict]:
    """個股健診抓該股新聞:沿用 fetch_focus_news 三路雙語邏輯,並把股票代號也納入檢索。"""
    en = ([ticker] if ticker else []) + list(aliases or [])
    zh = ([ticker] if ticker else []) + list(zh_aliases or [])
    return fetch_focus_news(query_en, en, query_zh, zh)


def fetch_housing_news() -> list[dict]:
    """抓房市新聞(預售/成屋冷熱、打房政策);委派 housing_fetcher。"""
    return housing_fetcher.fetch_housing_news(
        limit=int(os.environ.get("HOUSING_MAX", "18")),
        since_hours=int(os.environ.get("HOUSING_SINCE_HOURS", "72")),
    )


# ---------------------------------------------------------------------------
# LINE 推播 (Messaging API push)
# ---------------------------------------------------------------------------

def build_line_message(report: dict) -> str:
    """把報告整理成一則精簡的 LINE 文字訊息(僅保留標題與盲點/領先指標)。"""
    lines = [
        f"🌐 全球政經戰略報告 {report.get('report_date', '')}",
        f"主題:{report.get('topic', '')}",
    ]

    kpi = report.get("strategic_analysis", {}).get("blind_spots_and_kpi", "").strip()
    if kpi:
        lines += ["", "🎯 盲點與領先指標:", kpi[:400] + ("..." if len(kpi) > 400 else "")]

    lines += ["", f"(白話文來源:{report.get('dictionary_source', '—')})"]

    msg = "\n".join(lines)
    if len(msg) > LINE_TEXT_LIMIT:
        msg = msg[:LINE_TEXT_LIMIT] + "\n...(訊息過長已截斷)"
    return msg


def notify_line(report: dict) -> None:
    """透過 LINE Messaging API push 推送報告摘要。"""
    _push_line_text(build_line_message(report))


def _push_line_text(text: str) -> None:
    """以 LINE Messaging API push 推送一則文字(共用:戰略報告 / 國際盤預警)。"""
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    to = os.environ["LINE_TO"]

    payload = json.dumps(
        {"to": to, "messages": [{"type": "text", "text": text}]}
    ).encode("utf-8")

    req = urllib.request.Request(
        LINE_PUSH_ENDPOINT,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"LINE 回應非 200: {resp.status}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"LINE 推播失敗 ({exc.code}): {body}") from exc


# 對台股有「時間差領先」意義的市場(美股指數=隔夜、美股期貨=盤前);KOSPI 同步盤不算。
LEAD_DROP_TYPES = ("隔夜領先", "盤前即時")


def lead_market_drops(intl: dict) -> list[dict]:
    """取『時間差領先』市場(美股指數/期貨)的大跌清單,KOSPI 同步盤不構成盤前預警。"""
    return [d for d in intl.get("drops", []) if d.get("lead_type") in LEAD_DROP_TYPES]


def build_intl_alert_line_message(intl: dict) -> str:
    """把國際盤大跌預警整理成一則精簡 LINE 文字(真實報價數字 + Gemini 利空研判)。"""
    lines = [
        f"🚨 國際盤大跌預警 {intl.get('report_date', '')}",
        f"警示級別:{intl.get('alert_level', '—')}",
    ]
    if intl.get("summary"):
        lines.append(intl["summary"])

    lead = lead_market_drops(intl)
    if lead:
        lines += ["", "📉 大跌(時間差領先台股):"]
        for d in lead:
            lines.append(
                f"・{d.get('name', '')} {d.get('change_pct', 0):+.2f}%({d.get('lead_type', '')})"
            )
    others = [d for d in intl.get("drops", []) if d.get("lead_type") not in LEAD_DROP_TYPES]
    if others:
        lines.append(
            "・(同步盤)"
            + "、".join(f"{d.get('name', '')} {d.get('change_pct', 0):+.2f}%" for d in others)
        )

    interp = intl.get("interpretation", [])
    if interp:
        lines += ["", "🧭 利空原因(依新聞):"]
        for it in interp[:3]:
            mk = it.get("market", "")
            cause = (it.get("cause", "") or "").strip()
            lines.append(f"・{mk}:{cause}" if mk else f"・{cause}")

    imp = intl.get("tw_impact", {})
    if imp:
        lines += ["", f"🇹🇼 對台股:{imp.get('direction', '—')}"]
        reason = (imp.get("reason", "") or "").strip()
        if reason:
            lines.append(reason[:200] + ("..." if len(reason) > 200 else ""))
        sectors = imp.get("sectors", [])
        if sectors:
            lines.append("重點族群:" + "、".join(str(s) for s in sectors))

    lines += ["", "⚠️ 真實報價 + AI 研判,僅供參考,非投資建議"]
    msg = "\n".join(lines)
    if len(msg) > LINE_TEXT_LIMIT:
        msg = msg[:LINE_TEXT_LIMIT] + "\n...(訊息過長已截斷)"
    return msg


def notify_line_intl_alert(intl: dict) -> None:
    """國際盤大跌 + 利空 → 推一則 LINE 預警(沿用 Messaging API push)。"""
    _push_line_text(build_intl_alert_line_message(intl))


def build_chip_events_line_message(events: list[dict], today: str) -> str:
    """把『進入窗口的可預測法人賣壓事件』整理成一則精簡 LINE 文字。"""
    lines = [f"📅 法人事件預告 {today}", "未來數日已知的籌碼/賣壓窗口:"]
    for e in events:
        td = e.get("trading_days_until", 0)
        when = "今日" if td == 0 else f"約 {td} 個交易日後"
        lines.append(f"・{e.get('title', '')}({e.get('date', '')},{when})")
        if e.get("detail"):
            lines.append(f"　{e['detail']}")
    lines += ["", "⚠️ 日期為慣例/曆法推算,實際以官方公告為準;僅供參考,非投資建議"]
    msg = "\n".join(lines)
    if len(msg) > LINE_TEXT_LIMIT:
        msg = msg[:LINE_TEXT_LIMIT] + "\n...(訊息過長已截斷)"
    return msg


def notify_line_chip_events(events: list[dict], today: str) -> None:
    """可預測法人事件進入窗口 → 推一則 LINE 預告(沿用 Messaging API push)。"""
    _push_line_text(build_chip_events_line_message(events, today))


def load_pushed_events() -> list[str]:
    """讀已推播過的法人事件 id 清單(防 LINE 洗版);無檔回空。"""
    try:
        return list(json.loads(CHIP_PUSHED_STATE.read_text(encoding="utf-8")).get("ids", []))
    except Exception:  # noqa: BLE001 — 無檔/壞檔 → 視為尚未推過
        return []


def save_pushed_events(ids: list[str]) -> None:
    """寫回已推播事件 id 清單(只保留最近 60 筆,避免無限增長)。"""
    save_json(CHIP_PUSHED_STATE, {"ids": ids[-60:]})


# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------

def save_json(path: Path, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return payload


def trend_radar_enabled() -> bool:
    return os.environ.get("ENABLE_TREND_RADAR", "1").lower() not in ("0", "false", "no")


def stock_picker_enabled() -> bool:
    return os.environ.get("ENABLE_STOCK_PICKER", "1").lower() not in ("0", "false", "no")


def us_stock_picker_enabled() -> bool:
    return os.environ.get("ENABLE_US_STOCK_PICKER", "1").lower() not in ("0", "false", "no")


def intl_alert_enabled() -> bool:
    return os.environ.get("ENABLE_INTL_ALERT", "1").lower() not in ("0", "false", "no")


def intl_alert_line_enabled() -> bool:
    return os.environ.get("ENABLE_INTL_ALERT_LINE", "1").lower() not in ("0", "false", "no")


def chip_enabled() -> bool:
    return os.environ.get("ENABLE_CHIP", "1").lower() not in ("0", "false", "no")


def chip_line_enabled() -> bool:
    return os.environ.get("ENABLE_CHIP_LINE", "1").lower() not in ("0", "false", "no")


def focus_enabled() -> bool:
    return os.environ.get("ENABLE_FOCUS", "1").lower() not in ("0", "false", "no")


def build_focus_report(today: str) -> dict:
    """每日排程版全球人物追蹤:對 FOCUS_TOPICS 每個對象翻英→抓全球新聞→分析。

    回傳 {report_date, focuses:[<分析>, ...]};單一對象失敗只略過,不影響其他對象。
    """
    topics = parse_queries("FOCUS_TOPICS", DEFAULT_FOCUS_TOPICS)
    focuses: list[dict] = []
    for term in topics:
        try:
            tr = translate_focus_query(term)
            news = fetch_focus_news(
                tr.get("query_en", ""), tr.get("aliases"),
                tr.get("query_zh", term), tr.get("zh_aliases"),
            )
            analysis = get_focus_analysis(term, tr.get("query_en", ""), news, today)
            analysis["raw_news"] = news
            focuses.append(analysis)
            print(f"  ▸ 追蹤對象「{term}」({tr.get('query_en', '')}):{len(news)} 則新聞。")
        except Exception as exc:  # noqa: BLE001 — 單一對象失敗不影響其他對象
            print(f"  警告: 追蹤對象「{term}」產生失敗:{exc}", file=sys.stderr)
    if not focuses:
        raise RuntimeError("所有追蹤對象都失敗")
    return {"report_date": today, "focuses": focuses}


def housing_enabled() -> bool:
    return os.environ.get("ENABLE_HOUSING", "1").lower() not in ("0", "false", "no")


def parse_report_topics() -> list[str]:
    """戰略報告主題清單。

    優先讀 REPORT_TOPICS(以 ; 分隔多主題);否則退回單一 REPORT_TOPIC / 預設。
    回傳至少一個主題;第一個為主報告(寫入 latest_report.json,維持向後相容)。
    """
    raw = os.environ.get("REPORT_TOPICS", "")
    topics = [t.strip() for t in raw.split(";") if t.strip()]
    if topics:
        return topics
    return [os.environ.get("REPORT_TOPIC") or DEFAULT_TOPIC]


def build_macro_report(topic: str, today: str, *, extra_query: str | None = None) -> dict:
    """抓該主題新聞 → Gemini 四維度分析 + 白話文,組出一份戰略報告 dict 並驗證。"""
    news = fetch_macro_news(topic, extra_query=extra_query)
    print(f"  抓到 {len(news)} 則新聞。")
    if not news:
        print("  警告: 未抓到任何新聞,分析將缺乏真實素材。", file=sys.stderr)
    analysis = get_macro_analysis(news, topic, today)
    report = {
        "report_date": today,
        "topic": topic,
        "raw_news": news,
        "news_span": news_span(news),
        "strategic_analysis": analysis["strategic_analysis"],
        "laymans_dictionary": analysis["laymans_dictionary"],
        "dictionary_source": "gemini",
    }
    validate_report(report)
    return report


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    if not get_gemini_keys():
        print("錯誤: 未設定 GEMINI_API_KEY 環境變數", file=sys.stderr)
        return 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        # A. 戰略報告(支援多主題:第一個為主報告,維持 latest_report.json 向後相容)
        topics = parse_report_topics()
        multi = len(topics) > 1
        print(f"[1/8] 爬取真實外電並請 Gemini 分析(主題數:{len(topics)})...")

        # 主報告必成功(失敗→整體非零碼);其餘主題失敗只警告不中斷。
        print(f"  ▸ 主主題:{topics[0]}")
        report = build_macro_report(topics[0], today, extra_query=topics[0] if multi else None)
        reports = [report]
        for extra_topic in topics[1:]:
            print(f"  ▸ 次主題:{extra_topic}")
            try:
                reports.append(build_macro_report(extra_topic, today, extra_query=extra_topic))
            except Exception as exc:  # noqa: BLE001 — 次主題失敗不影響主報告
                print(f"  警告: 主題「{extra_topic}」產生失敗:{exc}", file=sys.stderr)

        print("[2/8] 戰略分析完成,寫入報告檔...")
        save_json(OUTPUT_LATEST, report)
        save_json(ARCHIVE_DIR / f"{today}.json", report)
        if multi:
            multi_doc = {"report_date": today, "reports": reports}
            save_json(OUTPUT_REPORTS_MULTI, multi_doc)
            save_json(REPORTS_MULTI_ARCHIVE_DIR / f"{today}.json", multi_doc)
            print(f"  多主題報告:{len(reports)}/{len(topics)} 份成功。")

        # B. 趨勢雷達
        trends = None
        if trend_radar_enabled():
            print("[3/8] 爬取產業新聞並向 Gemini 請求趨勢雷達...")
            try:
                trend_news = fetch_trend_news()
                print(f"  抓到 {len(trend_news)} 則產業新聞。")
                trends = get_trend_radar(trend_news, today)
                save_json(OUTPUT_TRENDS, trends)
                save_json(TRENDS_ARCHIVE_DIR / f"{today}.json", trends)
                top = "、".join(t.get("industry", "") for t in trends["trends"][:3])
                print(f"  趨勢雷達完成,Top3:{top}")
            except Exception as exc:  # noqa: BLE001 — 趨勢雷達失敗不影響戰略報告
                print(f"  警告: 趨勢雷達產生失敗:{exc}", file=sys.stderr)
        else:
            print("[3/8] ENABLE_TREND_RADAR=0,略過趨勢雷達。")

        # C. 台股觀察
        if stock_picker_enabled():
            print("[4/8] 爬取台灣財經新聞並向 Gemini 整理台股標的...")
            try:
                stock_news = fetch_stock_news()
                print(f"  抓到 {len(stock_news)} 則台灣財經新聞。")
                stocks = get_stock_picks(stock_news, today)
                save_json(OUTPUT_STOCKS, stocks)
                save_json(STOCKS_ARCHIVE_DIR / f"{today}.json", stocks)
                top = "、".join(s.get("name", "") for s in stocks["stocks"][:5])
                print(f"  台股觀察完成,最常被提到:{top}")
            except Exception as exc:  # noqa: BLE001 — 台股觀察失敗不影響戰略報告
                print(f"  警告: 台股觀察產生失敗:{exc}", file=sys.stderr)
        else:
            print("[4/8] ENABLE_STOCK_PICKER=0,略過台股觀察。")

        # D. 美股觀察
        if us_stock_picker_enabled():
            print("[5/8] 爬取美股財經新聞並向 Gemini 整理美股標的...")
            try:
                us_stock_news = fetch_us_stock_news()
                print(f"  抓到 {len(us_stock_news)} 則美股財經新聞。")
                us_stocks = get_us_stock_picks(us_stock_news, today)
                save_json(OUTPUT_US_STOCKS, us_stocks)
                save_json(US_STOCKS_ARCHIVE_DIR / f"{today}.json", us_stocks)
                top = "、".join(s.get("name", "") for s in us_stocks["stocks"][:5])
                print(f"  美股觀察完成,最常被提到:{top}")
            except Exception as exc:  # noqa: BLE001 — 美股觀察失敗不影響戰略報告
                print(f"  警告: 美股觀察產生失敗:{exc}", file=sys.stderr)
        else:
            print("[5/8] ENABLE_US_STOCK_PICKER=0,略過美股觀察。")

        # D2. 國際盤預警(美股指數/KOSPI/期貨真實漲跌幅 → 偵測大跌 → Gemini 解讀台股影響)
        if intl_alert_enabled():
            print("[6/8] 抓國際盤報價(美股/KOSPI/期貨)偵測大跌,向 Gemini 解讀台股影響...")
            try:
                intl = build_intl_alert(today)
                save_json(OUTPUT_INTL_ALERT, intl)
                save_json(INTL_ALERT_ARCHIVE_DIR / f"{today}.json", intl)
                drops = intl.get("drops", [])
                tag = "、".join(f"{d['name']}{d['change_pct']:+.1f}%" for d in drops[:3]) or "無"
                print(f"  國際盤預警完成,警示級別:{intl.get('alert_level', '—')}(大跌:{tag})")
                # 時間差領先市場(美股/期貨)出現大跌 → 主動 LINE 推播盤前預警
                lead = lead_market_drops(intl)
                if (lead and intl_alert_line_enabled()
                        and os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
                        and os.environ.get("LINE_TO")):
                    try:
                        notify_line_intl_alert(intl)
                        print(f"  ⚠️ 美股/期貨大跌,已推播 LINE 預警({len(lead)} 項)。")
                    except Exception as exc:  # noqa: BLE001 — 推播失敗不影響存檔
                        print(f"  警告: 國際盤 LINE 預警推播失敗:{exc}", file=sys.stderr)
                # 可預測法人事件首次進入 3 個交易日窗口 → 推一次 LINE 預告(防洗版)
                if (chip_line_enabled()
                        and os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
                        and os.environ.get("LINE_TO")):
                    try:
                        pushed = load_pushed_events()
                        due = chip_calendar.pick_new_pushable(
                            intl.get("upcoming_events", []), pushed)
                        if due:
                            notify_line_chip_events(due, today)
                            save_pushed_events(pushed + [e["id"] for e in due])
                            print(f"  📅 已推播 LINE 法人事件預告({len(due)} 項)。")
                    except Exception as exc:  # noqa: BLE001 — 推播失敗不影響存檔
                        print(f"  警告: 法人事件 LINE 預告推播失敗:{exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 — 國際盤預警失敗不影響戰略報告
                print(f"  警告: 國際盤預警產生失敗:{exc}", file=sys.stderr)
        else:
            print("[6/8] ENABLE_INTL_ALERT=0,略過國際盤預警。")

        # D3. 法人籌碼事後驗證(抓證交所近 N 日三大法人買賣超,真實數字)
        if chip_enabled():
            print("[6/8] 抓證交所三大法人買賣超(近 N 日,事後驗證真實籌碼)...")
            try:
                chip = chip_fetcher.fetch_chip_flow(
                    days=int(os.environ.get("CHIP_DAYS") or "10"), log=print)
                save_json(OUTPUT_CHIP, chip)
                if chip.get("days"):
                    save_json(CHIP_ARCHIVE_DIR / f"{chip['days'][0]['date']}.json", chip)
                    latest = chip["days"][0]
                    print(f"  法人籌碼完成,最新 {latest['date']}:"
                          f"外資 {latest['foreign']/1e8:+.0f}億、投信 {latest['trust']/1e8:+.0f}億。")
            except Exception as exc:  # noqa: BLE001 — 法人籌碼失敗不影響戰略報告
                print(f"  警告: 法人籌碼抓取失敗:{exc}", file=sys.stderr)
        else:
            print("[6/8] ENABLE_CHIP=0,略過法人籌碼。")

        # E. 全球人物追蹤(對 FOCUS_TOPICS 每個對象翻英→抓全球新聞→台美股關聯)
        if focus_enabled():
            print("[7/8] 翻譯追蹤對象並抓全球新聞,向 Gemini 整理台美股關聯...")
            try:
                focus_doc = build_focus_report(today)
                save_json(OUTPUT_FOCUS, focus_doc)
                save_json(FOCUS_ARCHIVE_DIR / f"{today}.json", focus_doc)
                names = "、".join(f.get("query_zh", "") for f in focus_doc["focuses"])
                print(f"  全球人物追蹤完成,對象:{names}")
            except Exception as exc:  # noqa: BLE001 — 人物追蹤失敗不影響戰略報告
                print(f"  警告: 全球人物追蹤產生失敗:{exc}", file=sys.stderr)
        else:
            print("[7/8] ENABLE_FOCUS=0,略過全球人物追蹤。")

        # F. 房市觀察(房價走代理,排程無代理時就只用新聞 + repo 既有房價當參考)
        housing = None
        if housing_enabled():
            print("[8/8] 爬取房市新聞並向 Gemini 判讀冷熱 + 打房政策...")
            try:
                housing_news = fetch_housing_news()
                print(f"  抓到 {len(housing_news)} 則房市新聞。")
                prices = housing_fetcher.load_house_prices()
                history = housing_fetcher.load_house_price_history()
                housing = get_housing_analysis(housing_news, prices, today, history)
                housing["raw_news"] = housing_news
                save_json(OUTPUT_HOUSING, housing)
                save_json(HOUSING_ARCHIVE_DIR / f"{today}.json", housing)
                print(f"  房市觀察完成,整體氛圍:{housing.get('overall_sentiment', '—')}")
            except Exception as exc:  # noqa: BLE001 — 房市觀察失敗不影響戰略報告
                print(f"  警告: 房市觀察產生失敗:{exc}", file=sys.stderr)
        else:
            print("[8/8] ENABLE_HOUSING=0,略過房市觀察。")

        print(
            f"資料更新成功!新聞 {len(report.get('raw_news', []))} 則、"
            f"白話文來源:{report['dictionary_source']}。"
        )

        # 推播
        if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_TO"):
            print("推送 LINE 通知...")
            try:
                notify_line(report)
                print("  LINE 推播成功。")
            except Exception as exc:  # noqa: BLE001
                print(f"  警告: LINE 推播失敗:{exc}", file=sys.stderr)

        return 0

    except Exception as exc:  # noqa: BLE001 — CI 需要明確失敗碼
        print(f"資料更新失敗:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
