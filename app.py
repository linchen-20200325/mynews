"""Streamlit 前端 — 全球政經戰略報告 + 趨勢雷達。

側邊欄可切換報告類型(戰略報告 / 趨勢雷達),並瀏覽歷史存檔。
本地執行: streamlit run app.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

import etf_fetcher  # 透過代理抓 MoneyDJ 成分股建庫
import etf_holdings  # ETF 持股反查(純設定檔,不呼叫 AI)
import price_fetcher  # 透過代理抓台股收盤價(供價位篩選)
import proxy_helper  # NAS 中繼站:設定讀取 + 連線健檢
import update_data  # 重用爬蟲 + Gemini 管線,讓網頁可即時抓新聞/產報告

REPORT_PATH = Path("latest_report.json")
ARCHIVE_DIR = Path("data/reports")
TRENDS_PATH = Path("latest_trends.json")
TRENDS_ARCHIVE_DIR = Path("data/trends")
STOCKS_PATH = Path("latest_stocks.json")
STOCKS_ARCHIVE_DIR = Path("data/stocks")
ETF_HOLDINGS_PATH = Path("etf_holdings.json")

SENTIMENT_STYLE = {
    "利多": ("🟢", "success"),
    "利空": ("🔴", "error"),
    "觀望": ("🟡", "info"),
}

ANALYSIS_SECTIONS = [
    ("geo_military", "🛰️ 一、地緣政治與軍事戰略"),
    ("supply_chain", "🛢️ 二、原物料與供應鏈傳導"),
    ("macro_economy", "💵 三、總體經濟與貨幣定價"),
    ("blind_spots_and_kpi", "🌏 四、全球大局觀與領先指標"),
]

SIGNAL_LABELS = [
    ("funding", "💰 資金流向"),
    ("hiring", "🧑‍💻 徵才動能"),
    ("policy", "📜 政策動向"),
    ("technology", "🔬 技術動能"),
]


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def list_archive(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted((p.name for p in directory.glob("*.json")), reverse=True)


def load_trend_history(archive_dir: Path) -> "pd.DataFrame | None":
    """彙整所有歷史趨勢存檔成『日期 × 產業』的熱度表,供折線圖使用。"""
    rows = []
    for p in sorted(archive_dir.glob("*.json")):
        data = load_json(p)
        if not data:
            continue
        date = data.get("report_date") or p.stem
        for t in data.get("trends", []):
            industry = t.get("industry")
            heat = t.get("heat_score")
            if industry and isinstance(heat, (int, float)):
                rows.append({"date": date, "industry": industry, "heat": heat})
    if not rows:
        return None
    df = pd.DataFrame(rows)
    pivot = df.pivot_table(index="date", columns="industry", values="heat", aggfunc="mean")
    return pivot.sort_index()


def pick_report(latest_path: Path, archive_dir: Path):
    """側邊欄報告選擇器,回傳選定的 dict。"""
    archive = list_archive(archive_dir)
    choice = st.sidebar.selectbox("選擇日期", ["最新 (latest)"] + archive)
    if archive:
        st.sidebar.caption(f"歷史存檔:{len(archive)} 份")
    if choice == "最新 (latest)":
        return load_json(latest_path)
    return load_json(archive_dir / choice)


# ---------------------------------------------------------------------------
# 新聞來源說明 + 即時抓取(免等每日排程)
# ---------------------------------------------------------------------------

NEWS_SOURCE_CAPTION = (
    "新聞來源(繁中、聚焦國際政治/軍事/財經):**Google News 世界＋財經分類頭條**"
    "(動態,抓當下實際大事)＋ **聚焦關鍵字**(聯準會、利率通膨、股匯債、地緣軍事)"
    "＋ 中央社/BBC 中文/DW 等官方 feed。只取開放 feed 的標題/來源/連結/摘要,不爬付費牆全文。"
)


def get_topic() -> str:
    return os.environ.get("REPORT_TOPIC") or update_data.DEFAULT_TOPIC


def available_secret_names() -> list[str]:
    """列出目前 Streamlit Secrets 內的頂層名稱(只回名稱,不回值),供除錯。"""
    try:
        return [str(k) for k in st.secrets.keys()]
    except Exception:  # noqa: BLE001 — 沒設定 secrets 或解析失敗
        return []


def _collect_keys_from_secrets() -> list[str]:
    """從 Streamlit Secrets 蒐集 Gemini 金鑰(支援多種命名/區段/陣列)。"""
    keys: list[str] = []

    def add(value) -> None:
        if isinstance(value, str) and value.strip():
            keys.append(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    keys.append(item.strip())

    try:
        secrets = st.secrets
    except Exception:  # noqa: BLE001
        return keys

    # 頂層:名稱含 GEMINI 與 KEY 的都收(涵蓋 GEMINI_API_KEY / GEMINI_API_KEYS / _1.._n)
    try:
        for name in secrets.keys():
            upper = str(name).upper()
            if "GEMINI" in upper and "KEY" in upper:
                add(secrets[name])
    except Exception:  # noqa: BLE001
        pass

    # 區段 [gemini] 內的所有值
    try:
        section = secrets["gemini"]
        for name in section.keys():
            add(section[name])
    except Exception:  # noqa: BLE001
        pass

    return keys


def ensure_gemini_key() -> bool:
    """確保環境中有 Gemini 金鑰:先看環境變數,再從 Streamlit Secrets 補上。"""
    if update_data.get_gemini_keys():
        return True
    keys = _collect_keys_from_secrets()
    if keys:
        os.environ["GEMINI_API_KEY"] = ",".join(keys)
    return bool(update_data.get_gemini_keys())


def render_key_hint() -> None:
    """金鑰讀不到時的診斷說明:列出目前 Secrets 名稱,幫使用者比對。"""
    st.caption(
        "ℹ️ 尚未偵測到金鑰。看新聞不需金鑰;要用 Gemini,請到 Streamlit Cloud → "
        "App settings → Secrets 加上(名稱需完全是 `GEMINI_API_KEY`):"
    )
    st.code(
        'GEMINI_API_KEY = "你的金鑰"\n\n# 多把金鑰二選一寫法:\n'
        '# GEMINI_API_KEY = "key1,key2"\n# 或\n# GEMINI_API_KEY = ["key1", "key2"]',
        language="toml",
    )
    names = available_secret_names()
    if names:
        st.caption("🔎 目前 Secrets 讀到的名稱:" + "、".join(f"`{n}`" for n in names))
    else:
        st.caption(
            "🔎 目前讀不到任何 Secrets — 可能尚未按 Save、或 TOML 格式有誤"
            "(常見:重複鍵、缺引號、貼到多餘符號)。"
        )


def ensure_proxy() -> str | None:
    """取得 NAS 中繼站 PROXY_URL(環境變數或 Streamlit Secrets),並同步到環境變數。

    統一走 proxy_helper.get_proxy_config(支援新格式 PROXY_URL 與舊格式 [proxy]),
    再把結果寫回環境變數,供 etf_fetcher 等子流程使用。
    """
    cfg = proxy_helper.get_proxy_config()
    url = cfg["http"] if cfg else None
    if url:
        os.environ["PROXY_URL"] = url
    return url


def render_proxy_status() -> None:
    """側邊欄:顯示 NAS 中繼站狀態 + 提供『檢驗中繼站是否可以使用』按鈕。"""
    cfg = proxy_helper.get_proxy_config()
    if cfg:
        st.caption(f"🛰️ NAS 中繼站:✅ 已設定（{proxy_helper.mask_endpoint(cfg['http'])}）")
    else:
        st.caption("🛰️ NAS 中繼站:⚠️ 未設定（ETF 成分股將直連 MoneyDJ,境外 IP 可能被擋）")

    if st.button("🧪 檢驗中繼站連線", use_container_width=True, key="btn_proxy_check"):
        with st.spinner("正在測試中繼站連線…"):
            res = proxy_helper.check_proxy()
        (st.success if res["ok"] else st.error)(res["detail"])
        if not cfg:
            st.caption(
                "設定方式:Streamlit Cloud → App settings → Secrets 加上\n"
            )
            st.code('PROXY_URL = "http://帳號:密碼@yourname.synology.me:3128"', language="toml")


def render_etf_crawl_panel() -> None:
    """透過 NAS 代理抓 MoneyDJ 成分股,建立/更新反查資料庫。"""
    with st.container(border=True):
        st.markdown("#### 🛰️ 透過 NAS 代理更新成分股(MoneyDJ)")
        st.caption("經由你設定的 PROXY_URL 代理抓 MoneyDJ 真實成分股,建立『個股→ETF』反查庫。")
        proxy = ensure_proxy()
        if not proxy:
            st.warning("未偵測到 PROXY_URL。請在 Streamlit Secrets 設定後再試。")
        if st.button("🧪 先檢驗中繼站是否可以使用", use_container_width=True, key="btn_etf_proxy_check"):
            with st.spinner("正在測試中繼站連線…"):
                res = proxy_helper.check_proxy()
            (st.success if res["ok"] else st.error)(res["detail"])
        if st.button(
            "🔄 立即抓取 / 更新 ETF 成分股資料庫",
            use_container_width=True,
            disabled=not proxy,
        ):
            with st.spinner("透過代理抓 MoneyDJ 成分股中…(視 ETF 檔數約數十秒)"):
                logs: list[str] = []
                try:
                    data = etf_fetcher.crawl(proxy=proxy, log=logs.append)
                    st.session_state["etf_data_live"] = data
                    st.success(f"完成!目前資料庫共 {len(data.get('etfs', {}))} 檔 ETF。")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"抓取失敗:{exc}")
                if logs:
                    with st.expander("📋 抓取明細"):
                        st.code("\n".join(logs))
        if st.session_state.get("etf_data_live"):
            st.download_button(
                "⬇️ 下載 etf_holdings.json(可 commit 回 repo 永久保存)",
                data=json.dumps(st.session_state["etf_data_live"], ensure_ascii=False, indent=2),
                file_name="etf_holdings.json",
                mime="application/json",
            )


def render_etf_add_panel() -> None:
    """網頁新增 ETF 到來源清單(含重複檢查)+ 下載清單。"""
    # 本回合若已新增,沿用 session 內的清單,否則從檔案載入
    sources = st.session_state.get("etf_sources_live") or etf_fetcher.load_sources()
    etfs = sources.get("moneydj", {}).get("etfs", {})

    with st.expander(f"➕ 新增 ETF 到清單（目前 {len(etfs)} 檔)", expanded=False):
        st.caption("只要輸入代號(可一次貼多檔,以逗號/空白/換行分隔);名稱會透過代理自動抓 MoneyDJ。"
                   "會自動檢查是否重複。新增後請按上方「🔄 立即抓取」更新成分股。")
        with st.form("add_etf_form", clear_on_submit=True):
            codes_text = st.text_area(
                "ETF 代號(可多筆)", placeholder="例:00940, 00982A 00713\n00878",
                height=80,
            )
            submitted = st.form_submit_button("加入清單", use_container_width=True)
        if submitted:
            if not codes_text.strip():
                st.warning("請先輸入至少一個 ETF 代號。")
            else:
                proxy = ensure_proxy()
                proxies = proxy_helper.get_proxy_config() if proxy else None
                with st.spinner("加入清單並抓取名稱中…"):
                    sources, msgs = etf_fetcher.add_etfs_bulk(codes_text, sources, proxies)
                st.session_state["etf_sources_live"] = sources
                try:
                    etf_fetcher.save_sources(sources)
                    saved = "(已寫入 etf_sources.json)"
                except Exception:  # noqa: BLE001 — 雲端唯讀
                    saved = "(雲端唯讀,請用下方按鈕下載清單並 commit 回 repo)"
                if not proxy:
                    st.info("未設定 PROXY_URL,名稱暫時留空;設定代理後重抓即可補上名稱。")
                st.success("處理完成 " + saved)
                st.write("\n".join(f"- {m}" for m in msgs))
                etfs = sources["moneydj"]["etfs"]

        # 移除 ETF
        st.markdown("**🗑️ 移除 ETF**")
        if etfs:
            to_remove = st.multiselect(
                "選擇要從清單移除的 ETF(可多選)",
                options=list(etfs.keys()),
                format_func=lambda c: f"{c} {etfs[c].get('name', '')}".strip(),
                key="etf_remove_select",
            )
            if st.button("移除選取的 ETF", use_container_width=True, disabled=not to_remove):
                rmsgs: list[str] = []
                for code in to_remove:
                    ok, msg, sources = etf_fetcher.remove_etf(code, sources)
                    rmsgs.append(("✅ " if ok else "⚠️ ") + msg)
                st.session_state["etf_sources_live"] = sources
                try:
                    etf_fetcher.save_sources(sources)
                    rsaved = "(已寫入 etf_sources.json)"
                except Exception:  # noqa: BLE001 — 雲端唯讀
                    rsaved = "(雲端唯讀,請用下方按鈕下載清單並 commit 回 repo)"
                st.success("處理完成 " + rsaved)
                st.write("\n".join(f"- {m}" for m in rmsgs))
                etfs = sources["moneydj"]["etfs"]
        else:
            st.caption("清單目前是空的。")

        # 目前清單一覽 + 下載
        st.caption(f"目前清單({len(etfs)} 檔):" + "、".join(f"{c} {i.get('name','')}".strip() for c, i in etfs.items()))
        st.download_button(
            "⬇️ 下載 etf_sources.json(清單)",
            data=json.dumps(sources, ensure_ascii=False, indent=2),
            file_name="etf_sources.json",
            mime="application/json",
        )


def render_news_cards(news: list[dict]) -> None:
    for item in news:
        title = item.get("title", "(無標題)")
        source = item.get("source", "")
        url = item.get("url", "")
        header = f"**{title}**" + (f" — _{source}_" if source else "")
        with st.container(border=True):
            st.markdown(header)
            meta = []
            if item.get("origin"):
                meta.append(f"📡 來源管道:{item['origin']}")
            if item.get("published"):
                meta.append(f"🕒 {item['published']}")
            if meta:
                st.caption("　｜　".join(meta))
            st.write(item.get("summary", ""))
            if url:
                st.markdown(f"[原文連結]({url})")


def render_live_panel() -> None:
    """第一步:只負責『抓新聞』,結果存進 session_state(Gemini 分析另由按鈕觸發)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時抓取(免等每日排程)")
        st.caption(NEWS_SOURCE_CAPTION)
        st.caption("流程:① 先抓新聞 → ② 看過後,再按 Gemini 按鈕做分析+白話文。")

        if st.button("🔄 ① 立即抓取最新新聞", use_container_width=True):
            with st.spinner("抓取真實外電中…"):
                try:
                    st.session_state["live_news"] = update_data.fetch_macro_news(get_topic())
                    st.session_state.pop("live_report", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["live_news"] = []
                    st.error(f"抓取失敗:{exc}")


def generate_live_report() -> None:
    """第二步:對『已抓到的新聞』請 Gemini 做四維度分析 + 白話文。"""
    news = st.session_state.get("live_news", [])
    topic = get_topic()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    analysis = update_data.get_macro_analysis(news, topic, today)
    st.session_state["live_report"] = {
        "report_date": today,
        "topic": topic,
        "raw_news": news,
        "strategic_analysis": analysis["strategic_analysis"],
        "laymans_dictionary": analysis["laymans_dictionary"],
        "dictionary_source": "gemini",
    }
    st.session_state.pop("live_news", None)


def render_trend_live_panel() -> None:
    """趨勢雷達第一步:只抓產業新聞(排名打分另由 Gemini 按鈕觸發)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時產生(免等每日排程)")
        st.caption(NEWS_SOURCE_CAPTION)
        st.caption("流程:① 先抓產業新聞 → ② 看過後,再按 Gemini 按鈕排名打分。")

        if st.button("🔄 ① 立即抓取產業新聞", use_container_width=True):
            with st.spinner("抓取產業新聞中…"):
                try:
                    st.session_state["live_trend_news"] = update_data.fetch_trend_news()
                    st.session_state.pop("live_trends", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["live_trend_news"] = []
                    st.error(f"抓取失敗:{exc}")


def generate_live_trends() -> None:
    """趨勢雷達第二步:對『已抓到的產業新聞』請 Gemini 排名打分。"""
    news = st.session_state.get("live_trend_news", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st.session_state["live_trends"] = update_data.get_trend_radar(news, today)
    st.session_state.pop("live_trend_news", None)


def render_stock_live_panel() -> None:
    """台股觀察第一步:只抓台灣財經新聞(整理另由 Gemini 按鈕觸發)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時產生(免等每日排程)")
        st.caption(
            "從台灣財經新聞統計被提到最多次的台股標的,分利多/利空/觀望,"
            "並歸納未來趨勢與夕陽產業。流程:① 先抓財經新聞 → ② 看過後再按 Gemini 整理。"
        )
        if st.button("🔄 ① 立即抓取台灣財經新聞", use_container_width=True):
            with st.spinner("抓取台灣財經新聞中…"):
                try:
                    st.session_state["live_stock_news"] = update_data.fetch_stock_news()
                    st.session_state.pop("live_stocks", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["live_stock_news"] = []
                    st.error(f"抓取失敗:{exc}")


def generate_live_stocks() -> None:
    """台股觀察第二步:對『已抓到的財經新聞』請 Gemini 整理台股標的。"""
    news = st.session_state.get("live_stock_news", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st.session_state["live_stocks"] = update_data.get_stock_picks(news, today)
    st.session_state.pop("live_stock_news", None)


def render_stocks(data: dict) -> None:
    st.metric("資料日期", data.get("report_date", "—"))
    if data.get("summary"):
        st.info(data["summary"])
    st.caption("依新聞『被提及次數』排序;標的分利多/利空/觀望。⚠️ 僅為新聞整理,非投資建議。")

    stocks = data.get("stocks", [])
    if not stocks:
        st.info("本次未整理出台股標的。")
        return

    # 交叉參照:每檔個股被幾檔 ETF 持有(來自 etf_holdings.json)
    holdings = etf_holdings.load_holdings(ETF_HOLDINGS_PATH)
    etf_counts = etf_holdings.etf_count_map(holdings) if holdings else {}

    # 總表(新聞提及次數 + ETF 持有檔數,兩個訊號一起看)
    st.subheader("📋 台股標的總表(新聞提及 × ETF 持有)")
    st.caption("被很多 ETF 持有 ＋ 新聞偏利多 = 相對更受關注。ETF 檔數來自 etf_holdings.json。")
    st.dataframe(
        [
            {
                "標的": s.get("name", ""),
                "代號": s.get("ticker", ""),
                "產業": s.get("sector", ""),
                "新聞提及": s.get("mention_count", 0),
                "ETF持有": etf_counts.get(str(s.get("ticker", "")), 0),
                "傾向": s.get("sentiment", ""),
                "原因": s.get("reason", ""),
            }
            for s in stocks
        ],
        use_container_width=True,
        hide_index=True,
    )

    # 依傾向分組卡片
    for label in ("利多", "利空", "觀望"):
        group = [s for s in stocks if s.get("sentiment") == label]
        if not group:
            continue
        emoji, _ = SENTIMENT_STYLE.get(label, ("", "info"))
        st.subheader(f"{emoji} {label}（{len(group)} 檔）")
        for s in group:
            name = s.get("name", "")
            ticker = s.get("ticker", "")
            head = f"**{name}**" + (f"（{ticker}）" if ticker else "")
            sector = s.get("sector", "")
            with st.container(border=True):
                st.markdown(
                    head
                    + (f"　·　{sector}" if sector else "")
                    + f"　·　📰 被提及 {s.get('mention_count', 0)} 次"
                )
                if s.get("reason"):
                    st.write(s["reason"])
                evidence = s.get("evidence_news", [])
                if evidence:
                    with st.expander("📰 佐證新聞"):
                        for n in evidence:
                            title = n.get("title", "")
                            src = n.get("source", "")
                            url = n.get("url", "")
                            line = f"- {title}" + (f" — _{src}_" if src else "")
                            if url:
                                line += f" [連結]({url})"
                            st.markdown(line)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🚀 未來趨勢產業")
        trends = data.get("future_trends", [])
        if trends:
            for t in trends:
                st.markdown(f"- {t}")
        else:
            st.caption("(本次新聞未明顯提及)")
    with col2:
        st.subheader("🌇 夕陽 / 轉弱產業")
        sunset = data.get("sunset_industries", [])
        if sunset:
            for t in sunset:
                st.markdown(f"- {t}")
        else:
            st.caption("(本次新聞未明顯提及)")

    st.caption("⚠️ 本頁由 AI 自動整理新聞而成,可能有誤,僅供參考,非投資建議。")


# ---------------------------------------------------------------------------
# 戰略報告
# ---------------------------------------------------------------------------

def render_report(report: dict) -> None:
    col1, col2, col3 = st.columns(3)
    col1.metric("報告日期", report.get("report_date", "—"))
    col2.metric("分析主題", report.get("topic", "—"))
    col3.metric("白話文來源", report.get("dictionary_source", "—"))
    st.divider()

    st.header("📰 第一階段:原始情報彙整")
    news = report.get("raw_news", [])
    if not news:
        st.info("本次未取得相關新聞。")
    render_news_cards(news)

    st.header("🧭 第二階段:四維度專業戰略分析")
    analysis = report.get("strategic_analysis", {})
    for key, label in ANALYSIS_SECTIONS:
        with st.expander(label, expanded=True):
            st.write(analysis.get(key, "(無內容)"))

    st.header("📖 第三階段:白話文翻譯字典")
    dictionary = report.get("laymans_dictionary", [])
    if dictionary:
        st.table(
            [{"專業術語": d.get("term", ""), "白話文意思": d.get("explanation", "")}
             for d in dictionary]
        )
    else:
        st.info("本次無術語需要翻譯。")

    st.caption("⚠️ 本報告由 AI 自動生成,非投資建議。局勢以最新確認消息為準。")


# ---------------------------------------------------------------------------
# 趨勢雷達
# ---------------------------------------------------------------------------

def render_trends(data: dict) -> None:
    st.metric("資料日期", data.get("report_date", "—"))
    st.caption("依「資金 / 徵才 / 政策 / 技術」四種訊號綜合評估,熱度 0–100。")
    st.divider()

    trends = data.get("trends", [])
    if not trends:
        st.info("本次未產生趨勢資料。")
        return

    for t in trends:
        rank = t.get("rank", "")
        industry = t.get("industry", "(未命名)")
        heat = t.get("heat_score", 0)
        with st.container(border=True):
            st.subheader(f"#{rank} {industry}　🔥 熱度 {heat}")
            try:
                st.progress(min(max(int(heat), 0), 100) / 100)
            except (TypeError, ValueError):
                pass
            if t.get("summary"):
                st.write(t["summary"])

            signals = t.get("signals", {})
            cols = st.columns(2)
            for i, (key, label) in enumerate(SIGNAL_LABELS):
                with cols[i % 2]:
                    st.markdown(f"**{label}**")
                    st.caption(signals.get(key, "—"))

            indicators = t.get("leading_indicators", [])
            if indicators:
                st.markdown("**📌 該緊盯的領先指標**")
                for ind in indicators:
                    st.markdown(f"- {ind}")

            evidence = t.get("evidence_news", [])
            if evidence:
                with st.expander("📰 佐證新聞"):
                    for n in evidence:
                        title = n.get("title", "")
                        source = n.get("source", "")
                        url = n.get("url", "")
                        line = f"- {title}" + (f" — _{source}_" if source else "")
                        if url:
                            line += f" [連結]({url})"
                        st.markdown(line)

    st.caption("⚠️ 趨勢評估由 AI 自動生成,非投資建議。新聞屬同步/落後訊號,請搭配資金與徵才等領先指標判讀。")


# ---------------------------------------------------------------------------
# ETF 持股反查
# ---------------------------------------------------------------------------

def render_price_update_panel(current_prices: dict) -> None:
    """透過 NAS 代理抓台股收盤價(供價位篩選);結果存 session,可下載。"""
    with st.expander(f"💰 股價資料（目前 {len(current_prices)} 檔)— 點此更新", expanded=not current_prices):
        st.caption("透過代理抓臺灣證交所(上市)＋櫃買中心(上櫃)當日收盤價,供『股價範圍』篩選使用。")
        proxy = ensure_proxy()
        if st.button("🔄 更新台股收盤價", use_container_width=True, disabled=not proxy):
            with st.spinner("透過代理抓台股收盤價中…"):
                logs: list[str] = []
                try:
                    data = price_fetcher.fetch_prices(proxy=proxy, log=logs.append)
                    st.session_state["price_data_live"] = data
                    st.success(f"完成!取得 {len(data.get('prices', {}))} 檔收盤價。請重整或重跑篩選。")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"抓取失敗:{exc}")
                if logs:
                    st.code("\n".join(logs))
        if not proxy:
            st.warning("未偵測到 PROXY_URL,無法抓股價。請先在 Streamlit Secrets 設定。")
        if st.session_state.get("price_data_live"):
            st.download_button(
                "⬇️ 下載 stock_prices.json(可 commit 回 repo 保存)",
                data=json.dumps(st.session_state["price_data_live"], ensure_ascii=False, indent=2),
                file_name="stock_prices.json",
                mime="application/json",
            )


def render_etf_lookup(data: dict | None = None) -> None:
    if data is None:
        data = etf_holdings.load_holdings(ETF_HOLDINGS_PATH)
    if not data:
        st.warning("找不到 `etf_holdings.json` 或格式有誤。請確認檔案存在且為合法 JSON。")
        return

    etfs = data.get("etfs", {})
    rows = etf_holdings.reverse_index(data)

    # 股價(供「價位範圍」篩選):本次即時抓到的優先,否則讀 repo 內 stock_prices.json
    price_data = st.session_state.get("price_data_live") or price_fetcher.load_prices()
    prices = price_data.get("prices", {}) if isinstance(price_data, dict) else {}
    for r in rows:
        r["price"] = prices.get(r["ticker"])

    c1, c2, c3 = st.columns(3)
    c1.metric("收錄 ETF 檔數", len(etfs))
    c2.metric("涵蓋個股數", len(rows))
    c3.metric("有股價個股數", sum(1 for r in rows if r.get("price")))
    st.caption(
        f"資料版本:{data.get('as_of', '—')}"
        + (f"　|　股價:{price_data.get('as_of', '—')}" if prices else "")
    )
    if data.get("note"):
        st.info("⚠️ " + data["note"])

    render_price_update_panel(prices)

    # 🔎 輸入代號/名稱直接查「這檔股票被哪些 ETF 持有」
    st.subheader("🔎 個股查詢 — 它被哪些 ETF 持有?")
    query = st.text_input(
        "輸入台股代號或名稱(例:2330 或 台積電)", value="", key="etf_query"
    ).strip()
    if query:
        matches = [
            r for r in rows if query in r["ticker"] or (r["name"] and query in r["name"])
        ]
        if matches:
            for r in matches:
                with st.container(border=True):
                    st.markdown(
                        f"### {r['name']}（{r['ticker']}）　🧩 被 **{r['etf_count']}** 檔 ETF 持有"
                    )
                    if r["etfs"]:
                        st.markdown(
                            "、".join(f"`{e['code']}` {e['name']}" for e in r["etfs"])
                        )
        else:
            st.warning(
                f"在目前收錄的 {len(etfs)} 檔 ETF 成分股裡找不到「{query}」。"
                "可能是該股尚未被收錄的 ETF 納入,或 `etf_holdings.json` 還沒收錄足夠 ETF。"
            )
    st.divider()

    st.subheader("📋 個股被 ETF 持有反查表")
    st.caption("『被幾檔 ETF 持有』越多,代表越多 ETF 同時納入該股——被動買盤越廣。可用下方條件篩選。")

    # 篩選條件
    f1, f2 = st.columns(2)
    max_count = max((r["etf_count"] for r in rows), default=1)
    min_etf = f1.slider(
        "① 至少被幾檔 ETF 持有", min_value=1, max_value=max_count, value=1, key="flt_min_etf"
    )

    priced = [r["price"] for r in rows if r.get("price")]
    use_price = bool(priced)
    if use_price:
        price_lo, price_hi = f2.slider(
            "② 股價範圍(元)", min_value=1, max_value=3000,
            value=(1, 3000), step=1, key="flt_price",
        )
        only_priced = f2.checkbox("只看有股價的個股", value=False, key="flt_only_priced")
    else:
        f2.caption("② 股價範圍:尚無股價資料,請先按上方「🔄 更新台股收盤價」。")
        price_lo, price_hi, only_priced = None, None, False

    # 套用篩選
    filtered = [r for r in rows if r["etf_count"] >= min_etf]
    if use_price:
        def _keep(r):
            p = r.get("price")
            if p is None:
                return not only_priced  # 沒股價的:勾「只看有股價」時排除
            return price_lo <= p <= price_hi
        filtered = [r for r in filtered if _keep(r)]

    st.caption(f"符合條件:**{len(filtered)}** 檔(共 {len(rows)} 檔)")
    st.dataframe(
        [
            {
                "個股": r["name"],
                "代號": r["ticker"],
                "股價": r.get("price") if r.get("price") is not None else "—",
                "被幾檔ETF持有": r["etf_count"],
                "ETF清單": "、".join(f"{e['code']} {e['name']}" for e in r["etfs"]),
            }
            for r in filtered
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.download_button(
        "⬇️ 下載篩選結果 JSON",
        data=json.dumps(filtered, ensure_ascii=False, indent=2),
        file_name="etf_reverse.json",
        mime="application/json",
    )

    with st.expander("📦 目前收錄的 ETF 與成分股"):
        names = data.get("stock_names", {})
        for code, info in etfs.items():
            name = info.get("name", code) if isinstance(info, dict) else code
            holdings = info.get("holdings", []) if isinstance(info, dict) else info
            shown = "、".join(f"{t} {names.get(t, '')}".strip() for t in holdings)
            st.markdown(f"**{code} {name}**（{len(holdings)} 檔）:{shown}")

    st.caption(
        "⚠️ 成分股為設定檔(`etf_holdings.json`)維護的範例/概況,可能非最新;"
        "請以各發行商最新公告為準。本頁非投資建議。"
    )


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="全球政經戰略看板", page_icon="🌐", layout="wide")
    st.title("🌐 全球政經戰略每日看板")

    st.sidebar.header("📂 報告類型")
    report_type = st.sidebar.radio(
        "選擇", ["戰略報告", "趨勢雷達", "台股觀察", "ETF持股反查"]
    )
    st.sidebar.divider()
    with st.sidebar:
        render_proxy_status()
    st.sidebar.divider()
    st.sidebar.header("📅 報告選擇")

    if report_type == "戰略報告":
        render_live_panel()

        # 1) 本次即時產生的完整報告(含 Gemini 分析)優先顯示
        if st.session_state.get("live_report"):
            live = st.session_state["live_report"]
            st.success("⚡ 以下為剛剛即時產生的報告(尚未存檔)。")
            st.download_button(
                "⬇️ 下載這份報告 JSON",
                data=json.dumps(live, ensure_ascii=False, indent=2),
                file_name=f"report_{live.get('report_date', 'latest')}.json",
                mime="application/json",
            )
            st.divider()
            render_report(live)
            return

        # 2) 已抓到新聞、尚未分析:顯示新聞,並提供第二步的 Gemini 按鈕
        if "live_news" in st.session_state:
            news = st.session_state["live_news"]
            st.divider()
            st.header("📰 即時抓取的新聞")
            if news:
                st.success(f"已抓到 {len(news)} 則真實外電,確認後再請 Gemini 分析:")
                has_key = ensure_gemini_key()
                if st.button(
                    "🧠 ② 用 Gemini 產生戰略分析 + 白話文",
                    use_container_width=True,
                    disabled=not has_key,
                    help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
                ):
                    with st.spinner("Gemini 分析中(約 10–30 秒)…"):
                        try:
                            generate_live_report()
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"產生報告失敗:{exc}")
                if not has_key:
                    render_key_hint()
                st.download_button(
                    "⬇️ 下載新聞 JSON",
                    data=json.dumps(news, ensure_ascii=False, indent=2),
                    file_name="news.json",
                    mime="application/json",
                )
                render_news_cards(news)
            else:
                st.info("這次沒抓到新聞,稍後再試或調整關鍵字。")
            return

        # 3) 否則顯示每日排程存檔的報告
        report = pick_report(REPORT_PATH, ARCHIVE_DIR)
        if report is None:
            st.warning("尚無每日排程報告。可用上方「⚡ 即時抓取」按鈕馬上取得新聞或產生報告。")
            return
        render_report(report)
    elif report_type == "趨勢雷達":
        st.header("🔥 趨勢雷達 — 現在最紅的產業")
        render_trend_live_panel()

        # 1) 本次即時產生的趨勢雷達優先顯示
        if st.session_state.get("live_trends"):
            live = st.session_state["live_trends"]
            st.success("⚡ 以下為剛剛即時產生的趨勢雷達(尚未存檔)。")
            st.download_button(
                "⬇️ 下載趨勢 JSON",
                data=json.dumps(live, ensure_ascii=False, indent=2),
                file_name=f"trends_{live.get('report_date', 'latest')}.json",
                mime="application/json",
            )
            st.divider()
            render_trends(live)
            return

        # 2) 已抓到產業新聞、尚未排名:顯示新聞,並提供第二步的 Gemini 按鈕
        if "live_trend_news" in st.session_state:
            news = st.session_state["live_trend_news"]
            st.divider()
            st.subheader("📰 即時抓取的產業新聞")
            if news:
                st.success(f"已抓到 {len(news)} 則產業新聞,確認後再請 Gemini 排名打分:")
                has_key = ensure_gemini_key()
                if st.button(
                    "🧠 ② 用 Gemini 產生趨勢雷達",
                    use_container_width=True,
                    disabled=not has_key,
                    help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
                ):
                    with st.spinner("Gemini 排名打分中(約 10–30 秒)…"):
                        try:
                            generate_live_trends()
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"產生趨勢雷達失敗:{exc}")
                if not has_key:
                    render_key_hint()
                st.download_button(
                    "⬇️ 下載產業新聞 JSON",
                    data=json.dumps(news, ensure_ascii=False, indent=2),
                    file_name="trend_news.json",
                    mime="application/json",
                )
                render_news_cards(news)
            else:
                st.info("這次沒抓到產業新聞,稍後再試或調整 TREND_QUERIES 關鍵字。")
            return

        # 3) 否則顯示每日排程存檔 + 歷史折線圖
        history = load_trend_history(TRENDS_ARCHIVE_DIR)
        if history is not None and len(history.index) >= 2:
            st.subheader("📈 產業熱度趨勢(歷史)")
            st.caption("看出『網路 → AI』式的長期轉移:哪條線持續往上,就是動能最強的產業。")
            st.line_chart(history)
            st.divider()
        elif history is not None:
            st.info("歷史折線圖需累積至少兩天的資料,明天就會開始出現。")

        data = pick_report(TRENDS_PATH, TRENDS_ARCHIVE_DIR)
        if data is None:
            st.warning("尚無每日趨勢雷達存檔。可用上方「⚡ 即時產生」按鈕馬上取得。")
            return
        render_trends(data)
    elif report_type == "台股觀察":
        st.header("📈 台股觀察 — 值得關注的台股標的")
        render_stock_live_panel()

        # 1) 本次即時產生的台股觀察優先顯示
        if st.session_state.get("live_stocks"):
            live = st.session_state["live_stocks"]
            st.success("⚡ 以下為剛剛即時產生的台股觀察(尚未存檔)。")
            st.download_button(
                "⬇️ 下載台股觀察 JSON",
                data=json.dumps(live, ensure_ascii=False, indent=2),
                file_name=f"stocks_{live.get('report_date', 'latest')}.json",
                mime="application/json",
            )
            st.divider()
            render_stocks(live)
            return

        # 2) 已抓到財經新聞、尚未整理:顯示新聞,並提供第二步的 Gemini 按鈕
        if "live_stock_news" in st.session_state:
            news = st.session_state["live_stock_news"]
            st.divider()
            st.subheader("📰 即時抓取的台灣財經新聞")
            if news:
                st.success(f"已抓到 {len(news)} 則財經新聞,確認後再請 Gemini 整理台股標的:")
                has_key = ensure_gemini_key()
                if st.button(
                    "🧠 ② 用 Gemini 整理台股標的(總表 + 利多/利空/觀望)",
                    use_container_width=True,
                    disabled=not has_key,
                    help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
                ):
                    with st.spinner("Gemini 整理台股標的中(約 10–30 秒)…"):
                        try:
                            generate_live_stocks()
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"整理台股標的失敗:{exc}")
                if not has_key:
                    render_key_hint()
                st.download_button(
                    "⬇️ 下載財經新聞 JSON",
                    data=json.dumps(news, ensure_ascii=False, indent=2),
                    file_name="stock_news.json",
                    mime="application/json",
                )
                render_news_cards(news)
            else:
                st.info("這次沒抓到台灣財經新聞,稍後再試或調整 STOCK_QUERIES 關鍵字。")
            return

        # 3) 否則顯示每日排程存檔
        data = pick_report(STOCKS_PATH, STOCKS_ARCHIVE_DIR)
        if data is None:
            st.warning("尚無每日台股觀察存檔。可用上方「⚡ 即時產生」按鈕馬上取得。")
            return
        render_stocks(data)
    else:
        st.header("🧩 ETF 持股反查 — 個股被幾檔 ETF 持有")
        render_etf_crawl_panel()
        render_etf_add_panel()
        # 本次即時抓到的資料庫優先;否則用 repo 內的 etf_holdings.json
        render_etf_lookup(st.session_state.get("etf_data_live"))


if __name__ == "__main__":
    main()
