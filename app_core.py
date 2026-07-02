"""app_core.py — 跨頁面共用工具函數與常數(SSOT)。

app.py 拆分 pages/ 後,原先散落在 app.py 頂部的共用工具統一搬移到此處。
pages/*.py 統一由此 import,不從 app.py import(避免循環相依)。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

import github_store  # 一鍵把資料檔 commit 回 GitHub repo
import paths  # 檔案/目錄路徑的單一真相源(SSOT)
import proxy_helper  # NAS 中繼站:設定讀取 + 連線健檢
import tz_utils  # 台灣時區時間的單一真相源(SSOT)
import config         # 環境變數讀取 SSOT
import gemini_client  # Gemini API 金鑰管理 SSOT
import update_data  # 重用爬蟲 + Gemini 管線,讓網頁可即時抓新聞/產報告

# 報告新鮮度門檻(天):歸屬日落後超過此值,看板顯示過期警告。可用 STALE_REPORT_DAYS 覆寫。
STALE_REPORT_DAYS = config.env_int("STALE_REPORT_DAYS", 2)
# 股價新鮮度門檻(天):stock_prices.json 抓取日落後超過此值,價位篩選顯示過期警告。可用 PRICE_STALE_DAYS 覆寫。
PRICE_STALE_DAYS = config.env_int("PRICE_STALE_DAYS", 5)

# 路徑一律取自 paths.py(SSOT);此處只保留本檔慣用的別名,引用處不動。
REPORT_PATH = paths.LATEST_REPORT
REPORTS_MULTI_PATH = paths.LATEST_REPORTS_MULTI
ARCHIVE_DIR = paths.ARCHIVE_REPORTS
TRENDS_PATH = paths.LATEST_TRENDS
TRENDS_ARCHIVE_DIR = paths.ARCHIVE_TRENDS
STOCKS_PATH = paths.LATEST_STOCKS
STOCKS_ARCHIVE_DIR = paths.ARCHIVE_STOCKS
US_STOCKS_PATH = paths.LATEST_US_STOCKS
US_STOCKS_ARCHIVE_DIR = paths.ARCHIVE_US_STOCKS
INTL_ALERT_PATH = paths.LATEST_INTL_ALERT
INTL_ALERT_ARCHIVE_DIR = paths.ARCHIVE_INTL_ALERT
CHIP_PATH = paths.LATEST_CHIP
CHIP_ARCHIVE_DIR = paths.ARCHIVE_CHIP
MARGIN_PATH = paths.LATEST_MARGIN
FUT_CHIP_PATH = paths.LATEST_FUT_CHIP  # 三大法人台指期留倉(外資期貨偏多/偏空)
FOCUS_PATH = paths.LATEST_FOCUS
FOCUS_ARCHIVE_DIR = paths.ARCHIVE_FOCUS
HOUSING_PATH = paths.LATEST_HOUSING
HOUSING_ARCHIVE_DIR = paths.ARCHIVE_HOUSING
HOUSING_REG_PATH = paths.LATEST_HOUSING_REG
HOUSING_REG_ARCHIVE_DIR = paths.ARCHIVE_HOUSING_REG
GEOJSON_PATH = paths.GEOJSON
REVERSAL_PATH = paths.LATEST_REVERSAL

SENTIMENT_STYLE = {
    "利多": ("🟢", "success"),
    "利空": ("🔴", "error"),
    "觀望": ("🟡", "info"),
}

# 房市冷熱配色(熱絡/持平/冷清)
HOUSING_SENTIMENT_STYLE = {
    "熱絡": ("🔥", "error"),
    "持平": ("⚖️", "info"),
    "冷清": ("❄️", "success"),
}

# 前五個章節資料來源回溯約 6 個月的說明(Google News RSS 實際回傳範圍仍有上限)
SIX_MONTH_SOURCE_CAPTION = (
    "🗓️ 資料來源回溯約 6 個月(實際可回溯範圍受 Google News RSS 限制);"
    "標的標示「首見 / 最近見報 / 共幾則」皆由本次抓到的真實新聞統計。"
)


def mention_caption(item: dict) -> str:
    """把一個標的的真實新聞統計組成『📅 首見 X · 最近 Y · 共 N 則』;無資料則回空字串。"""
    first = item.get("first_seen")
    last = item.get("last_seen")
    count = item.get("news_count")
    bits = []
    if first and last:
        bits.append(f"首見 {first}" + (f" · 最近 {last}" if last != first else ""))
    if count:
        bits.append(f"共 {count} 則")
    return "📅 " + " · ".join(bits) if bits else ""

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
    """側邊欄報告選擇器,回傳選定的 dict。

    同一頁會對不同資料源各呼叫一次,故以 latest_path 檔名衍生唯一 key,
    避免多個「選擇日期」selectbox 撞同一 auto ID(StreamlitDuplicateElementId)。
    """
    archive = list_archive(archive_dir)
    choice = st.sidebar.selectbox(
        "選擇日期", ["最新 (latest)"] + archive,
        key=f"datepick_{latest_path.stem}",
    )
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
    return config.env_str("REPORT_TOPIC") or update_data.DEFAULT_TOPIC


def available_secret_names() -> list[str]:
    """列出目前 Streamlit Secrets 內的頂層名稱(只回名稱,不回值),供除錯。"""
    try:
        return [str(k) for k in st.secrets.keys()]
    except Exception:  # noqa: BLE001 — 沒設定 secrets 或解析失敗
        return []


def _secret(name: str):
    """讀單一 Streamlit secret(供 github_store 取 GITHUB_TOKEN 等)。

    先看頂層;找不到再看 [github] 區段內的對應鍵(去掉 GITHUB_ 前綴)。
    """
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:  # noqa: BLE001
        pass
    # 區段寫法:[github] token = "..."
    try:
        sect = st.secrets["github"]
        short = name.lower().replace("github_", "").replace("gh_", "")
        for k in sect.keys():
            if str(k).lower() == short or str(k).lower() == name.lower():
                return sect[k]
    except Exception:  # noqa: BLE001
        pass
    return None


def render_crawl_summary(stats: dict) -> None:
    """顯示抓取摘要:✅成功 X / ❌失敗 Y + 失敗清單(代號/名稱/etfid/原因)。"""
    if not stats:
        return
    total = stats.get("total", 0)
    ok = stats.get("ok", 0)
    failed = stats.get("failed", []) or []
    cols = st.columns(3)
    cols[0].metric("清單檔數", total)
    cols[1].metric("✅ 成功", ok)
    cols[2].metric("❌ 失敗/略過", len(failed))
    if failed:
        with st.expander(f"❌ 沒抓到的 {len(failed)} 檔(代號 / 名稱 / 原因)", expanded=True):
            st.dataframe(
                [{"代號": f.get("code", ""), "名稱": f.get("name", ""),
                  "etfid": f.get("etfid", ""), "原因": f.get("reason", "")} for f in failed],
                use_container_width=True, hide_index=True,
            )
            st.caption("失敗多因 MoneyDJ 該頁無成分股表、或 etfid 格式不同。可把此清單貼給開發者校正。")


def save_to_github(filename: str, data, label: str = "") -> None:
    """把 data(dict)直接 commit 回 repo,並在畫面顯示結果。供『抓取後自動存』使用。"""
    if not github_store.is_configured(_secret):
        st.info(f"（未設定 GITHUB_TOKEN,{filename} 未自動存檔;可手動按下方存檔或下載。)")
        return
    content = json.dumps(data, ensure_ascii=False, indent=2)
    with st.spinner(f"自動存 {filename} 到 GitHub 中…"):
        ok, msg = github_store.commit_file(
            filename, content, f"🛰️ 自動更新 {filename}{label}", _secret
        )
    if ok:
        st.success(f"✅ 已自動存到 GitHub:{filename}　{msg}")
    else:
        st.error(f"自動存檔失敗({filename}):{msg}")


def render_github_save(filename: str, content: str, key: str, label: str | None = None) -> None:
    """通用『💾 直接存到 GitHub』按鈕:把 content commit 成 repo 內 filename。

    未設定 GITHUB_TOKEN 時停用按鈕並提示;同時保留旁邊的下載按鈕當備援。
    """
    configured = github_store.is_configured(_secret)
    if st.button(label or f"💾 直接存到 GitHub({filename})",
                 key=f"gh_{key}", use_container_width=True, disabled=not configured):
        with st.spinner("commit 到 GitHub 中…"):
            ok, msg = github_store.commit_file(
                filename, content, f"🛰️ 更新 {filename}(看板一鍵存檔)", _secret
            )
        if ok:
            st.success(f"已存到 GitHub!{msg}")
        else:
            st.error(f"存檔失敗:{msg}")
    if not configured:
        st.caption(
            "ℹ️ 一鍵存檔需 `GITHUB_TOKEN`(放 Streamlit Cloud → App settings → Secrets):"
        )
        st.code('GITHUB_TOKEN = "github_pat_..."', language="toml")
        names = available_secret_names()
        if names:
            st.caption("🔎 目前 Secrets 讀到的名稱:" + "、".join(f"`{n}`" for n in names)
                       + "(名稱需完全是 `GITHUB_TOKEN`,大小寫一致;勿放在 [區段] 下,或改用區段 `[github]` token=...)")
        else:
            st.caption("🔎 目前讀不到任何 Secrets — 可能尚未按 Save,或 TOML 格式有誤(缺引號/重複鍵)。")
        st.caption("未設定時可改用下方下載再手動上傳。")


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
    if gemini_client.get_gemini_keys():
        return True
    keys = _collect_keys_from_secrets()
    if keys:
        os.environ["GEMINI_API_KEY"] = ",".join(keys)
    return bool(gemini_client.get_gemini_keys())


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

def _render_evidence_news(evidence: list, label: str = "📰 佐證新聞") -> None:
    if not evidence:
        return
    with st.expander(label):
        for n in evidence:
            title = n.get("title", "")
            src = n.get("source", "")
            url = n.get("url", "")
            line = f"- {title}" + (f" — _{src}_" if src else "")
            if url:
                line += f" [連結]({url})"
            st.markdown(line)


def _render_stock_card_group(stocks: list, etf_counts: dict | None = None) -> None:
    """依傾向(利多/利空/觀望)分組顯示個股卡片。etf_counts 僅台股版傳入。"""
    for label in ("利多", "利空", "觀望"):
        group = [
            s for s in stocks
            if s.get("sentiment") == label
            and s.get("news_count", s.get("mention_count", 0)) > 0
        ]
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
                cap = mention_caption(s)
                if cap:
                    st.caption(cap)
                if s.get("reason"):
                    st.write(s["reason"])
                _render_evidence_news(s.get("evidence_news", []))


def _render_trends_sunset(data: dict) -> None:
    """未來趨勢產業 / 夕陽轉弱產業 兩欄(台股觀察 + 美股觀察共用)。"""
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

def render_market_digest(view: str, payload: dict) -> None:
    """各頁最上方:Gemini 把該領域當日各面板數據融成一段統一研判。"""
    key = f"digest_{view}_{tz_utils.taiwan_today()}"
    cached = st.session_state.get(key)
    with st.container(border=True):
        st.markdown(f"#### 🧠 AI 今日總結 — {view}")
        if cached:
            badge = {"偏多": "🟢 偏多", "偏空": "🔴 偏空", "中性": "⚪ 中性"}.get(
                cached.get("overall", ""), cached.get("overall", ""))
            if badge:
                st.markdown(f"**整體傾向:{badge}**")
            st.markdown(cached.get("digest_markdown", ""))
            st.caption("AI 依當日各面板數據融合研判,僅供參考、非投資建議。")
            if st.button("🔄 重新產生總結", key=f"redigest_{view}"):
                st.session_state.pop(key, None)
                st.rerun()
            return
        if not payload:
            st.caption("目前無當日資料可融合(等每日排程,或用下方各面板『即時抓取』後再產生)。")
            return
        has_key = ensure_gemini_key()
        if st.button(
            "🧠 產生 AI 今日總結", key=f"gen_digest_{view}",
            use_container_width=True, disabled=not has_key,
            help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
        ):
            with st.spinner("Gemini 融合當日數據中(約 10–30 秒)…"):
                try:
                    st.session_state[key] = update_data.get_market_digest(
                        view, payload, tz_utils.taiwan_today())
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"產生失敗:{exc}")
        if not has_key:
            render_key_hint()
