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
  - GEMINI_MAX_TOKENS              (選填) 單次輸出 token 上限,預設 16384
  - EARLIEST_TW_HHMM               (選填) schedule 早於此台灣時刻(HHMM)就略過,預設 0530
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
  - LINE_CHANNEL_ACCESS_TOKEN/LINE_TO (選填) 兩者皆設才推播。LINE_TO 群體發送:
      填 "broadcast"=發給所有好友;填多個 ID(逗號分隔)=群播名單;單一 ID=push(可為群組 ID)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import gemini_client  # Gemini API 封裝 SSOT(多 Key/多模型/退避/JSON 解析)
import line_notify  # LINE 推播 SSOT(路由/訊息組建/事件去重)
import chip_calendar  # 法人籌碼:可預測賣壓事件行事曆(純規則,零網路零 AI)
import config  # 環境變數讀取 + 功能開關的 SSOT
import chip_fetcher  # 法人籌碼:抓證交所三大法人買賣超(事後驗證,真實數字)
import chip_signals  # 個股盯盤:個股三大法人買賣超(T86)籌碼面訊號的 SSOT
import earnings_fetcher  # 個股盯盤:抓證交所 OpenAPI 月營收(真實財報更新訊號)
import freshness  # 資料新鮮度(staleness)守門的單一真相源(SSOT,§2.4)
import futures_chip_fetcher  # 法人籌碼:抓期交所三大法人台指期留倉(外資期貨偏多/偏空)
import housing_fetcher
import index_fetcher  # 國際盤預警:抓美股指數/美股期貨真實漲跌幅
import margin_fetcher  # 融資餘額:散戶槓桿/斷頭訊號(共振偵測用)
import news_fetcher
import numutil  # 數值計算的單一真相源(SSOT):pct_change / parse_number / OKU
import paths  # 檔案/目錄路徑的單一真相源(SSOT)
import tech_signals  # 個股技術面訊號(日K→均線/KD/RSI)的單一真相源(SSOT)
import tz_utils  # 台灣時區時間的單一真相源(SSOT)
import vcp_signals  # 個股盯盤:VCP 波動收縮型態買點偵測的單一真相源(SSOT)
import watchlist  # 個股盯盤清單(watchlist)的單一真相源(SSOT)

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

DEFAULT_NEWS_MAX = 25  # 單一主題抓新聞則數預設;可用 NEWS_MAX 覆寫
OKU = numutil.OKU  # 億元換算係數 SSOT 在 numutil,此處保留別名供本檔既有參照


# 資料新鮮度門檻(§2.4)— 過期→該章節 raise 被既有 try/except 接住→略過,不拖垮主報告。
# 天數依官方發布頻率設定,可用環境變數覆寫(門檻屬領域決策,具名常數帶入而非寫死於 freshness)。
CHIP_STALE_DAYS = config.env_int("CHIP_STALE_DAYS", 5)     # 籌碼(三大法人/融資/台指期留倉)走證交所每日
HOUSE_STALE_DAYS = config.env_int("HOUSE_STALE_DAYS", 40)  # 房價走內政部實價登錄,每約 10 天一批


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

# 路徑一律取自 paths.py(SSOT);此處只保留本檔慣用的別名,引用處不動。
OUTPUT_LATEST = paths.LATEST_REPORT
ARCHIVE_DIR = paths.ARCHIVE_REPORTS
OUTPUT_REPORTS_MULTI = paths.LATEST_REPORTS_MULTI
REPORTS_MULTI_ARCHIVE_DIR = paths.ARCHIVE_REPORTS_MULTI
OUTPUT_TRENDS = paths.LATEST_TRENDS
TRENDS_ARCHIVE_DIR = paths.ARCHIVE_TRENDS
OUTPUT_STOCKS = paths.LATEST_STOCKS
STOCKS_ARCHIVE_DIR = paths.ARCHIVE_STOCKS
OUTPUT_US_STOCKS = paths.LATEST_US_STOCKS
US_STOCKS_ARCHIVE_DIR = paths.ARCHIVE_US_STOCKS
OUTPUT_INTL_ALERT = paths.LATEST_INTL_ALERT
INTL_ALERT_ARCHIVE_DIR = paths.ARCHIVE_INTL_ALERT
OUTPUT_CHIP = paths.LATEST_CHIP
CHIP_ARCHIVE_DIR = paths.ARCHIVE_CHIP
OUTPUT_MARGIN = paths.LATEST_MARGIN  # 融資餘額(散戶斷頭訊號)
OUTPUT_FUT_CHIP = paths.LATEST_FUT_CHIP  # 三大法人台指期留倉(外資期貨偏多/偏空)
OUTPUT_FOCUS = paths.LATEST_FOCUS
FOCUS_ARCHIVE_DIR = paths.ARCHIVE_FOCUS
OUTPUT_HOUSING = paths.LATEST_HOUSING
HOUSING_ARCHIVE_DIR = paths.ARCHIVE_HOUSING

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
  (A)【真實指數/期貨報價漲跌幅】(已由程式抓自 Yahoo Finance / 期交所,含當日漲跌%);
  (B)【真實財經新聞】(美股為主,多為英文原文,含標題/來源/連結/摘要)。

時區常識(供你判斷時間差):
  - 美股指數收盤約台灣時間清晨,對台股開盤是【隔夜領先】訊號;美股期貨是【盤前即時】風向。
  - 台指期夜盤(台灣 15:00–次日 05:00)直接反映台股對隔夜美股的定價,屬【盤前即時】最直接訊號。

你的任務:【只根據 (A) 的真實數字與 (B) 的真實新聞】,做兩件事(無論有無大跌,每天都要完整給出):
  1. 研判「美股/台指期夜盤是否出現突然大跌或重大利空」並列出利空原因;
  2. 給出「對美股的整體看法(us_view)」與「對今日/隔日台股的可能影響(tw_impact)」——
     即使盤勢平靜、無大跌,也要依真實報價與新聞給出當下的方向研判與理由,不可留白。
最後【嚴格且唯一】輸出合法 JSON。

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
  "summary": "一句話總結:今日美股/台指期夜盤氛圍(平靜或大跌)、台股要不要當心",
  "interpretation": [
    {
      "market": "美股|台指期夜盤|半導體類股…",
      "cause": "依新聞說明這波下跌/利空的原因(新聞沒提就寫『新聞未明確說明』)",
      "evidence_news": [ { "title": "新聞標題", "source": "媒體來源", "url": "連結(若有)" } ]
    }
  ],
  "us_view": {
    "direction": "偏多|偏空|中性",
    "reason": "依真實報價與新聞,說明當前美股(大盤/科技/半導體)的整體研判與主要驅動;平靜時也要說明為何中性/偏多/偏空,不可留白",
    "focus": ["當前最該盯的美股焦點(如 Fed 利率、科技財報、AI 類股…)", "..."]
  },
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


