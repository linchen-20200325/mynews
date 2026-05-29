"""RSS 新聞抓取器 — 從具公信力的新聞來源抓取真實外電。

設計原則:
  * 只使用新聞網站「主動開放給程式讀取」的 RSS / feed(合法、穩定)。
  * 不硬爬付費牆網站的全文,避免違反其服務條款與著作權。
  * 只保留標題、來源、原文連結、摘要與發佈時間,並連回原文。
  * 純標準函式庫實作(urllib + xml.etree),不依賴第三方套件。

主要來源:
  1. Google News RSS 搜尋(依關鍵字聚合 BBC、AP、CNBC、Al Jazeera、Reuters、
     The Guardian、Nikkei 等可信來源)。
  2. 數個指定大媒體的官方 RSS feed(見 CREDIBLE_FEEDS)。

對外 API:
    fetch_news(queries, *, lang, region, feeds, limit, since_hours) -> list[dict]
    每則新聞為 {"title", "source", "url", "summary", "published"}。
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

# 指定的可信中文媒體官方 RSS feed(Google News 之外的補充來源)。
# 注意:個別 feed 若失效,fetch_feed 會自動略過(不影響其他來源)。
CREDIBLE_FEEDS: dict[str, str] = {
    "中央社 國際": "https://feeds.feedburner.com/rsscna/intworld",
    "中央社 兩岸": "https://feeds.feedburner.com/rsscna/mainland",
    "中央社 財經": "https://feeds.feedburner.com/rsscna/finance",
    "BBC 中文": "https://feeds.bbci.co.uk/zhongwen/trad/rss.xml",
    "德國之聲 DW 中文": "https://rss.dw.com/rdf/rss-chi-all",
}

HTTP_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; mynews-rss/1.0; +https://github.com/linchen-20200325/mynews)"

_TAG_RE = re.compile(r"<[^>]+>")


class _TextExtractor(HTMLParser):
    """把 RSS summary 裡的 HTML 標籤剝掉,只留純文字。"""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def strip_html(raw: str) -> str:
    """移除 HTML 標籤與多餘空白,回傳乾淨摘要。"""
    if not raw:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        text = parser.text()
    except Exception:  # noqa: BLE001 — HTML 解析失敗就退回粗略去標籤
        text = _TAG_RE.sub(" ", raw)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def google_news_rss_url(query: str, lang: str = "zh", region: str = "TW") -> str:
    """組出 Google News RSS 搜尋網址。"""
    ceid = f"{region}:{lang}"
    params = urllib.parse.urlencode(
        {"q": query, "hl": f"{lang}-{region}", "gl": region, "ceid": ceid}
    )
    return f"https://news.google.com/rss/search?{params}"


def google_news_topic_url(topic: str, lang: str = "zh", region: str = "TW") -> str:
    """Google News 分類頭條(不帶關鍵字,抓該分類『當下』的動態頭條)。

    topic 例:WORLD(世界)、BUSINESS(財經)、TECHNOLOGY(科技)、NATION(國內)。
    只取與主題相關的分類,可避免娛樂/體育等離題內容。
    """
    params = urllib.parse.urlencode(
        {"hl": f"{lang}-{region}", "gl": region, "ceid": f"{region}:{lang}"}
    )
    return f"https://news.google.com/rss/headlines/section/topic/{topic}?{params}"


def _local(tag: str) -> str:
    """去掉 XML 名稱空間,只留 local name(如 '{ns}title' -> 'title')。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(item: ET.Element, *names: str) -> str:
    """取第一個符合 local name 的子元素文字。"""
    wanted = set(names)
    for child in item:
        if _local(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _extract_link(item: ET.Element) -> str:
    """RSS 的 <link>文字 或 Atom 的 <link href=.../>。"""
    fallback = ""
    for child in item:
        if _local(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            rel = child.attrib.get("rel", "alternate")
            if rel == "alternate":
                return href.strip()
            fallback = fallback or href.strip()
        elif child.text:
            return child.text.strip()
    return fallback


def _extract_source(item: ET.Element, title: str, fallback: str) -> str:
    """盡量取出真實媒體來源名稱。"""
    for child in item:
        if _local(child.tag) == "source" and (child.text or "").strip():
            return child.text.strip()
    # Google News 標題常為「Headline - SourceName」,取尾段當來源。
    if " - " in title:
        tail = title.rsplit(" - ", 1)[-1].strip()
        if 0 < len(tail) <= 40:
            return tail
    return fallback


def _clean_title(title: str) -> str:
    """去掉 Google News 標題尾端的「 - SourceName」。"""
    title = strip_html(title).strip()
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if 0 < len(tail) <= 40 and head.strip():
            return head.strip()
    return title


def _parse_date(raw: str) -> tuple[str, datetime | None]:
    """解析 RFC822 (RSS pubDate) 或 ISO8601 (Atom),回傳 (ISO 字串, datetime)。"""
    raw = raw.strip()
    if not raw:
        return "", None
    # RFC822: "Mon, 25 May 2026 12:00:00 GMT"
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(), dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    # ISO8601: "2026-05-25T12:00:00Z"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(), dt.astimezone(timezone.utc)
    except ValueError:
        return raw, None


def _normalize_title(title: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", title.lower())


def fetch_feed(
    url: str, source_hint: str, since: datetime | None, origin: str = ""
) -> list[dict]:
    """抓取並解析單一 RSS/Atom feed。失敗時回傳空清單(不讓單一來源拖垮全局)。

    ``origin`` 標記這則新聞「從哪個管道抓到」(分類頭條/官方 feed/關鍵字),
    與媒體 ``source`` 不同,供前端顯示與判斷。
    """
    items: list[dict] = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except Exception:  # noqa: BLE001 — 網路/解析失敗都略過此來源
        return items

    # RSS 用 <item>,Atom 用 <entry>。
    entries = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    for entry in entries:
        raw_title = _child_text(entry, "title")
        title = _clean_title(raw_title)
        if not title:
            continue
        published_iso, published_dt = _parse_date(
            _child_text(entry, "pubDate", "published", "updated", "date")
        )
        if since and published_dt and published_dt < since:
            continue
        items.append(
            {
                "title": title,
                "source": _extract_source(entry, raw_title, source_hint),
                "url": _extract_link(entry),
                "summary": strip_html(
                    _child_text(entry, "description", "summary", "content")
                ),
                "published": published_iso,
                "origin": origin or source_hint,
            }
        )
    return items


def fetch_news(
    queries: list[str],
    *,
    lang: str = "zh",
    region: str = "TW",
    feeds: dict[str, str] | None = None,
    limit: int = 12,
    since_hours: int = 48,
) -> list[dict]:
    """從 Google News RSS(依 queries)+ 指定官方 feed 聚合真實新聞。

    會依標題/連結去重、依發佈時間由新到舊排序,最後取前 ``limit`` 則。
    """
    since = (
        datetime.now(timezone.utc) - timedelta(hours=since_hours)
        if since_hours and since_hours > 0
        else None
    )

    collected: list[dict] = []
    for query in queries:
        if not query.strip():
            continue
        collected += fetch_feed(
            google_news_rss_url(query, lang, region),
            "Google News",
            since,
            origin=f"關鍵字「{query.strip()}」",
        )

    for name, url in (feeds or {}).items():
        collected += fetch_feed(url, name, since, origin=name)

    # 去重:同連結或同(正規化)標題只留一則。
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[dict] = []
    for item in collected:
        url_key = item["url"].split("?")[0] if item["url"] else ""
        title_key = _normalize_title(item["title"])
        if url_key and url_key in seen_urls:
            continue
        if title_key and title_key in seen_titles:
            continue
        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        deduped.append(item)

    # 有時間的排前面(由新到舊),沒時間的排後面。
    deduped.sort(key=lambda d: d.get("published") or "", reverse=True)
    return deduped[:limit]
