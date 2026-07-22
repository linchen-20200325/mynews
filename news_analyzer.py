"""
news_analyzer.py — 新聞分析工具函數 SSOT（CalcEngine 層）

收納所有新聞分析共用邏輯（日期萃取、關鍵字比對、時間窗口統計、情感評分），
供 update_data.py 與 feature_aligner.py 共用，杜絕重複定義。

設計原則：
  - 純計算層（CalcEngine），零 HTTP 請求、零 Gemini 呼叫
  - 所有公開函數名稱以動詞開頭（單一職責）
  - 全部加型別提示；空輸入、None 均有防禦性處理
"""

from __future__ import annotations

import re

# ── 情感關鍵字（SSOT，原 feature_aligner.py 私有常數）────────────────────────
BULL_WORDS: list[str] = [
    "上漲", "大漲", "突破", "創高", "反彈",
    "買超", "利多", "降息", "強彈", "拉升", "轉強",
]
BEAR_WORDS: list[str] = [
    "下跌", "大跌", "破底", "崩跌", "賣超",
    "利空", "升息", "衰退", "拋售", "恐慌", "下殺", "暴跌",
]

_SPLITTER = re.compile(r"[()()\[\]/、,，]+")


# ── 公開 API ──────────────────────────────────────────────────────────────────

def extract_news_date(item: dict) -> str:
    """從新聞 dict 萃取日期字串（YYYY-MM-DD）；無合法日期則回空字串。

    期望欄位 ``published`` 為 ISO8601 字串，取前 10 碼即為日期。
    """
    raw = str(item.get("published") or "").strip()
    if len(raw) >= 10 and raw[4:5] == "-":
        return raw[:10]
    return ""


def expand_match_keys(keys: list[str]) -> list[str]:
    """將中英對照名（如「輝達(Nvidia)」）拆成可比對的子鍵，提高命中率。

    切分規則：依括號 ()（）[]、斜線 /、頓號、逗號拆分；
    保留長度 ≥ 2 的片段；全部小寫以利不分大小寫比對。
    """
    if not keys:
        return []

    expanded: set[str] = set()
    for raw_key in keys:
        key = str(raw_key or "").strip()
        if len(key) >= 2:
            expanded.add(key)
        for part in _SPLITTER.split(key):
            part = part.strip()
            if len(part) >= 2:
                expanded.add(part)
    return [k.lower() for k in expanded]


def matches_news_keywords(keys: list[str], item: dict) -> bool:
    """判斷單則新聞的標題 + 摘要是否包含任一關鍵字（供台媒整站 feed 過濾）。"""
    if not keys or not item:
        return False
    normalized = expand_match_keys(keys)
    if not normalized:
        return False
    haystack = (
        str(item.get("title", "")) + " " + str(item.get("summary", ""))
    ).lower()
    return any(k in haystack for k in normalized)


def count_keyword_mentions(keys: list[str], news: list[dict]) -> dict:
    """從新聞列表統計關鍵字命中的則數與首見/最近見報日期。

    對標題 + 摘要做不分大小寫子字串比對；全部由真實新聞算出，不交給模型臆測。

    Returns
    -------
    dict
        ``{"news_count": int, "first_seen"?: str, "last_seen"?: str}``
    """
    if not keys or not news:
        return {"news_count": 0}
    normalized = expand_match_keys(keys)
    if not normalized:
        return {"news_count": 0}

    matched_dates: list[str] = []
    hit_count = 0

    for article in news:
        haystack = (
            str(article.get("title", "")) + " " + str(article.get("summary", ""))
        ).lower()
        if any(k in haystack for k in normalized):
            hit_count += 1
            date = extract_news_date(article)
            if date:
                matched_dates.append(date)

    result: dict = {"news_count": hit_count}
    if matched_dates:
        matched_dates.sort()
        result["first_seen"] = matched_dates[0]
        result["last_seen"] = matched_dates[-1]
    return result