NEWS_ETF_STRATEGY_SYSTEM_PROMPT = """\
你是一位精通全球總體經濟、科技產業供應鏈(半導體/AI/電子)與台股 ETF 策略的首席投資策略師。
你會收到一則【新聞/時事文本】。請進行深度因果鏈推導,轉化為台股 ETF 的「進場佈局/持有/出場」實戰決策,
最後【嚴格且唯一】輸出合法 JSON。

【真實性規範(最重要)】
- 只依提供的新聞文本與你的產業常識推導,嚴禁虛構新聞中沒有的事實。
- 【嚴禁亂編 ETF 代號】:只給「真實存在的台股 ETF」(例:0050、006208、0056、00878、00891、00929、00632R 等);
  代號不確定一律留空字串,寧缺勿錯。你是中立資訊整理,非投資建議,不喊買賣、不保證漲跌。
- 一切數字與漲跌幅皆為示意/推估,請在 data_notes 提醒使用者自行核對即時行情與溢價率。

【分析流程 → 對應 JSON 欄位】
第一階段 phase1_causal:先判斷 category 屬「總經/地緣政治」或「產業供應鏈/科技技術」。
  - core_turn:一句話點出對市場最關鍵的衝擊(核心轉折)。
  - first_order:第一層效應(總經→資金避險/冒險、對原油/美債/美股大盤的短期影響;
    產業→核心瓶頸或核心機會在哪個環節)。
  - horizon_nature:這是短期雜音還是長期趨勢?
第二階段 phase2_camps:台股供應鏈依基本面分三陣營(各 1~4 個,元素含 industry/sector + reason 一句):
  - victims:營收立刻受阻或成本立刻暴增的利空受害族群。
  - beneficiaries:有報價話語權、毛利逆勢暴升,或成本大降的受惠族群。
  - foreign_reflow:恐慌消除後最先迎來外資回補的板塊或大型權值股。
第三階段 phase3_etf:台股 ETF 實戰佈局(只列真實台股 ETF 代號+名稱):
  - offense:1~2 檔最契合「受惠族群」的 ETF,logic 說明其指數篩選邏輯為何能賺到這波紅利。
  - defense:是否有優質 ETF 被無辜波及/短期壓制,logic 說明是否適合修正 10~15% 後左側定期定額佈局。
  - safety_check:一句提醒,如何用溢價率/日均成交量避免買在嚴重溢價高點。
第四階段 phase4_playbook:
  - holding_period:明確定性(「2-4 週短期事件交易」/「1-2 季中期循環」/「6-12 個月以上長線產能擴張」),
    不要用「長期投資」等模糊字眼。
  - exit_signals:2~4 個明確出場/獲利了結訊號(如上游產能開出、代工營收大爆發、原物料跌幅滿足、ETF 異常高溢價)。

【語言】一律繁體中文。【精簡】各陣營≤4、ETF 各≤2、exit_signals≤4、各敘述≤120 字。

【強制輸出:Zero-Tolerance】只輸出一個合法 JSON,前後不得有任何其他文字或 ```json 標記,能被 json.loads() 解析。

【JSON 結構 — 必須完全符合】
{
  "report_date": "YYYY-MM-DD",
  "news_summary": "一句話總結這則新聞",
  "category": "總經/地緣政治",
  "phase1_causal": {"core_turn": "...", "first_order": "...", "horizon_nature": "..."},
  "phase2_camps": {
    "victims": [{"industry": "產業", "reason": "為何受害"}],
    "beneficiaries": [{"industry": "產業", "reason": "為何受惠/定價權"}],
    "foreign_reflow": [{"sector": "板塊/權值股", "reason": "為何最先回補"}]
  },
  "phase3_etf": {
    "offense": [{"ticker": "0050", "name": "元大台灣50", "logic": "篩選邏輯為何賺到紅利"}],
    "defense": [{"ticker": "", "name": "ETF名", "logic": "為何被波及 + 左側佈局建議"}],
    "safety_check": "用溢價率/成交量避免買在溢價高點的提醒"
  },
  "phase4_playbook": {
    "holding_period": "2-4 週短期事件交易",
    "exit_signals": ["明確出場訊號1", "..."]
  },
  "data_notes": "本分析為 AI 依新聞推導,ETF 代號與數字僅供參考、非即時,請自行核對行情與溢價率,非投資建議。"
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
        f"請依下列『真實報價』與『真實新聞』:(1) 研判美股/台指期夜盤是否突然大跌或有重大利空;"
        f"(2) 無論有無大跌,都要給出『對美股的整體看法(us_view)』與『對台股(尤其半導體/電子)"
        f"的可能影響(tw_impact)』,平靜日也要有方向與理由、不可留白。嚴格輸出 JSON。"
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


def _validate_structure(
    data: dict,
    *,
    required_lists: tuple = (),
    nonempty_lists: tuple = (),
    required_dicts: tuple = (),
    nonempty_dicts: tuple = (),
) -> None:
    if "report_date" not in data:
        raise ValueError("缺少 report_date")
    for k in required_lists:
        if not isinstance(data.get(k), list):
            raise ValueError(f"{k} 必須是陣列")
    for k in nonempty_lists:
        if not isinstance(data.get(k), list) or not data[k]:
            raise ValueError(f"{k} 必須是非空陣列")
    for k in required_dicts:
        if not isinstance(data.get(k), dict):
            raise ValueError(f"{k} 必須是物件")
    for k in nonempty_dicts:
        if not isinstance(data.get(k), dict) or not data[k]:
            raise ValueError(f"{k} 必須是非空字典")


def validate_trends(data: dict) -> None:
    """趨勢雷達的最低限度結構驗證。"""
    _validate_structure(data, nonempty_lists=("trends",))


def validate_stocks(data: dict) -> None:
    """台股觀察的最低限度結構驗證。"""
    _validate_structure(data, nonempty_lists=("stocks",))


def validate_us_stocks(data: dict) -> None:
    """美股觀察的最低限度結構驗證(與台股同契約)。"""
    _validate_structure(data, nonempty_lists=("stocks",))


def validate_intl_alert(data: dict) -> None:
    """國際盤預警的最低限度結構驗證(報價必須是非空字典:無真實數字就不該成立)。"""
    _validate_structure(data, nonempty_dicts=("quotes",), required_dicts=("tw_impact", "us_view"))


def validate_focus(data: dict) -> None:
    """全球人物追蹤的最低限度結構驗證(允許 stocks 為空:該對象未必對應到個股)。"""
    _validate_structure(data, required_lists=("stocks",))


def validate_stock_query(data: dict) -> None:
    """個股健診的最低限度結構驗證。"""
    _validate_structure(data, required_lists=("relevance_points",))


def validate_news_etf(data: dict) -> None:
    """新聞 ETF 策略的最低限度結構驗證(四階段皆須為物件)。"""
    _validate_structure(data, required_dicts=("phase1_causal", "phase2_camps", "phase3_etf", "phase4_playbook"))


def validate_housing(data: dict) -> None:
    """房市觀察的最低限度結構驗證。"""
    _validate_structure(data, required_lists=("regions",))


def get_macro_analysis(news: list[dict], topic: str, today: str) -> dict:
    """Gemini 讀新聞 → 四維度分析 + 白話文字典。"""
    data = gemini_client.call_gemini_for_json(
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
        "laymans_dictionary": gemini_client.normalize_dictionary(data.get("laymans_dictionary")),
    }


def get_trend_radar(news: list[dict], today: str) -> dict:
    """Gemini 讀新聞 → 趨勢雷達。"""
    data = gemini_client.call_gemini_for_json(
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
    data = gemini_client.call_gemini_for_json(
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
    data = gemini_client.call_gemini_for_json(
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

    # 台指期夜盤(期交所即時):台股自身對隔夜的定價,屬最直接的【盤前即時】訊號。
    # 走 NAS 代理抓;失敗(沙箱無網路/期交所擋境外/代理不通)只略過,不影響其他報價與 LINE。
    try:
        import taifex_night_fetcher
        night = taifex_night_fetcher.fetch_night_quote(log=print)
        if night:
            qmap[night["symbol"]] = {
                "name": night["name"], "group": night["group"],
                "lead_type": night["lead_type"], "last": night["last"],
                "prev": night["prev"], "change_pct": night["change_pct"],
                "is_drop": night["change_pct"]
                <= quotes_doc.get("threshold", index_fetcher.DEFAULT_DROP_THRESHOLD),
            }
    except Exception as exc:  # noqa: BLE001 — 夜盤抓取失敗不得拖垮國際盤預警
        print(f"  警告: 台指期夜盤抓取失敗,略過:{exc}", file=sys.stderr)

    # 真實大跌清單(由報價計算,非 AI):跌幅由深到淺排序。債匯(殖利率/美元)不算大跌。
    drops = sorted(
        (
            {"symbol": sym, "name": q.get("name", sym),
             "change_pct": q.get("change_pct", 0), "lead_type": q.get("lead_type", "")}
            for sym, q in qmap.items()
            if q.get("is_drop") and q.get("group") != "債匯"
        ),
        key=lambda d: d["change_pct"],
    )

    news = fetch_intl_alert_news()
    # Gemini 只做文字研判(利空原因/對台股影響);失敗(如配額 429)不得拖垮
    # 真實報價與大跌偵測 → 包成容錯:AI 掛了仍保留報價/大跌/LINE 預警,只把原因留白。
    try:
        gemini = gemini_client.call_gemini_for_json(
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
    us_view = gemini.get("us_view")
    if not isinstance(us_view, dict):
        us_view = {"direction": "中性", "reason": "", "focus": []}
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
        "us_view": us_view,                   # 對美股整體看法(平靜日也有)
        "tw_impact": tw_impact,
        "ai_ok": ai_ok,                       # AI 解讀是否成功(供前端/LINE 標註「原因待補」)
        "upcoming_events": upcoming_events,    # 可預測法人賣壓事件(行事曆)
        "raw_news": news,
    }
    validate_intl_alert(result)
    return result


def translate_focus_query(term_zh: str) -> dict:
    """把中文關注對象轉成英文新聞檢索詞(Gemini)。"""
    data = gemini_client.call_gemini_for_json(
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
    data = gemini_client.call_gemini_for_json(
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
    data = gemini_client.call_gemini_for_json(
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


def build_news_etf_user_prompt(news_text: str, today: str) -> str:
    """新聞 ETF 策略:把使用者貼上的新聞文本包成 user content。"""
    return (
        f"今天日期:{today}。請依系統指示,對以下新聞/時事文本做台股 ETF 策略分析:\n\n"
        f"{news_text.strip()}"
    )


def get_news_etf_strategy(news_text: str, today: str) -> dict:
    """Gemini 讀一則新聞 → 首席策略師四階段台股 ETF 進場/持有/出場決策。"""
    data = gemini_client.call_gemini_for_json(
        NEWS_ETF_STRATEGY_SYSTEM_PROMPT,
        build_news_etf_user_prompt(news_text, today),
    )
    data.setdefault("report_date", today)
    data.setdefault("phase1_causal", {})
    data.setdefault("phase2_camps", {})
    data.setdefault("phase3_etf", {})
    data.setdefault("phase4_playbook", {})
    data.setdefault("data_notes", "")
    validate_news_etf(data)
    return data


MARKET_DIGEST_SYSTEM_PROMPT = """\
你是首席投資策略師。你會收到某個市場領域(台股/美股/全球/台灣房市)當日各面板的重點數據(JSON)。
請把它們融成一段「統一研判」,不要逐項複述數字,而是點出彼此的連動與含義。

