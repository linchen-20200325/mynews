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
  - PUSH_ALL_DAYS                  (選填) 設 1 =非台股交易日也全量推播;預設僅推國際盤快報
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
import sys
from pathlib import Path

import gemini_client  # Gemini API 封裝 SSOT(多 Key/多模型/退避/JSON 解析)
import line_notify  # LINE 推播 SSOT(路由/訊息組建/事件去重)
import chip_calendar  # 法人籌碼:可預測賣壓事件行事曆(純規則,零網路零 AI)
import feature_aligner  # 四路特徵對齊合流 SSOT → 中央決策大腦輸入
import config  # 環境變數讀取 + 功能開關的 SSOT
import chip_fetcher  # 法人籌碼:抓證交所三大法人買賣超(事後驗證,真實數字)
import chip_signals  # 個股盯盤:個股三大法人買賣超(T86)籌碼面訊號的 SSOT
import earnings_fetcher  # 個股盯盤:抓證交所 OpenAPI 月營收(真實財報更新訊號)
import freshness  # 資料新鮮度(staleness)守門的單一真相源(SSOT,§2.4)
import futures_chip_fetcher  # 法人籌碼:抓期交所三大法人台指期留倉(外資期貨偏多/偏空)
import housing_fetcher
import index_fetcher  # 國際盤預警:抓美股指數/美股期貨真實漲跌幅
import margin_fetcher  # 融資餘額:散戶槓桿/斷頭訊號(共振偵測用)
import nav_fetcher  # 個股盯盤:ETF 淨值/折溢價(fail-loud,NAV 過期不硬算)的 SSOT
import news_fetcher
import numutil  # 數值計算的單一真相源(SSOT):pct_change / parse_number / OKU
import paths  # 檔案/目錄路徑的單一真相源(SSOT)
import tech_signals  # 個股技術面訊號(日K→均線/KD/RSI)的單一真相源(SSOT)
import tz_utils  # 台灣時區時間的單一真相源(SSOT)
import vcp_signals  # 個股盯盤:VCP 波動收縮型態買點偵測的單一真相源(SSOT)
import watchlist  # 個股盯盤清單(watchlist)的單一真相源(SSOT)
import news_analyzer      # 新聞分析工具（日期萃取/關鍵字比對/時間窗口/情感評分）SSOT
import prompt_loader      # System prompt YAML 讀取 SSOT(prompts/*.yaml)
import reversal_signals   # 中線翻轉偵測 SSOT（三大指標共振）
from prompt_builder import (
    build_analysis_user_prompt,
    build_trend_user_prompt,
    build_stock_user_prompt,
    build_us_stock_user_prompt,
    build_intl_alert_user_prompt,
    build_focus_user_prompt,
    build_stock_query_user_prompt,
    build_housing_user_prompt,
    build_housing_reg_user_prompt,
    build_news_etf_user_prompt,
    build_market_digest_prompt,
)

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

# 抓新聞用的預設關鍵字 — 單一真相源在 query_config.json(SSOT)。
# 可直接在 GitHub UI 修改 JSON;env var（同名大寫、; 分隔）優先級更高。
_QUERY_CONFIG: dict = json.loads(
    (Path(__file__).parent / "query_config.json").read_text(encoding="utf-8")
)

DEFAULT_NEWS_QUERIES       = _QUERY_CONFIG["news_queries"]
DEFAULT_NEWS_QUERIES_EN    = _QUERY_CONFIG["news_queries_en"]
DEFAULT_TREND_QUERIES      = _QUERY_CONFIG["trend_queries"]
DEFAULT_US_TREND_QUERIES   = _QUERY_CONFIG["us_trend_queries"]
DEFAULT_STOCK_QUERIES      = _QUERY_CONFIG["stock_queries"]
DEFAULT_STOCK_QUERIES_EN   = _QUERY_CONFIG["stock_queries_en"]
DEFAULT_US_STOCK_QUERIES   = _QUERY_CONFIG["us_stock_queries"]
DEFAULT_US_STOCK_QUERIES_ZH = _QUERY_CONFIG["us_stock_queries_zh"]
DEFAULT_FOCUS_TOPICS       = _QUERY_CONFIG["focus_topics"]
DEFAULT_NEWS_TOPICS        = _QUERY_CONFIG["news_topics"]
DEFAULT_TREND_TOPICS       = _QUERY_CONFIG["trend_topics"]

