"""每日全球政經戰略情報自動產生器(雙模型版)。

流程:
  1. Claude (claude-opus-4-8) + 伺服器端 web_search:
     先抓取最近的真實外電,再進行四維度戰略分析,輸出 JSON 主體。
  2. Gemini:接手 Claude 的分析內容,產出「最終版白話文字典」
     (laymans_dictionary)。Gemini 失敗時自動回退使用 Claude 的字典。
  3. 本地做防護式清理 + 解析 + 結構驗證後存檔。

輸出:
  - latest_report.json                 最新一份報告 (Streamlit 讀這份)
  - data/reports/<YYYY-MM-DD>.json      當日歷史存檔

環境變數:
  - ANTHROPIC_API_KEY   (必填) Anthropic API 金鑰
  - GEMINI_API_KEY      (選填) Google Gemini 金鑰;設定後白話文改由 Gemini 產生
  - GEMINI_MODEL        (選填) Gemini 模型名稱,預設 gemini-2.5-flash
  - REPORT_TOPIC        (選填) 當日分析主題
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000
MAX_CONTINUATIONS = 5  # 防止 server-side 工具迴圈無限延伸

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_TOPIC = "全球總體經濟與地緣政治最新動態(中東局勢、美中科技戰、OPEC+原油、美國通膨與 Fed)"

OUTPUT_LATEST = Path("latest_report.json")
ARCHIVE_DIR = Path("data/reports")

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

ANALYSIS_LABELS = (
    ("geo_military", "【地緣政治與軍事戰略】"),
    ("supply_chain", "【原物料與供應鏈傳導】"),
    ("macro_economy", "【總體經濟與貨幣定價】"),
    ("blind_spots_and_kpi", "【全球大局觀與領先指標】"),
)

# ---------------------------------------------------------------------------
# 系統提示語 (純資料生成器 / Zero-Tolerance JSON)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
你是一位兼具「全球宏觀首席策略官」與「後端資料工程師」的純資料生成器。
你的唯一任務是:接收使用者提供的國際新聞主題,先使用 web_search 工具搜尋最近的
真實外電報導,再進行四維度深度戰略分析,並【嚴格且唯一】地輸出一份合法的 JSON。

【資料真實性】
1. 你必須先用 web_search 工具實際搜尋,raw_news 只能填入搜尋到的真實報導,
   嚴禁虛構任何標題、媒體或數據。若搜尋不到相關新聞,raw_news 請回傳空陣列 []。
2. summary 必須客觀中立,只陳述事件本身。

【強制輸出規範:Zero-Tolerance】
1. 你最終回覆的「文字內容」必須【只有】一個合法 JSON 物件,前後不得有任何其他文字。
2. 絕不允許輸出 JSON 以外的文字(不要說「好的」「這是報告」,不要加 ```json 之類的 markdown 標記)。
3. 輸出必須能被 Python 的 json.loads() 直接解析。
4. strategic_analysis 四個欄位請用「專業口吻」撰寫,可自由使用專業術語。
5. 凡是在 strategic_analysis 中使用到的專業經濟/軍事/金融術語,
   都必須挑出來放進 laymans_dictionary,並用像對高中生講話一樣的生活化白話文解釋。

【JSON 結構定義 — 必須完全符合】
{
  "report_date": "YYYY-MM-DD",
  "topic": "分析主題",
  "raw_news": [
    { "title": "新聞標題", "source": "媒體來源", "url": "原文連結(若有)", "summary": "客觀重點摘要" }
  ],
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

# Gemini:最終版白話文字典提示語
GEMINI_DICT_PROMPT = """\
你是一位「白話文翻譯官」。以下是一份專業的全球政經戰略分析。
請挑出文中所有的「專業經濟學名詞、軍事戰略術語、金融縮寫」(例如 Fed、殖利率、
利差、咽喉點、輸入型通膨、避險情緒等),並用最通俗、像對高中生講話一樣的生活化
譬喻逐一解釋。

【輸出規範】
- 只輸出一個合法的 JSON 陣列,不要任何其他文字或 markdown 標記。
- 格式:[{{"term": "專業術語", "explanation": "生活化白話文解釋"}}, ...]
- 解釋要用日常生活譬喻,絕對不要用更難的專有名詞去解釋專有名詞。
- 涵蓋分析中出現的每一個重要術語,至少 6 個。