輸出規範:
- overall:整體傾向,僅能是「偏多」「偏空」「中性」三者之一。
- digest_markdown:3~5 點 markdown 條列(每點以「- 」開頭),繁體中文、口語白話、≤350 字,
  點出今日該領域最關鍵的訊號、面板之間的因果連動,以及最該留意的風險。
- 只依提供的數據與常識,嚴禁虛構數據沒有的事實;不喊買賣、不保證漲跌;結尾不必加免責(由前端統一標註)。

只輸出合法 JSON:{"overall": "偏空", "digest_markdown": "- 重點1\\n- 重點2"}
"""


def build_market_digest_prompt(view: str, payload: dict, today: str) -> str:
    """把該領域當日各面板數據瘦身成精簡 JSON brief(去掉 raw_news 等重欄位、截斷長字串)。"""
    def _slim(d):
        if isinstance(d, dict):
            return {k: _slim(v) for k, v in d.items()
                    if k not in ("raw_news", "evidence_news", "laymans_dictionary")}
        if isinstance(d, list):
            return [_slim(x) for x in d[:12]]
        if isinstance(d, str):
            return d[:300]
        return d

    brief = json.dumps(_slim(payload), ensure_ascii=False)[:6000]
    return (
        f"領域:{view}。今天日期:{today}。以下是當日各面板的重點數據(JSON),"
        f"請依系統指示融成統一研判:\n{brief}"
    )


def get_market_digest(view: str, payload: dict, today: str) -> dict:
    """Gemini 把某領域當日各面板數據融成一段統一研判(overall + digest_markdown)。"""
    data = gemini_client.call_gemini_for_json(
        MARKET_DIGEST_SYSTEM_PROMPT, build_market_digest_prompt(view, payload, today)
    )
    data.setdefault("overall", "中性")
    data.setdefault("digest_markdown", "")
    return data


def get_focus_analysis(term_zh: str, query_en: str, news: list[dict], today: str) -> dict:
    """Gemini 讀全球新聞 → 該對象說了什麼 + 衍伸產業 + 牽動的台股/美股個股。"""
    data = gemini_client.call_gemini_for_json(
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
    data = gemini_client.call_gemini_for_json(
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
        limit=int(os.environ.get("NEWS_MAX", str(DEFAULT_NEWS_MAX))),
        since_hours=int(os.environ.get("NEWS_SINCE_HOURS", str(SIX_MONTHS_HOURS))),
    )


def fetch_trend_news() -> list[dict]:
    """抓產業趨勢新聞:台灣(中文)+ 美國(英文)兩邊都抓,讓趨勢雷達含到美股。"""
    lang = os.environ.get("NEWS_LANG", "zh")
    region = os.environ.get("NEWS_REGION", "TW")
    queries = parse_queries("TREND_QUERIES", DEFAULT_TREND_QUERIES)
    topics = parse_topics("TREND_TOPICS", DEFAULT_TREND_TOPICS)
    feeds = section_feeds(topics, lang, region)
    limit = int(os.environ.get("NEWS_MAX", str(DEFAULT_NEWS_MAX)))
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
    "Taiwan stock futures TAIEX overnight",
    "semiconductor chip stocks selloff",
]


def fetch_intl_alert_news() -> list[dict]:
    """抓國際盤預警用新聞:美股財經頭條 + 美/韓大跌相關英文關鍵字,輔以台媒中文角度。"""
    en_queries = parse_queries("INTL_ALERT_QUERIES", DEFAULT_INTL_ALERT_QUERIES)
    en_feeds = section_feeds(["BUSINESS"], "en", "US")
    zh_feeds = {"中央社 財經": news_fetcher.CREDIBLE_FEEDS.get("中央社 財經", "")}
    zh_feeds = {k: v for k, v in zh_feeds.items() if v}
    return fetch_bilingual_news(
        zh_queries=parse_queries("INTL_ALERT_QUERIES_ZH", ["美股 大跌 重挫", "台指期 夜盤 台股期貨"]),
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




def detect_pressure_confluence(intl: dict | None, chip: dict | None,
                               margin: dict | None, fut_chip: dict | None = None) -> dict:
    """多重賣壓共振:美股領先大跌(必要門檻)+ 四力成立≥2 → 觸發。全用真實數字,門檻 env 可調。

    四力:外資提款、散戶斷頭(融資)、Fed 收緊(殖利率/美元)、配息賣壓(除權息窗口+法人賣超)。
    外資提款=賣股+空單+匯出,任一成立即點亮:現貨賣超 / 台指期偏空(留倉淨空)/ 台幣明顯貶值。
    """
    quotes = (intl or {}).get("quotes", {})
    us_drops = line_notify.lead_market_drops(intl or {})  # 美股隔夜/盤前領先大跌 = 必要門檻
    forces: list[dict] = []
    days = (chip or {}).get("days") or []

    def _num(d: dict | None, key: str):
        """取數值;缺鍵/非數字回 None(Fail-Loud:缺值不得當 0 拿去比較或計算)。"""
        v = (d or {}).get(key)
        return v if isinstance(v, (int, float)) else None

    def _miss(tag: str) -> None:
        print(f"  [共振] {tag}資料缺漏,略過此項判定(不以 0 充數)", flush=True)

    # F1 外資提款:現貨賣超 / 台指期偏空 / 台幣貶值,任一成立即點亮(賣股+空單+匯出)
    f1_reasons: list[str] = []
    if days:
        foreign0 = _num(days[0], "foreign")
        total0 = _num(days[0], "total")
        total1 = _num(days[1], "total") if len(days) >= 2 else None
        if foreign0 is None:
            _miss("外資現貨買賣超")
        else:
            f0 = foreign0 / OKU
            cons2 = (total0 is not None and total1 is not None
                     and total0 < 0 and total1 < 0)
            if f0 <= -config.env_float("FORCE_FOREIGN_SELL_YI", 150) or (f0 < 0 and cons2):
                f1_reasons.append(f"現貨賣超{abs(f0):.0f}億"
                                  + ("、連2日站賣方" if cons2 else ""))
    if fut_chip and fut_chip.get("stance") == "偏空":
        net_oi = _num(fut_chip, "foreign_net_oi")
        if net_oi is None:
            _miss("台指期外資淨口數")
        else:
            net = abs(net_oi)
            f1_reasons.append("台指期偏空(淨空"
                              + (f"{net / 1e4:.1f}萬口)" if net >= 10000 else f"{net:,}口)"))
    twd_up = _num(quotes.get("TWD=X") or {}, "change_pct")
    if twd_up is not None and twd_up >= config.env_float("FORCE_TWD_DROP_PCT", 0.3):
        f1_reasons.append(f"台幣貶{twd_up:.1f}%(外資匯出)")
    if f1_reasons:
        forces.append({"key": "外資提款", "detail": "、".join(f1_reasons)})

    # F4 散戶斷頭:融資餘額單日大減
    if margin:
        pct = _num(margin, "margin_chg_pct")
        if pct is None:
            _miss("融資增減%")
        elif pct <= -config.env_float("FORCE_MARGIN_DROP_PCT", 1.5):
            forces.append({"key": "散戶斷頭",
                           "detail": f"融資單日減{abs(pct):.1f}%(去槓桿/斷頭)"})

    # F3 Fed 收緊:10年期殖利率跳升,或美元走強
    # bps 只在 last/prev 都有時才算(缺一律不充 0,避免噴出假 bps)。
    tnx, dxy = quotes.get("^TNX") or {}, quotes.get("DX-Y.NYB") or {}
    tnx_last, tnx_prev = _num(tnx, "last"), _num(tnx, "prev")
    dxy_chg = _num(dxy, "change_pct")
    bps = (tnx_last - tnx_prev) * 100 if (tnx_last is not None and tnx_prev is not None) else None
    if bps is not None and bps >= config.env_float("FORCE_YIELD_UP_BPS", 4):
        forces.append({"key": "Fed收緊",
                       "detail": f"10年美債殖利率 +{bps:.0f}bps(資金收緊)"})
    elif dxy_chg is not None and dxy_chg >= config.env_float("FORCE_DXY_UP_PCT", 0.4):
        forces.append({"key": "Fed收緊",
                       "detail": f"美元指數 +{dxy_chg:.1f}%(資金收緊)"})

    # F2 配息賣壓:除權息/ETF除息窗口(5 交易日內)且三大法人賣超
    exdiv = [e for e in (intl or {}).get("upcoming_events", [])
             if e.get("type") in ("除權息旺季", "ETF除息潮")
             and 0 <= e.get("trading_days_until", 99) <= 5]
    total0_f2 = _num(days[0], "total") if days else None
    if exdiv and total0_f2 is not None and total0_f2 < 0:
        forces.append({"key": "配息賣壓", "detail": f"{exdiv[0]['title']}+法人賣超"})

    return {"triggered": bool(us_drops) and len(forces) >= 2,
            "us_drops": us_drops, "forces": forces, "count": len(forces)}



# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------

def save_json(path: Path, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return payload



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

# ---------------------------------------------------------------------------
# 個股盯盤(第二個 LINE bot:LINE_WATCH_TOKEN / LINE_WATCH_TO)
#   早上排程讀 watchlist.json,逐檔抓真實新聞 → Gemini 個股消息面總結,
#   並抓最新月營收(真實財報更新訊號,dedup 只推新公告),用第二個 bot push 給使用者。
#   未設第二個 bot 的 token/對象時整段靜默略過(程式先就緒,設好 Secrets 即上線)。
# ---------------------------------------------------------------------------

WATCH_SYSTEM_PROMPT = """\
你是台股個股盯盤助理。使用者提供一份「個股 → 近期真實新聞」清單,請逐檔做消息面總結。

