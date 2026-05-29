"""每日全球政經戰略情報自動產生器。

流程:
  1. 呼叫 Claude (claude-opus-4-8) 並開啟「伺服器端網路搜尋」工具,
     讓模型先抓取最近的真實外電,再進行四維度戰略分析。
  2. 強制模型輸出單一合法 JSON。
  3. 在本地做防護式清理 + 解析 + 結構驗證後存檔。

輸出:
  - latest_report.json                 最新一份報告 (Streamlit 讀這份)
  - data/reports/<YYYY-MM-DD>.json      當日歷史存檔

環境變數:
  - ANTHROPIC_API_KEY   (必填) Anthropic API 金鑰
  - REPORT_TOPIC        (選填) 當日分析主題,預設為全球總經 + 地緣政治
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

def extract_json_text(content_blocks) -> str:
    """從回應的 content blocks 中取出 JSON 文字。

    web_search 會在回應中插入 server_tool_use / web_search_tool_result 等區塊,
    真正的 JSON 在 text 區塊裡。這裡把所有 text 區塊串起來再做清理。
    """
    text = "".join(b.text for b in content_blocks if getattr(b, "type", None) == "text")
    text = text.strip()

    # 移除可能殘留的 markdown 圍欄
    if text.startswith("```"):
        # 去掉開頭 ```json 或 ``` 這一行
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()

    # 萬一前後仍夾雜文字,擷取第一個 { 到最後一個 }
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]

    return text.strip()


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
# 主流程
# ---------------------------------------------------------------------------

def get_ai_macro_analysis(client: anthropic.Anthropic, topic: str, today: str) -> dict:
    user_prompt = build_user_prompt(topic, today)

    # 對話歷史 — 用於 server-side 工具的 pause_turn 續跑
    messages = [{"role": "user", "content": user_prompt}]
    final = None

    for attempt in range(MAX_CONTINUATIONS):
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # 穩定的大型 system prompt 標記為可快取(最佳實務;
                    # 每日單次呼叫多半不會命中,但無害且符合慣例)
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
        detail = getattr(final, "stop_details", None)
        raise RuntimeError(f"模型拒絕回答: {detail}")

    if final.stop_reason == "max_tokens":
        raise RuntimeError("輸出被 max_tokens 截斷,JSON 不完整;請調高 MAX_TOKENS")

    json_text = extract_json_text(final.content)
    if not json_text:
        raise ValueError("回應中找不到 JSON 文字內容")

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        # 印出前 500 字協助除錯
        raise ValueError(
            f"JSON 解析失敗: {exc}\n--- 原始內容前 500 字 ---\n{json_text[:500]}"
        ) from exc

    validate_report(data)
    return data


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("錯誤: 未設定 ANTHROPIC_API_KEY 環境變數", file=sys.stderr)
        return 1

    topic = os.environ.get("REPORT_TOPIC", DEFAULT_TOPIC)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    client = anthropic.Anthropic()

    try:
        print(f"開始向 AI 請求 {today} 的戰略情報(主題:{topic})...")
        report = get_ai_macro_analysis(client, topic, today)

        # 寫入最新報告
        OUTPUT_LATEST.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 寫入歷史存檔
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        (ARCHIVE_DIR / f"{today}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        news_count = len(report.get("raw_news", []))
        print(f"資料更新成功!共 {news_count} 則新聞,已儲存 latest_report.json")
        return 0

    except Exception as exc:  # noqa: BLE001 — CI 需要明確失敗碼
        print(f"資料更新失敗:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
