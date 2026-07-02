"""prompt_builder.py — Gemini user prompt builder 函數集中地（SSOT）。

4 個 format_* 格式化 helper + 10 個 build_*_user_prompt builder，
原本散落在 update_data.py，統一搬移至此。
update_data.py 一律 from prompt_builder import 取用，不得重複定義。
"""
from __future__ import annotations

import json

import index_fetcher  # 取 DEFAULT_DROP_THRESHOLD 用於 format_quotes_block


# ── Format helpers ──────────────────────────────────────────────────────────

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


def format_house_history_block(history: dict | None, top_n: int = 8) -> str:
    """把歷年每坪均價整理成精簡趨勢區塊(取成屋成交量較大的代表縣市,控制 token)。"""
    counties = (history or {}).get("counties") or {}
    years = (history or {}).get("years") or []
    if not counties or len(years) < 2:
        return "(本次未附歷年房價,請依當期房價與新聞研判趨勢)"
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


# ── Builder functions ────────────────────────────────────────────────────────

def _compose(today: str, instruction: str, news: list[dict]) -> str:
    """5 個標準 builder 共用模板：日期 header + 指令 body + report_date + news footer。"""
    return (
        f"今天的日期是 {today}。\n"
        f"{instruction}"
        f"report_date 請填 {today}。\n\n"
        f"{format_news_block(news)}"
    )


def build_analysis_user_prompt(news: list[dict], topic: str, today: str) -> str:
    return (
        f"今天的日期是 {today}。分析主題:『{topic}』。\n"
        f"以下是爬蟲抓到的真實新聞,請只根據這些新聞做四維度戰略分析並輸出 JSON:\n\n"
        f"{format_news_block(news)}"
    )


def build_trend_user_prompt(news: list[dict], today: str) -> str:
    return _compose(today,
        "請參考以下真實新聞(同時含台灣與美國市場),找出當前全球最熱門、動能最強的 "
        "3~5 個新興產業或主題,依資金、徵才、政策、技術四種訊號綜合評估與排名打分,"
        "並【每個產業都列出代表性的美股(us_stocks)與台股(tw_stocks)個股】,嚴格輸出 JSON。\n",
        news)


def build_stock_user_prompt(news: list[dict], today: str) -> str:
    return _compose(today,
        "請根據以下真實台灣財經新聞,整理出被提到的台股標的,統計提及次數並由高到低排序,"
        "判斷各自偏利多/利空/觀望並說明原因,另歸納未來趨勢產業與夕陽產業,嚴格輸出 JSON。\n",
        news)


def build_us_stock_user_prompt(news: list[dict], today: str) -> str:
    return _compose(today,
        "請根據以下真實美股相關財經新聞,整理出被提到的美股標的,統計提及次數並由高到低排序,"
        "判斷各自偏利多/利空/觀望並說明原因,另歸納未來趨勢產業與夕陽產業,嚴格輸出 JSON。\n",
        news)


def build_intl_alert_user_prompt(
    quotes_doc: dict,
    news: list[dict],
    today: str,
    divergence: dict | None = None,
) -> str:
    div_block = ""
    if divergence and divergence.get("signal") != "normal" and divergence.get("description"):
        sig_label = {
            "reversal": "翻轉訊號",
            "follow_through": "跌勢延伸",
            "caution": "盤前預警",
        }.get(divergence["signal"], divergence["signal"])
        div_block = (
            f"\n\n【期現背離偵測（程式算，非 AI）】\n"
            f"費半現貨(已定案):{divergence.get('sox_pct', 0):+.1f}%  "
            f"{divergence.get('futures_name', '期貨')}(即時):{divergence.get('futures_pct', 0):+.1f}%  "
            f"背離:{divergence.get('divergence', 0):+.1f}%  訊號:{sig_label}\n"
            f"請在 tw_impact.reason 中明確說明此期現背離對台股開盤的潛在影響。"
        )
    return (
        f"今天的日期是 {today}。\n"
        f"請依下列『真實報價』與『真實新聞』:(1) 研判美股/台指期夜盤是否突然大跌或有重大利空;"
        f"(2) 無論有無大跌,都要給出『對美股的整體看法(us_view)』與『對台股(尤其半導體/電子)"
        f"的可能影響(tw_impact)』,平靜日也要有方向與理由、不可留白。嚴格輸出 JSON。"
        f"數字一律以報價為準、不可竄改;利空原因只能引用新聞。report_date 請填 {today}。\n\n"
        f"{format_quotes_block(quotes_doc)}"
        f"{div_block}\n\n"
        f"{format_news_block(news)}"
    )


def build_focus_user_prompt(term_zh: str, query_en: str, news: list[dict], today: str) -> str:
    return _compose(today,
        f"關注對象(中文):{term_zh};英文檢索主名:{query_en}。\n"
        "請根據以下真實英文新聞,整理這個對象近期說了什麼/做了什麼、衍伸哪些產業,"
        "以及可能牽動哪些【台股與美股】個股(務必兩個市場都找),全部用繁體中文輸出,"
        "嚴格輸出 JSON。\n",
        news)


def build_stock_query_user_prompt(
    term_zh: str, query_en: str, ticker: str, market: str,
    news: list[dict], today: str,
) -> str:
    tag = f"{term_zh}" + (f"({ticker})" if ticker else "") + (f"／{market}" if market else "")
    return _compose(today,
        f"目標個股:{tag};英文名:{query_en}。\n"
        "請產出券商深度報告風格的『個股健診』,全部用繁體中文輸出,嚴格輸出 JSON,涵蓋:\n"
        "① 新聞相關性(高/中/低 + 條列依據,只依新聞);\n"
        "② 股價與籌碼動向(盤面/量能、外資投信自營/融資方向、技術面位置);\n"
        "③ 基本面與推升動能(營運績效、題材 catalysts、上漲屬短期消息面或基本面可持續);\n"
        "④ 護城河與競爭(是否龍頭、競爭對手、技術門檻、本檔所屬產業鏈上中下游代表個股,美股→對應台股);\n"
        "⑤ 估值與風險(本益比/EPS 推算與同業區間、主要風險點)、後續觀察指標、長期持有研判。\n"
        "可動用產業/財務常識補數字,但每個數字須標〔新聞〕或〔AI估算〕,代號不確定一律留空,不喊買賣不給目標價。\n",
        news)


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


def build_news_etf_user_prompt(news_text: str, today: str) -> str:
    """新聞 ETF 策略:把使用者貼上的新聞文本包成 user content。"""
    return (
        f"今天日期:{today}。請依系統指示,對以下新聞/時事文本做台股 ETF 策略分析:\n\n"
        f"{news_text.strip()}"
    )


def build_housing_reg_user_prompt(news: list[dict], today: str) -> str:
    """房產法規月報：把法規新聞包成 user content，請 Gemini 整理法規現況。"""
    return (
        f"今天的日期是 {today}。\n"
        f"請根據以下台灣房產法規相關新聞，整理目前主要法規的現況與對買方的影響，"
        f"並補充新聞未提到但已穩定施行的重要法規（標注「穩定施行中」）。"
        f"嚴格輸出 JSON。report_date 請填 {today}。\n\n"
        f"【房產法規相關新聞】\n{format_news_block(news)}"
    )


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