鐵則:
1. 只根據提供的新聞做判斷,嚴禁虛構任何事件、數字或消息。
2. 某檔若沒有相關或重大的新聞,summary 就寫「近期無重大消息」,sentiment 設「中性」。
3. summary 限 1~2 句、繁體中文、口語白話,點出對股價最關鍵的那件事。
4. sentiment 僅能是「利多」「利空」「中性」三者之一。

只輸出 JSON,格式:
{"stocks":[{"ticker":"2330","name":"台積電","sentiment":"利多","summary":"..."}]}
"""



def fetch_watch_news(stocks: list[dict]) -> dict[str, list[dict]]:
    """逐檔抓個股近期真實新聞(RSS,無 API 額度顧慮);回 {ticker: [news...]}。"""
    out: dict[str, list[dict]] = {}
    for s in stocks:
        ticker = str(s.get("ticker", "")).strip()
        if not ticker:
            continue
        name = (s.get("name") or "").strip()
        label = f"{name} {ticker}".strip() if name else ticker
        try:
            out[ticker] = news_fetcher.fetch_news(
                [label, f"{name or ticker} 股價 法人"],
                lang="zh", region="TW",
                limit=int(os.environ.get("WATCH_NEWS_MAX", "8")),
                since_hours=int(os.environ.get("WATCH_SINCE_HOURS", "96")),
            )
        except Exception as exc:  # noqa: BLE001 — 單檔抓新聞失敗不影響其他檔
            print(f"  警告: {label} 新聞抓取失敗:{exc}", file=sys.stderr)
            out[ticker] = []
    return out


def summarize_watch_stocks(stocks: list[dict], news_by_ticker: dict[str, list[dict]]) -> list[dict]:
    """把各檔個股新聞整批交給 Gemini,一次回傳逐檔消息面總結(省 API 額度)。"""
    blocks = []
    for s in stocks:
        ticker = str(s.get("ticker", "")).strip()
        name = (s.get("name") or "").strip()
        items = news_by_ticker.get(ticker, [])[:8]
        lines = [f"【{name or ''} {ticker}】"]
        if items:
            for n in items:
                lines.append(f"- {n.get('title', '')}({n.get('source', '')})")
        else:
            lines.append("-(近期無抓到相關新聞)")
        blocks.append("\n".join(lines))
    user_content = (
        "以下是各檔個股的近期真實新聞,請逐檔做消息面總結:\n\n" + "\n\n".join(blocks)
    )
    data = gemini_client.call_gemini_for_json(WATCH_SYSTEM_PROMPT, user_content)
    result = data.get("stocks")
    return result if isinstance(result, list) else []


def load_pushed_revenue() -> list[str]:
    """讀已推播過的月營收 id(``ticker-YYYY-MM``)清單;無檔回空。"""
    try:
        return list(json.loads(
            paths.WATCH_REVENUE_PUSHED.read_text(encoding="utf-8")).get("ids", []))
    except Exception:  # noqa: BLE001 — 無檔/壞檔 → 視為尚未推過
        return []


def save_pushed_revenue(ids: list[str]) -> None:
    """寫回已推月營收 id 清單(只留最近 500 筆,避免無限增長)。"""
    save_json(paths.WATCH_REVENUE_PUSHED, {"ids": ids[-500:]})



def _push_watch_for(today: str, stocks: list[dict], to: str, pushed: list[str],
                    dedup_prefix: str = "") -> list[str]:
    """為一份清單抓消息面+技術面+月營收並推給單一對象 to;回本次新推的營收 dedup id。

    不負責存檔(由 run_watch_section 統一 load/save 一次 pushed 清單);dedup_prefix 讓
    per-user 各自獨立判斷財報新舊(同一檔不同人都能各自收到一次月營收通知)。
    """
    if not stocks:
        return []
    # 1) 逐檔抓真實新聞 → Gemini 一次總結(省額度)
    try:
        news_by_ticker = fetch_watch_news(stocks)
        summaries = summarize_watch_stocks(stocks, news_by_ticker)
    except Exception as exc:  # noqa: BLE001 — Gemini 過載等 → 消息面降級為空,仍續推財報
        print(f"  警告: 個股消息面總結失敗(Gemini 可能過載):{exc}", file=sys.stderr)
        summaries = []

    # 2) 技術面(個股日K → 均線/KD/RSI 白話一行);抓不到的代號不收錄,該檔靜默略過
    tech_lines: dict[str, str] = {}
    try:
        tech_lines = tech_signals.signals_for(stocks, log=print)
    except Exception as exc:  # noqa: BLE001 — 技術面整批失敗不影響消息面/財報推播
        print(f"  警告: 技術面整批計算失敗:{exc}", file=sys.stderr)

    # 2.5) 籌碼面(個股三大法人買賣超 T86 → 外資/投信張數 + 連買連賣);抓不到的代號靜默略過
    chip_lines: dict[str, str] = {}
    try:
        chip_lines = chip_signals.signals_for(stocks, log=print)
    except Exception as exc:  # noqa: BLE001 — 籌碼面整批失敗不影響消息面/技術面/財報推播
        print(f"  警告: 籌碼面整批計算失敗:{exc}", file=sys.stderr)

    # 2.6) VCP 波動收縮型態買點(個股日K → 收縮序列+量縮);未成形的代號靜默略過
    vcp_lines: dict[str, str] = {}
    try:
        vcp_lines = vcp_signals.signals_for(stocks, log=print)
    except Exception as exc:  # noqa: BLE001 — VCP 整批失敗不影響消息面/技術面/籌碼面/財報推播
        print(f"  警告: VCP 整批偵測失敗:{exc}", file=sys.stderr)

    # 3) 月營收(真實財報更新訊號);dedup 只推「新出現」的期別
    new_revenue: list[dict] = []
    fresh_ids: list[str] = []
    try:
        revenue = earnings_fetcher.fetch_monthly_revenue(
            watchlist.tickers({"stocks": stocks}), log=print)
        for ticker, rev in revenue.items():
            rid = f"{dedup_prefix}{ticker}-{rev.get('period')}"
            if rid not in pushed:
                new_revenue.append(rev)
                fresh_ids.append(rid)
    except Exception as exc:  # noqa: BLE001 — 財報抓取失敗不影響消息面推播
        print(f"  警告: 月營收抓取失敗:{exc}", file=sys.stderr)

    # 4) 季報 EPS;dedup key 含期別(如「eps-2330-2026-Q1」)
    new_eps: list[dict] = []
    fresh_eps_ids: list[str] = []
    try:
        eps_data = earnings_fetcher.fetch_quarterly_eps(
            watchlist.tickers({"stocks": stocks}), log=print)
        for ticker, eps in eps_data.items():
            rid = f"{dedup_prefix}eps-{ticker}-{eps.get('period')}"
            if rid not in pushed:
                new_eps.append(eps)
                fresh_eps_ids.append(rid)
    except Exception as exc:  # noqa: BLE001 — EPS 失敗不影響消息面/月營收推播
        print(f"  警告: 季報 EPS 抓取失敗:{exc}", file=sys.stderr)

    if not summaries and not new_revenue and not new_eps:
        return []

    msg = line_notify.build_watch_line_message(
        today, summaries, new_revenue, tech_lines, chip_lines, vcp_lines, new_eps)
    line_notify._push_line_text(msg, token=os.environ["LINE_WATCH_TOKEN"], to=to)
    print(
        f"  · 推給 {to[:6]}…:消息面 {len(summaries)} 檔、技術面 {len(tech_lines)} 檔、"
        f"籌碼面 {len(chip_lines)} 檔、VCP {len(vcp_lines)} 檔、"
        f"月營收 {len(new_revenue)} 筆、季報 EPS {len(new_eps)} 筆"
    )
    return fresh_ids + fresh_eps_ids


def run_watch_section(today: str) -> None:
    """個股盯盤主流程:per-user 逐人各推自己清單;舊扁平格式維持單一推。整段失敗不外溢。"""
    doc = watchlist.load()
    pushed = load_pushed_revenue()

    if watchlist.is_per_user(doc):
        uids = watchlist.user_ids(doc)
        if not uids:
            print("  個股盯盤:尚無任何使用者清單,略過。")
            return
        print(f"  個股盯盤(per-user):{len(uids)} 位使用者,各推自己清單...")
        fresh_all: list[str] = []
        for uid in uids:
            fresh_all += _push_watch_for(
                today, watchlist.user_stocks(doc, uid), to=uid,
                pushed=pushed, dedup_prefix=f"{uid}-")
        if fresh_all:
            save_pushed_revenue(pushed + fresh_all)
        print("  ⑤ 個股盯盤(per-user)處理完畢。")
        return

    # 舊扁平格式:維持今天行為(整份清單單一推 LINE_WATCH_TO),平滑過渡到 per-user
    stocks = doc.get("stocks", [])
    if not stocks:
        print("  個股盯盤:watchlist 為空(傳「加 2330」給盯盤 bot 即可建立),略過。")
        return
    print(f"  個股盯盤:清單 {len(stocks)} 檔,抓新聞 + 技術面 + 月營收...")
    fresh = _push_watch_for(today, stocks, to=os.environ["LINE_WATCH_TO"], pushed=pushed)
    if fresh:
        save_pushed_revenue(pushed + fresh)
    print("  ⑤ 個股盯盤已推。")


def _schedule_guard(now_tw, today: str) -> bool:
    """排程前哨守門。若應略過本次跑動回 True。"""
    floor = os.environ.get("EARLIEST_TW_HHMM", "0530").strip()
    try:
        fh, fm = int(floor[:2]), int(floor[2:])
    except (ValueError, IndexError):
        fh, fm = 5, 30
    if (now_tw.hour, now_tw.minute) < (fh, fm):
        print(f"排程於台灣 {now_tw:%H:%M} 觸發,早於資料齊備時間 "
              f"{fh:02d}:{fm:02d}(GitHub 排程器半夜亂觸發),略過——不推不完整凌晨版。")
        return True
    try:
        existing = json.loads(OUTPUT_LATEST.read_text(encoding="utf-8"))
        if existing.get("report_date") == today:
            print(f"今日({today})報告已存在,排程備援班次略過(避免重複推播)。")
            return True
    except Exception:  # noqa: BLE001 — 讀不到/壞檔 → 正常往下跑
        pass
    return False


def _run_strategic_report(today: str) -> dict | None:
    """A. 戰略報告(支援多主題)。降級時回 None。"""
    topics = parse_report_topics()
    multi = len(topics) > 1
    print(f"[1/8] 爬取真實外電並請 Gemini 分析(主題數:{len(topics)})...")
    print(f"  ▸ 主主題:{topics[0]}")
    try:
        report = build_macro_report(topics[0], today, extra_query=topics[0] if multi else None)
    except Exception as exc:  # noqa: BLE001 — Gemini 過載等 → 降級續跑
        print(f"  警告: 主報告產生失敗(Gemini 可能過載),降級續跑真實數據與預警:{exc}",
              file=sys.stderr)
        report = None
    reports = [report] if report else []
    for extra_topic in topics[1:]:
        print(f"  ▸ 次主題:{extra_topic}")
        try:
            reports.append(build_macro_report(extra_topic, today, extra_query=extra_topic))
        except Exception as exc:  # noqa: BLE001 — 次主題失敗不影響主報告
            print(f"  警告: 主題「{extra_topic}」產生失敗:{exc}", file=sys.stderr)
    if report:
        print("[2/8] 戰略分析完成,寫入報告檔...")
        save_json(OUTPUT_LATEST, report)
        save_json(ARCHIVE_DIR / f"{today}.json", report)
        if multi and reports:
            multi_doc = {"report_date": today, "reports": reports}
            save_json(OUTPUT_REPORTS_MULTI, multi_doc)
            save_json(REPORTS_MULTI_ARCHIVE_DIR / f"{today}.json", multi_doc)
            print(f"  多主題報告:{len(reports)}/{len(topics)} 份成功。")
    else:
        print("[2/8] 主報告降級(Gemini 過載),跳過戰略報告檔,續跑國際盤/籌碼/共振/LINE。")
    return report


def _run_trend_radar(today: str) -> None:
    """B. 趨勢雷達。"""
    if not config.trend_radar_enabled():
        print("[3/8] ENABLE_TREND_RADAR=0,略過趨勢雷達。")
        return
    print("[3/8] 爬取產業新聞並向 Gemini 請求趨勢雷達...")
    try:
        trend_news = fetch_trend_news()
        print(f"  抓到 {len(trend_news)} 則產業新聞。")
        trends = get_trend_radar(trend_news, today)
        save_json(OUTPUT_TRENDS, trends)
        save_json(TRENDS_ARCHIVE_DIR / f"{today}.json", trends)
        top = "、".join(t.get("industry", "") for t in trends["trends"][:3])
        print(f"  趨勢雷達完成,Top3:{top}")
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 趨勢雷達產生失敗:{exc}", file=sys.stderr)


def _run_stock_picks(today: str) -> None:
    """C. 台股觀察。"""
    if not config.stock_picker_enabled():
        print("[4/8] ENABLE_STOCK_PICKER=0,略過台股觀察。")
        return
    print("[4/8] 爬取台灣財經新聞並向 Gemini 整理台股標的...")
    try:
        stock_news = fetch_stock_news()
        print(f"  抓到 {len(stock_news)} 則台灣財經新聞。")
        stocks = get_stock_picks(stock_news, today)
        save_json(OUTPUT_STOCKS, stocks)
        save_json(STOCKS_ARCHIVE_DIR / f"{today}.json", stocks)
        top = "、".join(s.get("name", "") for s in stocks["stocks"][:5])
        print(f"  台股觀察完成,最常被提到:{top}")
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 台股觀察產生失敗:{exc}", file=sys.stderr)


def _run_us_stock_picks(today: str) -> None:
    """D. 美股觀察。"""
    if not config.us_stock_picker_enabled():
        print("[5/8] ENABLE_US_STOCK_PICKER=0,略過美股觀察。")
        return
    print("[5/8] 爬取美股財經新聞並向 Gemini 整理美股標的...")
    try:
        us_stock_news = fetch_us_stock_news()
        print(f"  抓到 {len(us_stock_news)} 則美股財經新聞。")
        us_stocks = get_us_stock_picks(us_stock_news, today)
        save_json(OUTPUT_US_STOCKS, us_stocks)
        save_json(US_STOCKS_ARCHIVE_DIR / f"{today}.json", us_stocks)
        top = "、".join(s.get("name", "") for s in us_stocks["stocks"][:5])
        print(f"  美股觀察完成,最常被提到:{top}")
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 美股觀察產生失敗:{exc}", file=sys.stderr)


def _run_intl_alert(today: str) -> dict | None:
    """D2. 國際盤預警。回傳 intl dict 或 None。"""
    if not config.intl_alert_enabled():
        print("[6/8] ENABLE_INTL_ALERT=0,略過國際盤預警。")
        return None
    print("[6/8] 抓國際盤報價(美股/期貨/台指期夜盤)偵測大跌,向 Gemini 解讀台股影響...")
    try:
        intl = build_intl_alert(today)
        save_json(OUTPUT_INTL_ALERT, intl)
        save_json(INTL_ALERT_ARCHIVE_DIR / f"{today}.json", intl)
        drops = intl.get("drops", [])
        tag = "、".join(f"{d['name']}{d['change_pct']:+.1f}%" for d in drops[:3]) or "無"
        print(f"  國際盤預警完成,警示級別:{intl.get('alert_level', '—')}(大跌:{tag})")
        return intl
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 國際盤預警產生失敗:{exc}", file=sys.stderr)
        return None


def _run_chip_data(today: str) -> tuple[dict | None, dict | None, dict | None]:  # noqa: ARG001
    """D3. 法人籌碼(三大法人 + 融資餘額 + 台指期留倉)。回傳 (chip, margin, fut_chip)。"""
    if not config.chip_enabled():
        print("[6/8] ENABLE_CHIP=0,略過法人籌碼。")
        return None, None, None
    print("[6/8] 抓證交所三大法人買賣超(近 N 日,事後驗證真實籌碼)...")
    chip = margin = fut_chip = None
    try:
        fetched = chip_fetcher.fetch_chip_flow(
            days=int(os.environ.get("CHIP_DAYS") or "10"), log=print)
        # §2.4 過期守門:歸屬日落後過久→raise→留 chip=None→共振自動略過此力量
        latest_date = fetched["days"][0]["date"] if fetched.get("days") else None
        freshness.ensure_fresh(latest_date, CHIP_STALE_DAYS, "三大法人籌碼")
        chip = fetched
        save_json(OUTPUT_CHIP, chip)
        if chip.get("days"):
            save_json(CHIP_ARCHIVE_DIR / f"{chip['days'][0]['date']}.json", chip)
            latest = chip["days"][0]
            print(f"  法人籌碼完成,最新 {latest['date']}:"
                  f"外資 {latest['foreign']/OKU:+.0f}億、投信 {latest['trust']/OKU:+.0f}億。")
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 法人籌碼抓取失敗:{exc}", file=sys.stderr)
    try:
        fetched = margin_fetcher.fetch_margin(log=print)
        if fetched:
            freshness.ensure_fresh(fetched.get("date"), CHIP_STALE_DAYS, "融資餘額")
            margin = fetched
            save_json(OUTPUT_MARGIN, margin)
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 融資餘額抓取失敗:{exc}", file=sys.stderr)
    try:
        fetched = futures_chip_fetcher.fetch_futures_chip(log=print)
        if fetched:
            freshness.ensure_fresh(fetched.get("date"), CHIP_STALE_DAYS, "台指期留倉")
            fut_chip = fetched
            save_json(OUTPUT_FUT_CHIP, fut_chip)
            print(f"  台指期留倉完成,外資{fut_chip['stance']}"
                  f"(淨{fut_chip['foreign_net_oi']:+,}口)。")
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 台指期留倉抓取失敗:{exc}", file=sys.stderr)
    return chip, margin, fut_chip


def _run_focus(today: str) -> None:
    """E. 全球人物追蹤。"""
    if not config.focus_enabled():
        print("[7/8] ENABLE_FOCUS=0,略過全球人物追蹤。")
        return
    print("[7/8] 翻譯追蹤對象並抓全球新聞,向 Gemini 整理台美股關聯...")
    try:
        focus_doc = build_focus_report(today)
        save_json(OUTPUT_FOCUS, focus_doc)
        save_json(FOCUS_ARCHIVE_DIR / f"{today}.json", focus_doc)
        names = "、".join(f.get("query_zh", "") for f in focus_doc["focuses"])
        print(f"  全球人物追蹤完成,對象:{names}")
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 全球人物追蹤產生失敗:{exc}", file=sys.stderr)


def _run_housing(today: str) -> None:
    """F. 房市觀察。"""
    if not config.housing_enabled():
        print("[8/8] ENABLE_HOUSING=0,略過房市觀察。")
        return
    print("[8/8] 爬取房市新聞並向 Gemini 判讀冷熱 + 打房政策...")
    try:
        housing_news = fetch_housing_news()
        print(f"  抓到 {len(housing_news)} 則房市新聞。")
        prices = housing_fetcher.load_house_prices()
        # §2.4 外科式守門:房價快取過期→只丟掉價格數字、保留新鮮的房市新聞判讀
        note = freshness.stale_note(prices.get("as_of"), HOUSE_STALE_DAYS, "實價登錄房價")
        if note:
            print(f"  {note} → 本次判讀不採用過期房價數字(僅依新聞)", file=sys.stderr)
            prices = {}
        history = housing_fetcher.load_house_price_history()
        housing = get_housing_analysis(housing_news, prices, today, history)
        housing["raw_news"] = housing_news
        save_json(OUTPUT_HOUSING, housing)
        save_json(HOUSING_ARCHIVE_DIR / f"{today}.json", housing)
        print(f"  房市觀察完成,整體氛圍:{housing.get('overall_sentiment', '—')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  警告: 房市觀察產生失敗:{exc}", file=sys.stderr)


def _run_line_push(
    report: dict | None,
    intl: dict | None,
    chip: dict | None,
    fut_chip: dict | None,
    conf: dict | None,
    today: str,
) -> None:
    """LINE 推播(依序:① 國際盤快報 → ② 共振預警 → ③ 法人事件預告 → ④ 戰略報告)。"""
    if not (os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_TO")):
        return
    print("推送 LINE 通知(依序:國際盤大跌→共振→事件預告→戰略報告)...")
    lead = line_notify.lead_market_drops(intl) if intl else []
    if intl and config.intl_alert_line_enabled():
        try:
            line_notify.notify_line_intl_alert(intl)
            tag = f"大跌 {len(lead)} 項" if lead else "平靜快報"
            print(f"  ① 國際盤快報已推({tag})。")
        except Exception as exc:  # noqa: BLE001
            print(f"  警告: 國際盤 LINE 推播失敗:{exc}", file=sys.stderr)
    if conf and conf.get("triggered") and config.confluence_line_enabled():
        try:
            line_notify.notify_line_confluence(conf, today)
            print("  ② 多重賣壓共振預警已推。")
        except Exception as exc:  # noqa: BLE001
            print(f"  警告: 共振 LINE 推播失敗:{exc}", file=sys.stderr)
    if intl and config.chip_line_enabled():
        try:
            pushed = line_notify.load_pushed_events()
            due = chip_calendar.pick_new_pushable(intl.get("upcoming_events", []), pushed)
            if due:
                line_notify.notify_line_chip_events(due, today)
                line_notify.save_pushed_events(pushed + [e["id"] for e in due])
                print(f"  ③ 法人事件預告已推({len(due)} 項)。")
        except Exception as exc:  # noqa: BLE001
            print(f"  警告: 法人事件 LINE 預告推播失敗:{exc}", file=sys.stderr)
    if report:
        try:
            line_notify.notify_line(report, line_notify.chip_flow_hint(chip, fut_chip))
            print("  ④ 戰略報告已推。")
        except Exception as exc:  # noqa: BLE001
            print(f"  警告: 戰略報告 LINE 推播失敗:{exc}", file=sys.stderr)
    else:
        print("  ④ 主報告降級(Gemini 過載),本次跳過戰略報告 LINE。")


def main() -> int:
    if not gemini_client.get_gemini_keys():
        print("錯誤: 未設定 GEMINI_API_KEY 環境變數", file=sys.stderr)
        return 1

    # 以台灣時區(UTC+8,無日光節約)定義「今天」
    now_tw = tz_utils.taiwan_now()
    today = now_tw.strftime("%Y-%m-%d")

    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and _schedule_guard(now_tw, today):
        return 0

    try:
        report = _run_strategic_report(today)
        _run_trend_radar(today)
        _run_stock_picks(today)
        _run_us_stock_picks(today)
        intl = _run_intl_alert(today)
        chip, margin, fut_chip = _run_chip_data(today)
        conf = None
        try:
            conf = detect_pressure_confluence(intl, chip, margin, fut_chip)
            forces = "、".join(f["key"] for f in conf["forces"]) or "無"
            print(f"  共振偵測:{'🔴 觸發' if conf['triggered'] else '未觸發'}"
                  f"(美股大跌={bool(conf['us_drops'])}、力量 {conf['count']}/4:{forces})")
        except Exception as exc:  # noqa: BLE001
            print(f"  警告: 共振偵測失敗:{exc}", file=sys.stderr)
        _run_focus(today)
        _run_housing(today)
        if report:
            print(
                f"資料更新成功!新聞 {len(report.get('raw_news', []))} 則、"
                f"白話文來源:{report.get('dictionary_source', '—')}。"
            )
        else:
            print("資料更新部分完成(主報告降級,國際盤/籌碼/共振仍已產出)。")
        _run_line_push(report, intl, chip, fut_chip, conf, today)
        if config.watch_enabled():
            print("個股盯盤(第二個 bot):自選台股消息面 + 月營收...")
            try:
                run_watch_section(today)
            except Exception as exc:  # noqa: BLE001
                print(f"  警告: 個股盯盤推播失敗:{exc}", file=sys.stderr)
        return 0

    except Exception as exc:  # noqa: BLE001 — CI 需要明確失敗碼
        print(f"資料更新失敗:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
