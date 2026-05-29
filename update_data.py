"""每日全球政經戰略情報自動產生器(RSS 爬蟲 + Gemini)。

資料流:
  1. news_fetcher (RSS):從具公信力的新聞來源抓真實外電(標題/來源/連結/摘要)。
  2. Gemini (gemini-2.5-flash):讀取抓回來的新聞 →
       A. 戰略報告 (latest_report.json):四維度深度戰略分析 + 白話文字典。
          raw_news 直接採用爬蟲抓到的真實新聞,絕不虛構。
       B. 趨勢雷達 (latest_trends.json):依「資金/徵才/政策/技術」四訊號排名打分,
          回答「現在最紅的產業是什麼」。 [可用 ENABLE_TREND_RADAR=0 關閉]
  3. 可選:把摘要推播到 LINE (Messaging API)。

環境變數:
  - GEMINI_API_KEY                 (必填) Gemini 金鑰
  - GEMINI_MODEL                   (選填) Gemini 模型,預設 gemini-2.5-flash
  - REPORT_TOPIC                   (選填) 戰略報告的分析主題
  - NEWS_QUERIES                   (選填) 戰略報告抓新聞的關鍵字,以 ; 分隔
  - TREND_QUERIES                  (選填) 趨勢雷達抓新聞的關鍵字,以 ; 分隔
  - NEWS_LANG / NEWS_REGION        (選填) Google News 語系/地區,預設 zh / TW
  - NEWS_MAX / NEWS_SINCE_HOURS    (選填) 抓新聞則數上限 / 回溯時數,預設 12 / 48
  - ENABLE_TREND_RADAR             (選填) 設為 0/false/no 可關閉趨勢雷達
  - LINE_CHANNEL_ACCESS_TOKEN/LINE_TO (選填) 兩者皆設才推播
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import news_fetcher

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# LINE Messaging API(LINE Notify 已於 2025 停用,改用 Messaging API push)
LINE_PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"
LINE_TEXT_LIMIT = 4500  # 單則 text 上限 5000,留安全餘裕

DEFAULT_TOPIC = "全球總體經濟與地緣政治最新動態(中東局勢、美中科技戰、OPEC+原油、美國通膨與 Fed)"

# 抓新聞用的預設關鍵字(可用 NEWS_QUERIES / TREND_QUERIES 覆寫)。
DEFAULT_NEWS_QUERIES = [
    "地緣政治",
    "聯準會 通膨",
    "OPEC 油價",
    "美中 科技戰",
    "中東 局勢",
]
DEFAULT_TREND_QUERIES = [
    "新興產業 募資",
    "AI 產業 投資",
    "科技業 徵才",
    "產業政策 補貼",
    "創投 新創",
]

OUTPUT_LATEST = Path("latest_report.json")
ARCHIVE_DIR = Path("data/reports")
OUTPUT_TRENDS = Path("latest_trends.json")
TRENDS_ARCHIVE_DIR = Path("data/trends")

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


# ---------------------------------------------------------------------------
# Gemini:共用的「讀新聞 → JSON」呼叫
# ---------------------------------------------------------------------------

def call_gemini_for_json(system_instruction: str, user_content: str) -> dict:
    """以 Gemini 讀取內容並回傳解析後的 JSON dict。"""
    from google import genai
    from google.genai import types

    model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    resp = client.models.generate_content(
        model=model,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            temperature=0.7,
        ),
    )

    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini 回傳空內容(可能被安全機制阻擋)")

    json_text = clean_json_text(text)
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 解析失敗: {exc}\n--- 原始內容前 500 字 ---\n{json_text[:500]}"
        ) from exc


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


# ---------------------------------------------------------------------------
# 新聞抓取設定
# ---------------------------------------------------------------------------

def parse_queries(env_name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env_name, "")
    queries = [q.strip() for q in raw.split(";") if q.strip()]
    return queries or default


def fetch_macro_news(topic: str) -> list[dict]:
    queries = parse_queries("NEWS_QUERIES", DEFAULT_NEWS_QUERIES)
    return news_fetcher.fetch_news(
        queries,
        lang=os.environ.get("NEWS_LANG", "zh"),
        region=os.environ.get("NEWS_REGION", "TW"),
        feeds=news_fetcher.CREDIBLE_FEEDS,
        limit=int(os.environ.get("NEWS_MAX", "12")),
        since_hours=int(os.environ.get("NEWS_SINCE_HOURS", "48")),
    )


def fetch_trend_news() -> list[dict]:
    queries = parse_queries("TREND_QUERIES", DEFAULT_TREND_QUERIES)
    return news_fetcher.fetch_news(
        queries,
        lang=os.environ.get("NEWS_LANG", "zh"),
        region=os.environ.get("NEWS_REGION", "TW"),
        limit=int(os.environ.get("NEWS_MAX", "12")),
        since_hours=int(os.environ.get("NEWS_SINCE_HOURS", "72")),
    )


# ---------------------------------------------------------------------------
# LINE 推播 (Messaging API push)
# ---------------------------------------------------------------------------

def build_line_message(report: dict, trends: dict | None = None) -> str:
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

    lines += ["", f"(白話文來源:{report.get('dictionary_source', '—')})"]

    msg = "\n".join(lines)
    if len(msg) > LINE_TEXT_LIMIT:
        msg = msg[:LINE_TEXT_LIMIT] + "\n...(訊息過長已截斷)"
    return msg


def notify_line(report: dict, trends: dict | None = None) -> None:
    """透過 LINE Messaging API push 推送報告摘要。"""
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    to = os.environ["LINE_TO"]

    payload = json.dumps(
        {"to": to, "messages": [{"type": "text", "text": build_line_message(report, trends)}]}
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("錯誤: 未設定 GEMINI_API_KEY 環境變數", file=sys.stderr)
        return 1

    topic = os.environ.get("REPORT_TOPIC") or DEFAULT_TOPIC
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        # A. 戰略報告
        print(f"[1/4] 爬取真實外電(主題:{topic})...")
        news = fetch_macro_news(topic)
        print(f"  抓到 {len(news)} 則新聞。")
        if not news:
            print("  警告: 未抓到任何新聞,分析將缺乏真實素材。", file=sys.stderr)

        print("[2/4] 向 Gemini 請求四維度戰略分析 + 白話文...")
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

        save_json(OUTPUT_LATEST, report)
        save_json(ARCHIVE_DIR / f"{today}.json", report)

        # B. 趨勢雷達
        trends = None
        if trend_radar_enabled():
            print("[3/4] 爬取產業新聞並向 Gemini 請求趨勢雷達...")
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
            print("[3/4] ENABLE_TREND_RADAR=0,略過趨勢雷達。")

        print(
            f"[4/4] 資料更新成功!新聞 {len(report.get('raw_news', []))} 則、"
            f"白話文來源:{report['dictionary_source']}。"
        )

        # 推播
        if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_TO"):
            print("推送 LINE 通知...")
            try:
                notify_line(report, trends)
                print("  LINE 推播成功。")
            except Exception as exc:  # noqa: BLE001
                print(f"  警告: LINE 推播失敗:{exc}", file=sys.stderr)

        return 0

    except Exception as exc:  # noqa: BLE001 — CI 需要明確失敗碼
        print(f"資料更新失敗:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