# 前五個章節(報告/趨勢/台股/美股/人物)新聞回溯視窗 ~6 個月。
SIX_MONTHS_HOURS = 24 * 183
# 台媒自家 RSS(直接來源,補 Google News 排名外的台灣報導);人物追蹤會「先過濾出有提到
# 該對象者」才納入,避免灌入無關新聞。抓不到的 feed 會自動略過(fetch_news 容錯)。
TW_MEDIA_FEEDS = {
    "自由時報 即時": "https://news.ltn.com.tw/rss/all.xml",
    "自由時報 財經": "https://ec.ltn.com.tw/rss/all.xml",
    "經濟日報": "https://money.udn.com/rssfeed/news/1001/5590?ch=money",
}

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

ANALYSIS_SYSTEM_PROMPT = prompt_loader.load("analysis")

TREND_SYSTEM_PROMPT = prompt_loader.load("trend")


STOCK_SYSTEM_PROMPT = prompt_loader.load("stock")


US_STOCK_SYSTEM_PROMPT = prompt_loader.load("us_stock")


INTL_ALERT_SYSTEM_PROMPT = prompt_loader.load("intl_alert")


FOCUS_TRANSLATE_SYSTEM_PROMPT = prompt_loader.load("focus_translate")


STOCK_QUERY_TRANSLATE_SYSTEM_PROMPT = prompt_loader.load("stock_query_translate")


FOCUS_SYSTEM_PROMPT = prompt_loader.load("focus")


STOCK_QUERY_SYSTEM_PROMPT = prompt_loader.load("stock_query")


NEWS_ETF_STRATEGY_SYSTEM_PROMPT = prompt_loader.load("news_etf_strategy")


HOUSING_SYSTEM_PROMPT = prompt_loader.load("housing")
HOUSING_REG_SYSTEM_PROMPT = prompt_loader.load("housing_regulation")


MASTER_DECISION_SYSTEM_PROMPT = prompt_loader.load("master_decision")


def get_master_decision(
    date: str | None = None,
    _features: dict | None = None,
) -> dict:
    """整合四路特徵後呼叫 Gemini，產出結構化市場決策 JSON。

    Parameters
    ----------
    date : str or None
        台股交易日，預設今日。
    _features : dict or None
        已建立的特徵 JSON（避免重複呼叫 feature_aligner）。

    Returns 格式：
      {date, market_regime, action_signal, confidence_score,
       decision_weights, key_drivers, risk_alert, disclaimer}
    失敗時 raise（由呼叫端決定是否 swallow）。
    """
    features = _features if _features is not None else feature_aligner.build_feature_json(date)
    result = gemini_client.call_gemini_for_json(
        system_instruction=MASTER_DECISION_SYSTEM_PROMPT,
        user_content=json.dumps(features, ensure_ascii=False, default=str),
    )
    # 基本驗證
    required = {"date", "market_regime", "action_signal", "confidence_score",
                "decision_weights", "key_drivers", "risk_alert"}
    missing = required - set(result)
    if missing:
        raise ValueError(f"Gemini master decision 缺少欄位: {missing}")
    weights = result.get("decision_weights") or {}
    total = sum(float(v) for v in weights.values())
    if not (0.98 <= total <= 1.02):
        raise ValueError(f"decision_weights 總和異常: {total:.3f}")
    return result


_REVERSAL_SYMBOLS: list[tuple[str, bool]] = [
    ("^TWII", True),   # 台股大盤
    ("^SOX",  True),   # 費城半導體指數
]