【待解讀的分析內容】
{analysis}
"""


def build_user_prompt(topic: str, today: str) -> str:
    """每次呼叫變動的部分放使用者訊息,讓 system prompt 維持穩定(利於快取)。"""
    return (
        f"今天的日期是 {today}。\n"
        f"請針對主題『{topic}』,先用 web_search 搜尋最近 48 小時內最重要的 1~3 則"
        f"國際外電,再依四維度進行深度戰略分析,並嚴格輸出 JSON。"
        f"report_date 請填 {today}。"
    )


# ---------------------------------------------------------------------------
# JSON 防護式清理與解析
# ---------------------------------------------------------------------------

def clean_json_text(text: str) -> str:
    """去除 markdown 圍欄,並擷取最外層的 JSON 值(物件或陣列)。"""
    text = text.strip()

    # 移除可能殘留的 markdown 圍欄
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()

    # 萬一前後仍夾雜文字,擷取第一個 { 或 [ 到對應的結尾
    if text and text[0] not in "{[":
        candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if candidates:
            start = min(candidates)
            closer = "}" if text[start] == "{" else "]"
            end = text.rfind(closer)
            if end > start:
                text = text[start:end + 1]

    return text.strip()


def extract_json_text(content_blocks) -> str:
    """從 Claude 回應的 content blocks 中取出 JSON 文字。

    web_search 會插入 server_tool_use / web_search_tool_result 區塊,
    真正的 JSON 在 text 區塊裡。
    """
    text = "".join(b.text for b in content_blocks if getattr(b, "type", None) == "text")
    return clean_json_text(text)


def validate_report(data: dict) -> None:
    """最低限度的結構驗證,確保 Streamlit 端不會因缺欄位而崩潰。"""
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


# ---------------------------------------------------------------------------
# Claude:網路搜尋 + 四維度分析
# ---------------------------------------------------------------------------

def get_ai_macro_analysis(client: anthropic.Anthropic, topic: str, today: str) -> dict:
    user_prompt = build_user_prompt(topic, today)

    # 對話歷史 — 用於 server-side 工具的 pause_turn 續跑
    messages = [{"role": "user", "content": user_prompt}]
    final = None

    for _ in range(MAX_CONTINUATIONS):
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
        ) as stream:
            final = stream.get_final_message()

        # server-side 工具達迭代上限 → 把 assistant 回應接回去再續跑
        if final.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": final.content})
            continue
        break

    if final is None:
        raise RuntimeError("未取得任何 API 回應")
    if final.stop_reason == "refusal":
        raise RuntimeError(f"模型拒絕回答: {getattr(final, 'stop_details', None)}")
    if final.stop_reason == "max_tokens":
        raise RuntimeError("輸出被 max_tokens 截斷,JSON 不完整;請調高 MAX_TOKENS")

    json_text = extract_json_text(final.content)
    if not json_text:
        raise ValueError("回應中找不到 JSON 文字內容")

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 解析失敗: {exc}\n--- 原始內容前 500 字 ---\n{json_text[:500]}"
        ) from exc

    validate_report(data)
    return data


# ---------------------------------------------------------------------------
# Gemini:最終版白話文字典
# ---------------------------------------------------------------------------

def generate_laymans_dictionary_gemini(analysis: dict) -> list:
    """用 Gemini 依據分析內容產生最終版白話文字典(JSON 陣列)。"""
    from google import genai
    from google.genai import types

    model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    combined = "\n\n".join(
        f"{label}\n{analysis.get(key, '')}" for key, label in ANALYSIS_LABELS
    )
    prompt = GEMINI_DICT_PROMPT.format(analysis=combined)

    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )

    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini 回傳空內容(可能被安全機制阻擋)")

    data = json.loads(clean_json_text(text))

    # 容許 {"laymans_dictionary": [...]} 或直接是陣列
    if isinstance(data, dict) and isinstance(data.get("laymans_dictionary"), list):
        data = data["laymans_dictionary"]
    if not isinstance(data, list):
        raise ValueError("Gemini 字典格式不是陣列")

    cleaned = [
        {"term": str(d.get("term", "")), "explanation": str(d.get("explanation", ""))}
        for d in data
        if isinstance(d, dict) and d.get("term")
    ]
    if not cleaned:
        raise ValueError("Gemini 字典為空")
    return cleaned


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("錯誤: 未設定 ANTHROPIC_API_KEY 環境變數", file=sys.stderr)
        return 1

    topic = os.environ.get("REPORT_TOPIC") or DEFAULT_TOPIC
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    client = anthropic.Anthropic()

    try:
        print(f"[1/2] 向 Claude 請求 {today} 的戰略情報(主題:{topic})...")
        report = get_ai_macro_analysis(client, topic, today)
        report["dictionary_source"] = "claude"

        # 最終版白話文 — 改用 Gemini(失敗則回退 Claude 的字典)
        if os.environ.get("GEMINI_API_KEY"):
            print("[2/2] 向 Gemini 請求最終版白話文字典...")
            try:
                report["laymans_dictionary"] = generate_laymans_dictionary_gemini(
                    report["strategic_analysis"]
                )
                report["dictionary_source"] = "gemini"
            except Exception as exc:  # noqa: BLE001 — 字典失敗不應讓整份報告失敗
                print(f"  警告: Gemini 白話文產生失敗,回退使用 Claude 字典:{exc}",
                      file=sys.stderr)
        else:
            print("[2/2] 未設定 GEMINI_API_KEY,沿用 Claude 產生的白話文字典。")

        # 寫入最新報告 + 歷史存檔
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        OUTPUT_LATEST.write_text(payload, encoding="utf-8")
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        (ARCHIVE_DIR / f"{today}.json").write_text(payload, encoding="utf-8")

        print(
            f"資料更新成功!新聞 {len(report.get('raw_news', []))} 則、"
            f"白話文來源:{report['dictionary_source']}。已儲存 latest_report.json"
        )
        return 0

    except Exception as exc:  # noqa: BLE001 — CI 需要明確失敗碼
        print(f"資料更新失敗:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
