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

import housing_fetcher
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
DEFAULT_TREND_QUERIES = [
    "類股 題材 資金流向",
    "AI 半導體 投資",
    "產業 趨勢 基金",
]
DEFAULT_STOCK_QUERIES = [
    "台股 個股 焦點",
    "台積電 聯發科 鴻海 台股",
    "台股 外資 法人 買超",
    "台股 類股 漲跌",
    "上市 上櫃 營收 財報",
]

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

【真實性】evidence_news 請優先引用使用者提供的真實新聞,嚴禁虛構標題/媒體/數據。

【強制輸出規範:Zero-Tolerance】
1. 最終回覆只能有一個合法 JSON 物件,前後不得有任何其他文字或 ```json 標記。
2. 必須能被 Python json.loads() 解析。
3. heat_score 為 0~100 整數,代表綜合熱度;trends 依 heat_score 由高到低排序。

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


HOUSING_SYSTEM_PROMPT = """\
你是一位兼具「房地產市場研究員」與「後端資料工程師」的純資料生成器。
你會收到一批【已由爬蟲抓取的真實台灣房市新聞】(含標題、來源、連結、摘要),
可能再附上【實價登錄各縣市每坪均價(萬元/坪,政府真實統計)】當參考。
你的任務是:【只根據這些真實新聞】判讀房市冷熱與打房政策,並把新聞講到的縣市標出來,
最後【嚴格且唯一】輸出合法 JSON。

【判讀重點】
1. 預售屋市場(presale_market)與成屋/中古屋市場(resale_market)目前各自偏
   「熱絡 / 持平 / 冷清」,並用一句白話說明依據(務必對應新聞,不可臆測)。
2. 打房政策(policy):整理新聞提到的政府打房/信用管制措施(如央行選擇性信用管制、
   平均地權條例、囤房稅 2.0、限貸令等),每項用白話說明對買賣方的影響。
3. 分區(regions):把新聞中明確提到的『縣市』各標一個冷熱傾向與 0~100 熱度分,
   county 欄位只能填以下名稱之一(沒提到的縣市不要硬填):
   臺北市、新北市、桃園市、臺中市、臺南市、高雄市、基隆市、新竹市、新竹縣、苗栗縣、
   彰化縣、南投縣、雲林縣、嘉義市、嘉義縣、屏東縣、宜蘭縣、花蓮縣、臺東縣、澎湖縣、金門縣、連江縣。

【真實性】
- 房價數字若有附參考統計就照用,沒有就不要自己編造價格。冷熱/政策判讀要對應新聞。
- 你是中立的資訊整理,不是投資建議;不要喊買賣、不要預測漲跌幅。

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
        f"請參考以下真實新聞,找出當前全球最熱門、動能最強的 3~5 個新興產業或主題,"
        f"依資金、徵才、政策、技術四種訊號綜合評估與排名打分,並嚴格輸出 JSON。"
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


def build_housing_user_prompt(news: list[dict], prices: dict | None, today: str) -> str:
    return (
        f"今天的日期是 {today}。\n"
        f"請根據以下真實台灣房市新聞,判讀預售屋與成屋市場的冷熱、整理打房政策,"
        f"並把新聞提到的縣市標出冷熱與熱度分,嚴格輸出 JSON。report_date 請填 {today}。\n\n"
        f"【實價登錄參考房價】\n{format_house_price_block(prices)}\n\n"
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


def call_gemini_for_json(system_instruction: str, user_content: str) -> dict:
    """以 Gemini 讀取內容並回傳解析後的 JSON dict;多把金鑰會逐一嘗試。"""
    from google import genai
    from google.genai import types

    model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    keys = get_gemini_keys()
    if not keys:
        raise RuntimeError("未設定 GEMINI_API_KEY")

    last_exc: Exception | None = None
    for key in keys:
        try:
            client = genai.Client(api_key=key)
            resp = client.models.generate_content(
                model=model,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    temperature=0.7,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — 金鑰/額度/網路錯誤 → 換下一把
            last_exc = exc
            continue

        text = (resp.text or "").strip()
        if not text:
            last_exc = RuntimeError("Gemini 回傳空內容(可能被安全機制阻擋)")
            continue

        json_text = clean_json_text(text)
        try:
            return json.loads(json_text)
        except json.JSONDecodeError as exc:  # 解析失敗非金鑰問題,直接報錯
            raise ValueError(
                f"JSON 解析失敗: {exc}\n--- 原始內容前 500 字 ---\n{json_text[:500]}"
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
    return data


def get_housing_analysis(news: list[dict], prices: dict | None, today: str) -> dict:
    """Gemini 讀房市新聞(+實價登錄參考)→ 冷熱判讀 + 打房政策 + 縣市標記。"""
    data = call_gemini_for_json(
        HOUSING_SYSTEM_PROMPT, build_housing_user_prompt(news, prices, today)
    )
    data.setdefault("report_date", today)
    data.setdefault("regions", [])
    data.setdefault("policy", [])
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


def fetch_macro_news(topic: str, extra_query: str | None = None) -> list[dict]:
    lang = os.environ.get("NEWS_LANG", "zh")
    region = os.environ.get("NEWS_REGION", "TW")
    queries = parse_queries("NEWS_QUERIES", DEFAULT_NEWS_QUERIES)
    # 多主題時把該主題當額外關鍵字,讓不同主題抓到偏向該主題的新聞(短主題才適合當查詢)。
    if extra_query and 0 < len(extra_query) <= 30 and extra_query not in queries:
        queries = [extra_query] + queries
    topics = parse_topics("NEWS_TOPICS", DEFAULT_NEWS_TOPICS)
    # 動態(分類頭條)+ 財經官方 feed,確保不漏突發大事又不離題。
    feeds = {**news_fetcher.CREDIBLE_FEEDS, **section_feeds(topics, lang, region)}
    return news_fetcher.fetch_news(
        queries,
        lang=lang,
        region=region,
        feeds=feeds,
        limit=int(os.environ.get("NEWS_MAX", "12")),
        since_hours=int(os.environ.get("NEWS_SINCE_HOURS", "48")),
    )


def fetch_trend_news() -> list[dict]:
    lang = os.environ.get("NEWS_LANG", "zh")
    region = os.environ.get("NEWS_REGION", "TW")
    queries = parse_queries("TREND_QUERIES", DEFAULT_TREND_QUERIES)
    topics = parse_topics("TREND_TOPICS", DEFAULT_TREND_TOPICS)
    feeds = section_feeds(topics, lang, region)
    return news_fetcher.fetch_news(
        queries,
        lang=lang,
        region=region,
        feeds=feeds,
        limit=int(os.environ.get("NEWS_MAX", "12")),
        since_hours=int(os.environ.get("NEWS_SINCE_HOURS", "72")),
    )


def fetch_stock_news() -> list[dict]:
    """抓台灣財經/台股新聞(較大量,以利統計被提及次數)。"""
    lang = os.environ.get("NEWS_LANG", "zh")
    region = os.environ.get("NEWS_REGION", "TW")
    queries = parse_queries("STOCK_QUERIES", DEFAULT_STOCK_QUERIES)
    # 台股聚焦財經分類頭條 + 中央社財經 feed
    feeds = {"中央社 財經": news_fetcher.CREDIBLE_FEEDS.get("中央社 財經", "")}
    feeds = {k: v for k, v in feeds.items() if v}
    feeds.update(section_feeds(["BUSINESS"], lang, region))
    return news_fetcher.fetch_news(
        queries,
        lang=lang,
        region=region,
        feeds=feeds,
        limit=int(os.environ.get("STOCK_MAX", "25")),
        since_hours=int(os.environ.get("STOCK_SINCE_HOURS", "48")),
    )


def fetch_housing_news() -> list[dict]:
    """抓房市新聞(預售/成屋冷熱、打房政策);委派 housing_fetcher。"""
    return housing_fetcher.fetch_housing_news(
        limit=int(os.environ.get("HOUSING_MAX", "18")),
        since_hours=int(os.environ.get("HOUSING_SINCE_HOURS", "72")),
    )


# ---------------------------------------------------------------------------
# LINE 推播 (Messaging API push)
# ---------------------------------------------------------------------------

def build_line_message(report: dict, trends: dict | None = None,
                       housing: dict | None = None) -> str:
    """把報告整理成一則精簡的 LINE 文字訊息。"""
    lines = [
        f"🌐 全球政經戰略報告 {report.get('report_date', '')}",
        f"主題:{report.get('topic', '')}",
        "",
        "📰 焦點新聞:",
    ]
    news = report.get("raw_news", [])
    if news:
        for i, item in enumerate(news[:3], 1):
            title = item.get("title", "(無標題)")
            source = item.get("source", "")
            lines.append(f"{i}. {title}" + (f"({source})" if source else ""))
    else:
        lines.append("(本次未取得相關新聞)")

    kpi = report.get("strategic_analysis", {}).get("blind_spots_and_kpi", "").strip()
    if kpi:
        lines += ["", "🎯 盲點與領先指標:", kpi[:400] + ("..." if len(kpi) > 400 else "")]

    if trends and trends.get("trends"):
        lines += ["", "🔥 熱門產業 Top3:"]
        for t in trends["trends"][:3]:
            lines.append(
                f"・{t.get('industry', '')}(熱度 {t.get('heat_score', '—')})"
            )

    if housing:
        lines += ["", f"🏠 房市:整體{housing.get('overall_sentiment', '—')}"]
        presale = (housing.get("presale_market") or {}).get("sentiment")
        resale = (housing.get("resale_market") or {}).get("sentiment")
        if presale or resale:
            lines.append(f"・預售{presale or '—'} / 成屋{resale or '—'}")
        policy = housing.get("policy") or []
        if policy:
            lines.append("・打房政策:"
                         + "、".join(p.get("title", "") for p in policy[:2]))

    lines += ["", f"(白話文來源:{report.get('dictionary_source', '—')})"]

    msg = "\n".join(lines)
    if len(msg) > LINE_TEXT_LIMIT:
        msg = msg[:LINE_TEXT_LIMIT] + "\n...(訊息過長已截斷)"
    return msg


def notify_line(report: dict, trends: dict | None = None,
                housing: dict | None = None) -> None:
    """透過 LINE Messaging API push 推送報告摘要。"""
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    to = os.environ["LINE_TO"]

    payload = json.dumps(
        {"to": to, "messages": [{"type": "text",
                                 "text": build_line_message(report, trends, housing)}]}
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
        print(f"[1/5] 爬取真實外電並請 Gemini 分析(主題數:{len(topics)})...")

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

        print("[2/5] 戰略分析完成,寫入報告檔...")
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
            print("[3/5] 爬取產業新聞並向 Gemini 請求趨勢雷達...")
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
            print("[3/5] ENABLE_TREND_RADAR=0,略過趨勢雷達。")

        # C. 台股觀察
        if stock_picker_enabled():
            print("[4/5] 爬取台灣財經新聞並向 Gemini 整理台股標的...")
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
            print("[4/5] ENABLE_STOCK_PICKER=0,略過台股觀察。")

        # D. 房市觀察(房價走代理,排程無代理時就只用新聞 + repo 既有房價當參考)
        housing = None
        if housing_enabled():
            print("[5/5] 爬取房市新聞並向 Gemini 判讀冷熱 + 打房政策...")
            try:
                housing_news = fetch_housing_news()
                print(f"  抓到 {len(housing_news)} 則房市新聞。")
                prices = housing_fetcher.load_house_prices()
                housing = get_housing_analysis(housing_news, prices, today)
                housing["raw_news"] = housing_news
                save_json(OUTPUT_HOUSING, housing)
                save_json(HOUSING_ARCHIVE_DIR / f"{today}.json", housing)
                print(f"  房市觀察完成,整體氛圍:{housing.get('overall_sentiment', '—')}")
            except Exception as exc:  # noqa: BLE001 — 房市觀察失敗不影響戰略報告
                print(f"  警告: 房市觀察產生失敗:{exc}", file=sys.stderr)
        else:
            print("[5/5] ENABLE_HOUSING=0,略過房市觀察。")

        print(
            f"資料更新成功!新聞 {len(report.get('raw_news', []))} 則、"
            f"白話文來源:{report['dictionary_source']}。"
        )

        # 推播
        if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_TO"):
            print("推送 LINE 通知...")
            try:
                notify_line(report, trends, housing)
                print("  LINE 推播成功。")
            except Exception as exc:  # noqa: BLE001
                print(f"  警告: LINE 推播失敗:{exc}", file=sys.stderr)

        return 0

    except Exception as exc:  # noqa: BLE001 — CI 需要明確失敗碼
        print(f"資料更新失敗:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