def _run_reversal_detection(today: str, log=print) -> dict | None:
    """排程 helper：對 _REVERSAL_SYMBOLS 跑翻轉偵測並歸檔，失敗靜默回 None。

    輸出結構：{"report_date": "YYYY-MM-DD", "signals": [{symbol, signal, ...}, ...]}
    """
    log("🔭 [reversal] 中線翻轉偵測（三大指標共振）...")
    try:
        signals = []
        for sym, is_mkt in _REVERSAL_SYMBOLS:
            result = reversal_signals.detect_trend_reversal(sym, is_market=is_mkt)
            signals.append(result)
            log(f"   {sym}: {result['signal']} (信心 {result['confidence']}/3)")
        doc = {
            "report_date": today,
            "as_of": tz_utils.taiwan_now().strftime("%Y-%m-%d %H:%M UTC+8"),
            "signals": signals,
        }
        save_json(paths.LATEST_REVERSAL, doc)
        save_json(paths.ARCHIVE_REVERSAL / f"{today}.json", doc)
        log(f"   ✅ 翻轉偵測完成：{len(signals)} 個標的")
        return doc
    except Exception as exc:  # noqa: BLE001
        log(f"   ⚠️  翻轉偵測失敗（不影響主流程）：{exc}")
        return None


def _run_master_decision(today: str, log=print) -> dict | None:
    """排程 helper：執行中央決策並歸檔，失敗靜默回 None（不拖垮主流程）。"""
    log("🧠 [master] 四路合流 → Gemini 中央決策...")
    try:
        features = feature_aligner.build_feature_json(today)
        decision = get_master_decision(today, _features=features)
        decision["features"] = features  # 嵌入特徵供 Streamlit 儀表板展示
        save_json(paths.LATEST_DECISION, decision)
        save_json(paths.ARCHIVE_DECISION / today / "decision.json", decision)
        signal = decision.get("action_signal", "?")
        score = decision.get("confidence_score", "?")
        log(f"   ✅ 決策完成：{signal}  信心分數：{score}")
        return decision
    except Exception as exc:  # noqa: BLE001
        log(f"   ⚠️  中央決策失敗（不影響主流程）：{exc}")
        return None


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


# ── 以下函數已移至 news_analyzer.py（CalcEngine SSOT）──────────────────────
# summarize_news_span  ← news_span
# count_keyword_mentions ← mention_window
# matches_news_keywords  ← news_matches
# expand_match_keys      ← _expand_match_keys
# extract_news_date      ← _news_date
# 呼叫請改用 news_analyzer.<函數名>


# ── format_news_block / format_quotes_block / format_house_* / build_*_user_prompt
# 已移至 prompt_builder.py（SSOT），update_data.py 頂部 from prompt_builder import 取用。

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
    """國際盤預警的最低限度結構驗證。

    正常情況報價必須是非空字典(無真實數字就不該成立);唯一例外是報價全數
    抓取失敗的「明示降級」(quotes_ok=False):允許空 quotes,僅保留新聞面研判,
    不讓 Yahoo 限流/代理故障連鎖炸掉排程 digest 與共振偵測。
    """
    if data.get("quotes_ok") is False:
        _validate_structure(data, required_dicts=("quotes", "tw_impact", "us_view"))
        return
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
        t.update(news_analyzer.count_keyword_mentions([t.get("industry", "")], news))
    news_analyzer.verify_evidence_news(data, news)  # F10:對帳佐證來源是否真的出自本次新聞
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
        s.update(news_analyzer.count_keyword_mentions([s.get("name", ""), s.get("ticker", "")], news))
    news_analyzer.verify_evidence_news(data, news)  # F10:對帳佐證來源是否真的出自本次新聞
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
        s.update(news_analyzer.count_keyword_mentions([s.get("name", ""), s.get("ticker", "")], news))
    news_analyzer.verify_evidence_news(data, news)  # F10:對帳佐證來源是否真的出自本次新聞
    return data