def summarize_news_span(news: list[dict]) -> dict:
    """統計整批新聞的時間跨度（則數、最早與最近日期）。

    Returns
    -------
    dict
        ``{"news_count": int, "first_seen"?: str, "last_seen"?: str}``
    """
    if not news:
        return {"news_count": 0}

    dates = sorted(
        d for d in (extract_news_date(n) for n in news) if d
    )
    result: dict = {"news_count": len(news)}
    if dates:
        result["first_seen"] = dates[0]
        result["last_seen"] = dates[-1]
    return result


def score_headline_sentiment(headlines: list[str]) -> float:
    """依多/空關鍵字計算情感分數：-1.0（極空）~ +1.0（極多）。

    各標題計算「多詞命中數 − 空詞命中數」，加總後除以標題數，
    正規化至 [-1, 1] 並四捨五入至小數第三位。
    """
    if not headlines:
        return 0.0

    total = sum(
        sum(1 for w in BULL_WORDS if w in h)
        - sum(1 for w in BEAR_WORDS if w in h)
        for h in headlines
    )
    return round(max(-1.0, min(1.0, total / len(headlines))), 3)


# ── 佐證來源核對（F10：把 AI 回傳的 evidence_news 與真實餵入新聞對帳）─────────────

_TITLE_KEEP = re.compile(r"[^0-9a-z一-鿿]+")  # 只留數字/小寫拉丁/CJK，其餘皆去


def _norm_url(url) -> str:
    """URL 正規化成可比對鍵：去 scheme/www、前後空白、尾斜線、小寫。"""
    u = str(url or "").strip().lower()
    for pre in ("https://", "http://"):
        if u.startswith(pre):
            u = u[len(pre):]
            break
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def _norm_title(title) -> str:
    """標題正規化成可比對鍵：小寫後去空白/標點/全形符號，只留字母數字與漢字。"""
    return _TITLE_KEEP.sub("", str(title or "").lower())


def verify_evidence_news(parsed, real_news: list[dict]) -> dict:
    """核對 AI 回傳的每一則 evidence_news 是否真的出自本次餵入的新聞（硬規則1 可查核化）。

    走訪 parsed（dict/list 任意巢狀），對每一份 evidence_news 內的每則以
    「url 正規化為主、title 正規化為輔」比對真實新聞；命中標 ``verified=True``、
    否則 ``False``（就地寫回該則 dict）。**保守偏誤**：只採正規化後「精確相等」比對，
    寧可標『無法核對』也不亂標『已核對』——一個 ✓ 保證該來源確實在本次新聞中。

    翻譯後標題（如人物/美股報告英轉中）title 對不上時，僅靠 url；AI 若漏帶 url，
    該則會被誠實標為『無法核對』（非等同造假）。

    Returns
    -------
    dict
        ``{"matched": int, "total": int}``（供記錄／測試；呼叫端可忽略回傳值）。
    """
    real_urls = {
        _norm_url(n.get("url")) for n in (real_news or [])
        if isinstance(n, dict) and n.get("url")
    }
    real_urls.discard("")
    real_titles = {
        _norm_title(n.get("title")) for n in (real_news or [])
        if isinstance(n, dict) and n.get("title")
    }
    real_titles.discard("")
    tally = {"matched": 0, "total": 0}

    def _mark(item: dict) -> None:
        url_ok = bool(item.get("url")) and _norm_url(item.get("url")) in real_urls
        title_ok = bool(item.get("title")) and _norm_title(item.get("title")) in real_titles
        ok = bool(url_ok or title_ok)
        item["verified"] = ok
        tally["total"] += 1
        tally["matched"] += int(ok)

    def _walk(obj) -> None:
        if isinstance(obj, dict):
            for key, val in obj.items():
                if key == "evidence_news" and isinstance(val, list):
                    for it in val:
                        if isinstance(it, dict):
                            _mark(it)
                else:
                    _walk(val)
        elif isinstance(obj, list):
            for x in obj:
                _walk(x)

    _walk(parsed)
    return tally