def build_intl_alert(today: str, *, quotes: dict | None = None) -> dict:
    """國際盤預警:真實指數/期貨報價(算大跌)+ 美韓新聞 → Gemini 解讀利空與台股影響。

    數字一律取自 index_fetcher 的真實報價(quotes/drops),Gemini 只負責文字研判(利空原因、
    對台股影響),不得竄改數字。可傳入既抓好的 quotes(供前端兩步流程重用,免重抓)。
    報價全數抓取失敗時降級為空報價續跑(quotes_ok=False),不讓 Yahoo 限流/代理故障
    炸掉整個國際盤預警(排程 digest / 共振偵測連鎖失效的單點)。
    """
    quotes_doc = quotes
    if not quotes_doc:
        try:
            quotes_doc = index_fetcher.fetch_index_quotes(log=print)
        except Exception as exc:  # noqa: BLE001 — 全失敗 → 降級空報價,只留新聞面研判
            print(f"  警告: 指數報價全數抓取失敗,以空報價降級續跑:{exc}", file=sys.stderr)
            quotes_doc = {"as_of": "—", "threshold": index_fetcher.drop_threshold(),
                          "quotes": {}}
    qmap = quotes_doc.get("quotes", {})

    # 台指期夜盤(期交所即時):台股自身對隔夜的定價,屬最直接的【盤前即時】訊號。
    # 走 NAS 代理抓;失敗(沙箱無網路/期交所擋境外/代理不通)只略過,不影響其他報價與 LINE。
    # 欄位一律 .get + 型別檢查:來源改版缺欄/回 None 時只略過夜盤,不拖垮整段。
    try:
        import taifex_night_fetcher
        night = taifex_night_fetcher.fetch_night_quote(log=print)
        night_pct = night.get("change_pct") if isinstance(night, dict) else None
        if isinstance(night_pct, (int, float)):
            qmap[night.get("symbol", "TXF-NIGHT")] = {
                "name": night.get("name", "台指期夜盤"),
                "group": night.get("group", "台股期貨"),
                "lead_type": night.get("lead_type", "盤前即時"),
                "last": night.get("last"),
                "prev": night.get("prev"),
                "change_pct": night_pct,
                "is_drop": night_pct
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

    # 期現背離偵測（純程式計算，非 AI；^SOX 定案 vs NQ=F/ES=F 即時）
    divergence = index_fetcher.detect_spot_futures_divergence(
        qmap, threshold=quotes_doc.get("threshold", index_fetcher.DEFAULT_DROP_THRESHOLD)
    )
    if divergence.get("signal") != "normal":
        print(f"  期現背離:{divergence['signal']} — {divergence.get('description', '')}")

    news = fetch_intl_alert_news()
    # Gemini 只做文字研判(利空原因/對台股影響);失敗(如配額 429)不得拖垮
    # 真實報價與大跌偵測 → 包成容錯:AI 掛了仍保留報價/大跌/LINE 預警,只把原因留白。
    try:
        gemini = gemini_client.call_gemini_for_json(
            INTL_ALERT_SYSTEM_PROMPT,
            build_intl_alert_user_prompt(quotes_doc, news, today, divergence=divergence),
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
    if not qmap:
        summary = "⚠️ 本次未取得任何即時報價(來源/代理暫時不可用),大跌偵測不可用;" + (summary or "以下僅新聞面研判。")
    result = {
        "report_date": today,
        "as_of": quotes_doc.get("as_of", ""),
        "threshold": quotes_doc.get("threshold", index_fetcher.DEFAULT_DROP_THRESHOLD),
        "quotes": qmap,                       # 真實報價(唯一數字來源)
        "quotes_ok": bool(qmap),              # 報價是否取得(False=降級,僅新聞面)
        "drops": drops,                       # 真實大跌清單(程式算)
        "alert_level": gemini.get("alert_level") or ("警戒" if drops else "平靜"),
        "summary": summary,
        "interpretation": gemini.get("interpretation", []),
        "us_view": us_view,                   # 對美股整體看法(平靜日也有)
        "tw_impact": tw_impact,
        "futures_divergence": divergence,     # 期現背離偵測(程式算；signal/description/sox_pct/futures_pct)
        "ai_ok": ai_ok,                       # AI 解讀是否成功(供前端/LINE 標註「原因待補」)
        "upcoming_events": upcoming_events,    # 可預測法人賣壓事件(行事曆)
        "raw_news": news,
    }
    news_analyzer.verify_evidence_news(result, news)  # F10:①佐證來源對帳(補齊唯一漏對帳的報告;render_intl_alert 確有渲染 evidence_news)
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
    data.update(news_analyzer.summarize_news_span(news))
    data.update(news_analyzer.count_keyword_mentions(_uniq_queries([term_zh, query_en, ticker]), news))
    news_analyzer.verify_evidence_news(data, news)  # F10:對帳佐證來源是否真的出自本次新聞
    return data


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


MARKET_DIGEST_SYSTEM_PROMPT = prompt_loader.load("market_digest")


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
    data.update(news_analyzer.summarize_news_span(news))
    for s in data["stocks"]:
        s.update(news_analyzer.count_keyword_mentions([s.get("name", ""), s.get("ticker", "")], news))
    news_analyzer.verify_evidence_news(data, news)  # F10:對帳佐證來源是否真的出自本次新聞
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
    news_analyzer.verify_evidence_news(data, news)  # F10:對帳佐證來源是否真的出自本次新聞
    return data


def fetch_housing_reg_news() -> list[dict]:
    """抓台灣房產法規相關新聞（每月觸發一次）。"""
    queries = [
        "平均地權條例", "囤房稅", "房地合一稅", "不動產法規",
        "新青安房貸", "央行信用管制", "預售屋換約", "實價登錄",
        "房屋稅 修法", "土地稅 改革",
    ]
    return news_fetcher.fetch_news(
        queries=queries,
        lang="zh", region="TW",
        limit=20,
        since_hours=24 * 35,  # 回溯 35 天確保月更不漏
    )


def get_housing_regulation_analysis(news: list[dict], today: str) -> dict:
    """Gemini 讀房產法規新聞 → 整理各法規狀態與對買方影響（月報格式）。"""
    data = gemini_client.call_gemini_for_json(
        HOUSING_REG_SYSTEM_PROMPT, build_housing_reg_user_prompt(news, today)
    )
    data.setdefault("report_date", today)
    data.setdefault("trend", "持平")
    data.setdefault("summary", "")
    data.setdefault("regulations", [])
    data.setdefault("evidence_news", [])
    news_analyzer.verify_evidence_news(data, news)  # F10:對帳佐證來源是否真的出自本次新聞
    return data


# ---------------------------------------------------------------------------
# 新聞抓取設定
# ---------------------------------------------------------------------------

def parse_queries(env_name: str, default: list[str]) -> list[str]:
    raw = config.env_str(env_name, "")
    queries = [q.strip() for q in raw.split(";") if q.strip()]
    return queries or default


def parse_topics(env_name: str, default: list[str]) -> list[str]:
    raw = config.env_str(env_name, "").replace(",", ";")
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
        limit=config.env_int("NEWS_MAX", DEFAULT_NEWS_MAX),
        since_hours=config.env_int("NEWS_SINCE_HOURS", SIX_MONTHS_HOURS),
    )


def fetch_trend_news() -> list[dict]:
    """抓產業趨勢新聞:台灣(中文)+ 美國(英文)兩邊都抓,讓趨勢雷達含到美股。"""
    lang = config.env_str("NEWS_LANG", "zh")
    region = config.env_str("NEWS_REGION", "TW")
    queries = parse_queries("TREND_QUERIES", DEFAULT_TREND_QUERIES)
    topics = parse_topics("TREND_TOPICS", DEFAULT_TREND_TOPICS)
    feeds = section_feeds(topics, lang, region)
    limit = config.env_int("NEWS_MAX", DEFAULT_NEWS_MAX)
    since = config.env_int("NEWS_SINCE_HOURS", SIX_MONTHS_HOURS)
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
        limit=config.env_int("STOCK_MAX", 60),
        since_hours=config.env_int("STOCK_SINCE_HOURS", SIX_MONTHS_HOURS),
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
        limit=config.env_int("US_STOCK_MAX", 40),
        since_hours=config.env_int("US_STOCK_SINCE_HOURS", SIX_MONTHS_HOURS),
    )


DEFAULT_INTL_ALERT_QUERIES = _QUERY_CONFIG["intl_alert_queries"]


def fetch_intl_alert_news() -> list[dict]:
    """抓國際盤預警用新聞:美股財經頭條 + 地緣政治 + 重大政策衝擊英文關鍵字,輔以台媒/BBC 中文角度。"""
    en_queries = parse_queries("INTL_ALERT_QUERIES", DEFAULT_INTL_ALERT_QUERIES)
    en_feeds = section_feeds(["BUSINESS", "WORLD"], "en", "US")
    zh_feed_keys = ["中央社 財經", "中央社 國際", "BBC 中文"]
    zh_feeds = {k: news_fetcher.CREDIBLE_FEEDS[k] for k in zh_feed_keys if k in news_fetcher.CREDIBLE_FEEDS}
    return fetch_bilingual_news(
        zh_queries=parse_queries("INTL_ALERT_QUERIES_ZH", _QUERY_CONFIG["intl_alert_queries_zh"]),
        en_queries=en_queries,
        zh_feeds=zh_feeds,
        en_feeds=en_feeds,
        limit=config.env_int("INTL_ALERT_MAX", 60),
        since_hours=config.env_int("INTL_ALERT_SINCE_HOURS", 72),
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

    since_hours = config.env_int("FOCUS_SINCE_HOURS", SIX_MONTHS_HOURS)
    # 1) + 2) 關鍵字雙語檢索
    keyword_news = fetch_bilingual_news(
        zh_queries=zh_terms,
        en_queries=en_queries,
        zh_feeds=None,
        en_feeds=None,
        limit=config.env_int("FOCUS_MAX", 50),
        since_hours=since_hours,
    )
    # 3) 台媒整站 RSS → 過濾出有提到該對象者(用中文名稱/別名比對)
    site_news = news_fetcher.fetch_news(
        [], lang="zh", region="TW", feeds=TW_MEDIA_FEEDS,
        limit=config.env_int("FOCUS_SITE_MAX", 200), since_hours=since_hours,
    )
    site_hits = [n for n in site_news if zh_terms and news_analyzer.matches_news_keywords(zh_terms, n)]
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
        limit=config.env_int("HOUSING_MAX", 18),
        since_hours=config.env_int("HOUSING_SINCE_HOURS", 72),
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
    paths.atomic_write_text(path, payload)  # F7:原子寫入(temp + os.replace),防半寫/併發損毀狀態競態
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
    raw = config.env_str("REPORT_TOPICS", "")
    topics = [t.strip() for t in raw.split(";") if t.strip()]
    if topics:
        return topics
    return [config.env_str("REPORT_TOPIC") or DEFAULT_TOPIC]


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
        "news_span": news_analyzer.summarize_news_span(news),
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

WATCH_SYSTEM_PROMPT = prompt_loader.load("watch")



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
                limit=config.env_int("WATCH_NEWS_MAX", 8),
                since_hours=config.env_int("WATCH_SINCE_HOURS", 96),
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

    # 2.7) ETF 淨值/折溢價(只對 00 開頭 ETF);fail-loud:NAV 過期/抓不到只標記不硬算
    nav_lines: dict[str, str] = {}
    try:
        nav_lines = nav_fetcher.nav_lines_for(stocks, log=print)
    except Exception as exc:  # noqa: BLE001 — NAV 整批失敗不影響消息面/技術面/籌碼面/財報推播
        print(f"  警告: ETF 淨值/折溢價整批計算失敗:{exc}", file=sys.stderr)

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
        today, summaries, new_revenue, tech_lines, chip_lines, vcp_lines, new_eps,
        nav_lines)
    line_notify._push_line_text(msg, token=config.env_required("LINE_WATCH_TOKEN"), to=to)
    print(
        f"  · 推給 {to[:6]}…:消息面 {len(summaries)} 檔、技術面 {len(tech_lines)} 檔、"
        f"籌碼面 {len(chip_lines)} 檔、VCP {len(vcp_lines)} 檔、"
        f"ETF 淨值 {len(nav_lines)} 檔、月營收 {len(new_revenue)} 筆、季報 EPS {len(new_eps)} 筆"
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
    fresh = _push_watch_for(today, stocks, to=config.env_required("LINE_WATCH_TO"), pushed=pushed)
    if fresh:
        save_pushed_revenue(pushed + fresh)
    print("  ⑤ 個股盯盤已推。")


def _schedule_guard(now_tw, today: str) -> bool:
    """排程前哨守門。若應略過本次跑動回 True。"""
    floor = config.env_str("EARLIEST_TW_HHMM", "0530").strip()
    try:
        fh, fm = int(floor[:2]), int(floor[2:])
        if not (len(floor) == 4 and floor.isdigit() and 0 <= fh < 24 and 0 <= fm < 60):
            raise ValueError(f"格式應為 HHMM(0000–2359),得到 {floor!r}")
    except (ValueError, IndexError) as exc:
        print(f"  警告: EARLIEST_TW_HHMM 無效({exc}),回退預設 05:30。", file=sys.stderr)
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


def _run_chip_data(today: str) -> tuple[dict | None, dict | None, dict | None]:
    """D3. 法人籌碼(三大法人 + 融資餘額 + 台指期留倉)。回傳 (chip, margin, fut_chip)。"""
    if not config.chip_enabled():
        print("[6/8] ENABLE_CHIP=0,略過法人籌碼。")
        return None, None, None
    print("[6/8] 抓證交所三大法人買賣超(近 N 日,事後驗證真實籌碼)...")
    chip = margin = fut_chip = None
    try:
        fetched = chip_fetcher.fetch_chip_flow(
            days=config.env_int("CHIP_DAYS", 10), log=print)
        # §2.4 過期守門:歸屬日落後過久→raise→留 chip=None→共振自動略過此力量
        latest_date = fetched["days"][0]["date"] if fetched.get("days") else None
        freshness.ensure_fresh(latest_date, CHIP_STALE_DAYS, "三大法人籌碼")
        chip = fetched
        save_json(OUTPUT_CHIP, chip)
        if chip.get("days"):
            latest = chip["days"][0]
            save_json(CHIP_ARCHIVE_DIR / f"{latest.get('date', today)}.json", chip)
            # 欄位 .get + 型別檢查:來源改版缺單欄時只少顯示一欄,不讓整段存檔/歸檔被 except 放棄
            foreign, trust = latest.get("foreign"), latest.get("trust")
            f_txt = f"{foreign/OKU:+.0f}億" if isinstance(foreign, (int, float)) else "—"
            t_txt = f"{trust/OKU:+.0f}億" if isinstance(trust, (int, float)) else "—"
            print(f"  法人籌碼完成,最新 {latest.get('date', '—')}:"
                  f"外資 {f_txt}、投信 {t_txt}。")
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
            net_oi = fut_chip.get("foreign_net_oi")
            oi_txt = f"{net_oi:+,}口" if isinstance(net_oi, (int, float)) else "—"
            print(f"  台指期留倉完成,外資{fut_chip.get('stance', '中性')}(淨{oi_txt})。")
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
    trading_day: bool = True,
) -> None:
    """LINE 推播(依序:① 國際盤快報 → ② 共振預警 → ③ 法人事件預告 → ④ 戰略報告)。

    trading_day=False(台股非交易日):僅推 ①、②③④ 靜音——台股類預警在休市日
    無行動意義;國際盤快報保留(如週六早上=美股週五收盤,仍具閱讀價值)。
    """
    if not (config.env_str("LINE_CHANNEL_ACCESS_TOKEN") and config.env_str("LINE_TO")):
        return
    muted = set(watchlist.muted_types(watchlist.load()))  # F5:全域靜音類別(②③④;①心跳載體不受理)
    if muted:
        print(f"  🔕 靜音中,本次跳過:{'、'.join(sorted(muted))}")
    print("推送 LINE 通知(依序:國際盤大跌→共振→事件預告→戰略報告)...")
    lead = line_notify.lead_market_drops(intl) if intl else []
    if intl and config.intl_alert_line_enabled():
        try:
            gap_note = line_notify.heartbeat_gap_note(today)
            line_notify.notify_line_intl_alert(intl, gap_note)
            line_notify.save_push_heartbeat(today)
            if line_notify.ping_heartbeat_monitor():
                print("  ✓ 外部心跳已通知(dead-man's-switch 存活 ping)。")
            tag = f"大跌 {len(lead)} 項" if lead else "平靜快報"
            if gap_note:
                print(f"  ⚠️ 推播心跳自檢:{gap_note}")
            print(f"  ① 國際盤快報已推({tag})。")
        except Exception as exc:  # noqa: BLE001
            print(f"  警告: 國際盤 LINE 推播失敗:{exc}", file=sys.stderr)
    if not trading_day:
        print("  今日非台股交易日:②共振/③事件預告/④戰略報告靜音(設 PUSH_ALL_DAYS=1 可全推)。")
        return
    if conf and conf.get("triggered") and config.confluence_line_enabled() and "confluence" not in muted:
        try:
            line_notify.notify_line_confluence(conf, today)
            print("  ② 多重賣壓共振預警已推。")
        except Exception as exc:  # noqa: BLE001
            print(f"  警告: 共振 LINE 推播失敗:{exc}", file=sys.stderr)
    if intl and config.chip_line_enabled() and "chip_event" not in muted:
        try:
            pushed = line_notify.load_pushed_events()
            due = chip_calendar.pick_new_pushable(intl.get("upcoming_events", []), pushed)
            if due:
                line_notify.notify_line_chip_events(due, today)
                line_notify.save_pushed_events(pushed + [e["id"] for e in due])
                print(f"  ③ 法人事件預告已推({len(due)} 項)。")
        except Exception as exc:  # noqa: BLE001
            print(f"  警告: 法人事件 LINE 預告推播失敗:{exc}", file=sys.stderr)
    if "report" in muted:
        print("  ④ 戰略報告已靜音,跳過。")
    elif report:
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

    for _line in config.summary_lines():  # F8:設定總表開機自檢(只印狀態,不印金鑰值)
        print(_line)

    # 以台灣時區(UTC+8,無日光節約)定義「今天」
    now_tw = tz_utils.taiwan_now()
    today = now_tw.strftime("%Y-%m-%d")

    if config.env_str("GITHUB_EVENT_NAME") == "schedule" and _schedule_guard(now_tw, today):
        return 0

    # 台股交易日守門:非交易日(週六日/TW_HOLIDAYS)報告照產,但 LINE 僅推國際盤快報。
    trading_day = (tz_utils.is_tw_trading_day(now_tw.date())
                   or config.env_bool("PUSH_ALL_DAYS", False))
    if not trading_day:
        print(f"今日({today})非台股交易日:LINE 僅推國際盤快報,個股盯盤暫停"
              "(設 PUSH_ALL_DAYS=1 可恢復全量)。")

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
        _run_master_decision(today)
        _run_reversal_detection(today)
        if report:
            print(
                f"資料更新成功!新聞 {len(report.get('raw_news', []))} 則、"
                f"白話文來源:{report.get('dictionary_source', '—')}。"
            )
        else:
            print("資料更新部分完成(主報告降級,國際盤/籌碼/共振仍已產出)。")
        _run_line_push(report, intl, chip, fut_chip, conf, today, trading_day=trading_day)
        if config.watch_enabled():
            if not trading_day:
                print("個股盯盤:非台股交易日,暫停推播(下個交易日恢復)。")
            else:
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
