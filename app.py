"""Streamlit 前端 — 全球政經戰略報告 + 趨勢雷達。

側邊欄可切換報告類型(戰略報告 / 趨勢雷達),並瀏覽歷史存檔。
本地執行: streamlit run app.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

import etf_data  # ETF 資料單一真相源(快取共用層)
import etf_fetcher  # 透過代理抓 MoneyDJ 成分股建庫
import etf_holdings  # ETF 持股反查(純設定檔,不呼叫 AI)
import etf_profile_fetcher  # ETF 圖鑑:抓基本資料(型態/配息/費用/策略)
import freshness  # 資料新鮮度(staleness)判定 SSOT(§2.4)
import github_store  # 一鍵把資料檔 commit 回 GitHub repo
import numutil  # 漲跌幅公式 + 方向對帳的單一真相源(SSOT)
import housing_fetcher  # 房市觀察:抓房市新聞 + 實價登錄各縣市每坪房價
import paths  # 檔案/目錄路徑的單一真相源(SSOT)
import price_fetcher  # 透過代理抓台股收盤價(供價位篩選)
import proxy_helper  # NAS 中繼站:設定讀取 + 連線健檢
import tz_utils  # 台灣時區時間的單一真相源(SSOT)
import update_data  # 重用爬蟲 + Gemini 管線,讓網頁可即時抓新聞/產報告

# 報告新鮮度門檻(天):歸屬日落後超過此值,看板顯示過期警告。可用 STALE_REPORT_DAYS 覆寫。
try:
    STALE_REPORT_DAYS = int(os.environ.get("STALE_REPORT_DAYS", "2"))
except ValueError:
    STALE_REPORT_DAYS = 2
# 股價新鮮度門檻(天):stock_prices.json 抓取日落後超過此值,價位篩選顯示過期警告。可用 PRICE_STALE_DAYS 覆寫。
try:
    PRICE_STALE_DAYS = int(os.environ.get("PRICE_STALE_DAYS", "5"))
except ValueError:
    PRICE_STALE_DAYS = 5

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
GEOJSON_PATH = paths.GEOJSON

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
    return os.environ.get("REPORT_TOPIC") or update_data.DEFAULT_TOPIC


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
        auto = st.session_state.get("auto_save_github", True)
        if st.button(
            "🔄 立即抓取 / 更新 ETF 成分股資料庫",
            use_container_width=True,
            disabled=not proxy,
        ):
            with st.spinner("透過代理抓 MoneyDJ 成分股中…(視 ETF 檔數約數十秒)"):
                logs: list[str] = []
                try:
                    # 優先用 session 內的最新清單(雲端磁碟唯讀,新增的 ETF 只在 session)
                    live_sources = st.session_state.get("etf_sources_live")
                    data = etf_fetcher.crawl(proxy=proxy, log=logs.append, sources=live_sources)
                    stats = data.pop("_crawl_stats", {})  # 取出統計,不存進檔案
                    st.session_state["etf_data_live"] = data
                    st.success(f"完成!目前資料庫共 {len(data.get('etfs', {}))} 檔 ETF。")
                    render_crawl_summary(stats)
                    if auto:
                        save_to_github("etf_holdings.json", data, f"({len(data.get('etfs', {}))} 檔)")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"抓取失敗:{exc}")
                if logs:
                    with st.expander("📋 抓取明細(完整 log)"):
                        st.code("\n".join(logs))
        # 存檔區:常駐顯示(不必先抓取)。優先存本回合抓到的,否則存 repo 現有的。
        st.divider()
        st.markdown("**💾 存檔成分股資料庫**")
        live = st.session_state.get("etf_data_live")
        holdings_data = live or etf_data.get_holdings() or {}
        n_etf = len(holdings_data.get("etfs", {}))
        if live:
            st.caption(f"將存入本回合抓到的最新資料({n_etf} 檔 ETF)。")
        else:
            st.caption(f"尚未在本回合抓取;可先按上方「🔄 立即抓取」更新,或直接存目前 repo 既有的 {n_etf} 檔。")
        _holdings_str = json.dumps(holdings_data, ensure_ascii=False, indent=2)
        render_github_save("etf_holdings.json", _holdings_str, key="holdings")
        st.download_button(
            "⬇️ 下載 etf_holdings.json(備援:手動上傳)",
            data=_holdings_str,
            file_name="etf_holdings.json",
            mime="application/json",
        )


def render_etf_add_panel() -> None:
    """網頁新增 ETF 到來源清單(含重複檢查)+ 下載清單。"""
    # 本回合若已新增,沿用 session 內的清單,否則從檔案載入
    sources = st.session_state.get("etf_sources_live") or etf_fetcher.load_sources()
    etfs = sources.get("moneydj", {}).get("etfs", {})

    with st.expander(f"➕ 新增 ETF 到清單（目前 {len(etfs)} 檔)", expanded=False):
        # 一鍵匯入全市場 ETF(從 MoneyDJ ETF 列表頁抓所有代號)
        st.markdown("**🌐 一鍵匯入全市場 ETF**")
        st.caption("透過代理抓 MoneyDJ ETF 列表,把所有台股 ETF 代號併入清單(只補沒有的,名稱稍後抓取時自動補)。")
        proxy_imp = ensure_proxy()
        if st.button("🌐 匯入全台股 ETF 清單", use_container_width=True, disabled=not proxy_imp):
            with st.spinner("抓取 MoneyDJ ETF 列表中…"):
                try:
                    sources, added, total = etf_fetcher.import_all_etfs(proxy=proxy_imp, sources=sources)
                    st.session_state["etf_sources_live"] = sources
                    etfs = sources["moneydj"]["etfs"]
                    st.success(f"全市場共 {total} 檔,新增 {added} 檔,清單現有 {len(etfs)} 檔。")
                    if st.session_state.get("auto_save_github", True):
                        save_to_github("etf_sources.json", sources, f"({len(etfs)} 檔, 全市場匯入)")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"匯入失敗:{exc}")
        st.divider()
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
                    saved = ""
                if not proxy:
                    st.info("未設定 PROXY_URL,名稱暫時留空;設定代理後重抓即可補上名稱。")
                st.success("處理完成 " + saved)
                st.write("\n".join(f"- {m}" for m in msgs))
                etfs = sources["moneydj"]["etfs"]
                if st.session_state.get("auto_save_github", True):
                    save_to_github("etf_sources.json", sources,
                                   f"({len(etfs)} 檔)")

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
                    rsaved = ""
                st.success("處理完成 " + rsaved)
                st.write("\n".join(f"- {m}" for m in rmsgs))
                etfs = sources["moneydj"]["etfs"]
                if st.session_state.get("auto_save_github", True):
                    save_to_github("etf_sources.json", sources,
                                   f"({len(etfs)} 檔)")
        else:
            st.caption("清單目前是空的。")

        # 目前清單一覽 + 存檔
        st.caption(f"目前清單({len(etfs)} 檔):" + "、".join(f"{c} {i.get('name','')}".strip() for c, i in etfs.items()))
        _sources_str = json.dumps(sources, ensure_ascii=False, indent=2)
        render_github_save("etf_sources.json", _sources_str, key="sources")
        st.download_button(
            "⬇️ 下載 etf_sources.json(備援:手動上傳)",
            data=_sources_str,
            file_name="etf_sources.json",
            mime="application/json",
        )


def render_etf_profiles() -> None:
    """ETF 圖鑑:抓基本資料建庫 + 篩選器(型態/區域/配息/費用/主題/策略)。"""
    # 建庫面板
    with st.container(border=True):
        st.markdown("#### 🛰️ 透過 NAS 代理建立 ETF 圖鑑(MoneyDJ 基本資料)")
        st.caption("抓清單內每檔 ETF 的型態、投資區域、配息、經理費/保管費、追蹤指數與主題標籤。")
        proxy = ensure_proxy()
        if not proxy:
            st.warning("未偵測到 PROXY_URL,無法抓取。請先在 Streamlit Secrets 設定。")
        auto_p = st.session_state.get("auto_save_github", True)
        if st.button("🔄 抓取 / 更新 ETF 圖鑑資料", use_container_width=True, disabled=not proxy):
            with st.spinner("透過代理抓 ETF 基本資料中…(視檔數約 1 分鐘)"):
                logs: list[str] = []
                try:
                    live_sources = st.session_state.get("etf_sources_live")
                    data = etf_profile_fetcher.crawl(proxy=proxy, log=logs.append, sources=live_sources)
                    st.session_state["etf_profiles_live"] = data
                    st.success(f"完成!共 {len(data.get('profiles', {}))} 檔。")
                    if auto_p:
                        save_to_github("etf_profiles.json", data, f"({len(data.get('profiles', {}))} 檔)")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"抓取失敗:{exc}")
                if logs:
                    with st.expander("📋 抓取明細"):
                        st.code("\n".join(logs))
        # 存檔區:常駐顯示
        st.divider()
        st.markdown("**💾 存檔 ETF 圖鑑資料庫**")
        live_p = st.session_state.get("etf_profiles_live")
        profiles_data = live_p or etf_data.get_profiles() or {}
        n_p = len(profiles_data.get("profiles", {}))
        st.caption(
            f"將存入本回合抓到的最新資料({n_p} 檔)。" if live_p
            else f"尚未在本回合抓取;可先按上方「🔄 抓取」,或直接存 repo 既有的 {n_p} 檔。"
        )
        _profiles_str = json.dumps(profiles_data, ensure_ascii=False, indent=2)
        render_github_save("etf_profiles.json", _profiles_str, key="profiles")
        st.download_button(
            "⬇️ 下載 etf_profiles.json(備援:手動上傳)",
            data=_profiles_str,
            file_name="etf_profiles.json",
            mime="application/json",
        )

        # 診斷:抓一檔看 MoneyDJ 真實欄位名(若分類大量判錯,用這個校正解析器)
        with st.expander("🔬 診斷單檔欄位(分類抓不到時用)"):
            st.caption("輸入代號,列出該頁解析到的『欄位名 → 值』。基本資料在 Basic0004(簡介頁);"
                       "Basic0001 是即時報價頁(沒有種類/配息/費用)。可截圖貼給開發者校正。")
            dc1, dc2 = st.columns([2, 1])
            diag_code = dc1.text_input("代號", value="0056", key="etf_diag_code").strip()
            diag_page = dc2.selectbox("頁面", ["0004", "0005", "0003", "0001"], key="etf_diag_page")
            if st.button("🔬 診斷此檔", disabled=not proxy, key="btn_etf_diag"):
                with st.spinner("抓取中…"):
                    try:
                        kv = etf_profile_fetcher.diagnose(f"{diag_code}.TW", proxy=proxy, page=diag_page)
                        if kv:
                            st.json(kv)
                        else:
                            st.warning("沒解析到任何欄位(頁面結構可能不同,換個頁面試試)。")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"診斷失敗:{exc}")

    data = st.session_state.get("etf_profiles_live") or etf_data.get_profiles()
    profiles = list((data.get("profiles") or {}).values()) if isinstance(data, dict) else []
    if not profiles:
        st.info("尚無 ETF 圖鑑資料。請先按上方「🔄 抓取 / 更新」建立(需設定 PROXY_URL)。")
        return

    st.caption(f"資料版本:{data.get('as_of', '—')}　|　共 {len(profiles)} 檔")

    # ---- 篩選器 ----
    st.subheader("🔎 篩選器")
    present = lambda key, opts: [o for o in opts if any(p.get(key) == o for p in profiles)]
    c1, c2, c3 = st.columns(3)
    f_cat = c1.multiselect("型態", present("category", etf_profile_fetcher.CATEGORIES))
    f_region = c2.multiselect("投資區域", present("region", etf_profile_fetcher.REGIONS))
    f_freq = c3.multiselect("配息頻率", present("dividend_freq", etf_profile_fetcher.DIVIDEND_FREQS))

    c4, c5 = st.columns(2)
    f_months = c4.multiselect("配息月份(任一符合)", list(range(1, 13)))
    c4.caption("配息月份:月配=確定;季配/雙月/半年/年配為依頻率推測的常見版本(月份後標 *)。")
    all_themes = sorted({t for p in profiles for t in (p.get("themes") or [])})
    f_themes = c5.multiselect("主題 / 理念(任一符合)", all_themes)

    c6, c7 = st.columns(2)
    f_strategy = c6.multiselect("投資策略", ["被動(追蹤指數)", "主動式"])
    max_fee = c7.slider("總管理費用上限(%)", 0.0, 3.0, 3.0, step=0.05)

    price_lo, price_hi = st.slider("ETF 市價範圍(元)", 0, 3000, (0, 3000), step=1)

    def _fee(p: dict):
        # 優先用總管理費用;沒有就退回經理費(+保管費)
        return p.get("total_fee") or p.get("mgmt_fee")

    def keep(p: dict) -> bool:
        if f_cat and p.get("category") not in f_cat:
            return False
        if f_region and p.get("region") not in f_region:
            return False
        if f_freq and p.get("dividend_freq") not in f_freq:
            return False
        if f_months and not (set(f_months) & set(p.get("dividend_months") or [])):
            return False
        if f_themes and not (set(f_themes) & set(p.get("themes") or [])):
            return False
        if f_strategy and p.get("strategy") not in f_strategy:
            return False
        fee = _fee(p)
        if fee is not None and fee > max_fee:
            return False
        pr = p.get("price")
        # 非預設範圍時才用市價過濾;有市價才比對(沒抓到市價的不因此被濾掉)
        if (price_lo, price_hi) != (0, 3000) and pr is not None and not (price_lo <= pr <= price_hi):
            return False
        return True

    filtered = [p for p in profiles if keep(p)]
    st.caption(f"符合條件:**{len(filtered)}** 檔(共 {len(profiles)} 檔)")

    st.dataframe(
        [
            {
                "代號": p.get("code", ""),
                "名稱": p.get("name", ""),
                "型態": p.get("category", ""),
                "區域": p.get("region", ""),
                "配息": p.get("dividend_freq", ""),
                "配息月": ("、".join(str(m) for m in (p.get("dividend_months") or []))
                          + (" *" if p.get("months_estimated") else "")),
                "市價": p.get("price"),
                "殖利率%": p.get("yield_pct"),
                "經理費%": p.get("mgmt_fee"),
                "總費用%": p.get("total_fee"),
                "規模(百萬)": p.get("scale_million"),
                "策略": p.get("strategy", ""),
                "主題": "、".join(p.get("themes") or []),
                "經理人": p.get("manager", ""),
                "發行商": p.get("issuer", ""),
                "追蹤指數": p.get("index_tracked", ""),
            }
            for p in filtered
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.download_button(
        "⬇️ 下載篩選結果 JSON",
        data=json.dumps(filtered, ensure_ascii=False, indent=2),
        file_name="etf_profiles_filtered.json",
        mime="application/json",
    )
    st.caption("⚠️ 資料抓自 MoneyDJ、自動分類可能有誤;費用/配息以各發行商公開說明書為準。非投資建議。")


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
    today = tz_utils.taiwan_today()
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
    today = tz_utils.taiwan_today()
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
    today = tz_utils.taiwan_today()
    st.session_state["live_stocks"] = update_data.get_stock_picks(news, today)
    st.session_state.pop("live_stock_news", None)


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


def render_stocks(data: dict) -> None:
    st.metric("資料日期", data.get("report_date", "—"))
    if data.get("summary"):
        st.info(data["summary"])
    st.caption("依新聞『被提及次數』排序;標的分利多/利空/觀望。⚠️ 僅為新聞整理,非投資建議。")
    st.caption(SIX_MONTH_SOURCE_CAPTION)

    stocks = data.get("stocks", [])
    if not stocks:
        st.info("本次未整理出台股標的。")
        return

    # 交叉參照:每檔個股被幾檔 ETF 持有(共用 etf_data 快取,與反查頁同一來源)
    etf_counts = etf_data.get_etf_count_map()

    # 總表(新聞提及次數 + ETF 持有檔數 + 首見/最近見報,多個訊號一起看)
    st.subheader("📋 台股標的總表(新聞提及 × ETF 持有 × 見報區間)")
    st.caption("被很多 ETF 持有 ＋ 新聞偏利多 = 相對更受關注。ETF 檔數來自 etf_holdings.json;首見/最近/則數由真實新聞統計。")
    st.dataframe(
        [
            {
                "標的": s.get("name", ""),
                "代號": s.get("ticker", ""),
                "產業": s.get("sector", ""),
                "則數": s.get("news_count", s.get("mention_count", 0)),
                "首見": s.get("first_seen", ""),
                "最近": s.get("last_seen", ""),
                "ETF持有": etf_counts.get(str(s.get("ticker", "")), 0),
                "傾向": s.get("sentiment", ""),
                "原因": s.get("reason", ""),
            }
            for s in stocks
        ],
        use_container_width=True,
        hide_index=True,
    )

    _render_stock_card_group(stocks, etf_counts)
    _render_trends_sunset(data)
    st.caption("⚠️ 本頁由 AI 自動整理新聞而成,可能有誤,僅供參考,非投資建議。")


# ---------------------------------------------------------------------------
# 美股觀察(邏輯同台股觀察,資料來源換成美股財經新聞)
# ---------------------------------------------------------------------------

def render_us_stock_live_panel() -> None:
    """美股觀察第一步:只抓美股財經新聞(整理另由 Gemini 按鈕觸發)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時產生(免等每日排程)")
        st.caption(
            "從美股財經新聞統計被提到最多次的美股標的,分利多/利空/觀望,"
            "並歸納未來趨勢與夕陽產業。流程:① 先抓財經新聞 → ② 看過後再按 Gemini 整理。"
        )
        if st.button("🔄 ① 立即抓取美股財經新聞", use_container_width=True):
            with st.spinner("抓取美股財經新聞中…"):
                try:
                    st.session_state["live_us_stock_news"] = update_data.fetch_us_stock_news()
                    st.session_state.pop("live_us_stocks", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["live_us_stock_news"] = []
                    st.error(f"抓取失敗:{exc}")


def generate_live_us_stocks() -> None:
    """美股觀察第二步:對『已抓到的財經新聞』請 Gemini 整理美股標的。"""
    news = st.session_state.get("live_us_stock_news", [])
    today = tz_utils.taiwan_today()
    st.session_state["live_us_stocks"] = update_data.get_us_stock_picks(news, today)
    st.session_state.pop("live_us_stock_news", None)


def render_us_stocks(data: dict) -> None:
    st.metric("資料日期", data.get("report_date", "—"))
    if data.get("summary"):
        st.info(data["summary"])
    st.caption("依新聞『被提及次數』排序;標的分利多/利空/觀望。⚠️ 僅為新聞整理,非投資建議。")
    st.caption(SIX_MONTH_SOURCE_CAPTION)

    stocks = data.get("stocks", [])
    if not stocks:
        st.info("本次未整理出美股標的。")
        return

    # 總表(依新聞提及次數 + 首見/最近見報)
    st.subheader("📋 美股標的總表(新聞提及 × 見報區間)")
    st.caption("被很多新聞提及 ＋ 偏利多 = 相對更受關注;首見/最近/則數由真實新聞統計。")
    st.dataframe(
        [
            {
                "標的": s.get("name", ""),
                "代號": s.get("ticker", ""),
                "產業": s.get("sector", ""),
                "則數": s.get("news_count", s.get("mention_count", 0)),
                "首見": s.get("first_seen", ""),
                "最近": s.get("last_seen", ""),
                "傾向": s.get("sentiment", ""),
                "原因": s.get("reason", ""),
            }
            for s in stocks
        ],
        use_container_width=True,
        hide_index=True,
    )

    _render_stock_card_group(stocks)
    _render_trends_sunset(data)
    st.caption("⚠️ 本頁由 AI 自動整理新聞而成,可能有誤,僅供參考,非投資建議。")


# ---------------------------------------------------------------------------
# 國際盤預警(美股指數/期貨/台指期夜盤真實漲跌幅 → 偵測大跌 → Gemini 解讀台股影響)
# ---------------------------------------------------------------------------

ALERT_BADGE = {"警戒": ("🔴", "error"), "觀察": ("🟠", "warning"), "平靜": ("🟢", "success")}


def render_intl_alert_live_panel() -> None:
    """國際盤預警第一步:抓真實指數/期貨報價(免金鑰)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時產生(免等每日排程)")
        st.caption(
            "抓美股指數(隔夜領先)、美股期貨與台指期夜盤(盤前即時)的真實漲跌幅,"
            "偵測突然大跌;再由 Gemini 依新聞解讀利空原因與對台股影響。"
            "流程:① 先抓報價(免金鑰)→(看過後)② 按 Gemini 解讀。"
        )
        if st.button("🔄 ① 立即抓國際盤報價(免金鑰)", use_container_width=True):
            with st.spinner("抓國際盤報價中…"):
                try:
                    st.session_state["live_intl_quotes"] = (
                        update_data.index_fetcher.fetch_index_quotes()
                    )
                    st.session_state.pop("live_intl_alert", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state.pop("live_intl_quotes", None)
                    st.error(f"抓取失敗:{exc}")


def generate_live_intl_alert() -> None:
    """國際盤預警第二步:用『已抓報價』+ 新聞,請 Gemini 解讀利空與台股影響。"""
    quotes = st.session_state.get("live_intl_quotes")
    today = tz_utils.taiwan_today()
    st.session_state["live_intl_alert"] = update_data.build_intl_alert(today, quotes=quotes)
    st.session_state.pop("live_intl_quotes", None)


def render_index_quotes(qmap: dict) -> None:
    """以 st.metric(自動紅綠)分組呈現指數/期貨漲跌幅。"""
    if not qmap:
        st.info("本次未取得任何指數報價。")
        return
    groups: dict[str, list] = {}
    for sym, q in qmap.items():
        groups.setdefault(q.get("group", "其他"), []).append((sym, q))
    for group, items in groups.items():
        st.markdown(f"**{group}**")
        cols = st.columns(len(items))
        for col, (sym, q) in zip(cols, items):
            col.metric(
                label=q.get("name", sym),
                value=q.get("last", "—"),
                delta=f"{q.get('change_pct', 0):+.2f}%",
            )
            if group == "債匯":
                up = q.get("change_pct", 0) > 0
                if sym == "TWD=X":
                    # 新台幣:USD/TWD 走升=台幣貶值=外資賣股匯出提款的真實訊號
                    cap = "📈 台幣貶=外資匯出" if up else "台幣升=外資匯入"
                else:
                    # 殖利率/美元:上升=資金收緊(利空),非「大跌」
                    cap = "📈 走升=資金收緊" if up else "走弱=資金寬鬆"
            else:
                cap = q.get("lead_type", "")
                if q.get("is_drop"):
                    cap += " · ⚠️大跌"
            if cap:
                col.caption(cap)


def render_intl_alert(data: dict) -> None:
    level = data.get("alert_level", "—")
    emoji, kind = ALERT_BADGE.get(level, ("", "info"))
    banner = getattr(st, kind, st.info)
    summary = data.get("summary", "")
    banner(f"{emoji} 警示級別:{level}" + (f" — {summary}" if summary else ""))
    st.caption(
        f"報價時間:{data.get('as_of', '—')} · 大跌門檻 {data.get('threshold', '')}% · "
        "數字為真實市場報價(Yahoo Finance),非 AI 估算"
    )

    st.subheader("📊 國際盤即時報價(時間差定位)")
    st.caption("漲跌幅=最新 vs 前收;美股指數→隔夜領先台股,美股期貨/台指期夜盤→盤前即時。")
    render_index_quotes(data.get("quotes", {}))

    drops = data.get("drops", [])
    if drops:
        st.subheader(f"⚠️ 大跌預警({len(drops)} 項)")
        for d in drops:
            st.error(
                f"**{d.get('name', '')}** {d.get('change_pct', 0):+.2f}%"
                f"　·　{d.get('lead_type', '')}"
            )
    else:
        st.success("✅ 目前追蹤標的均未觸及大跌門檻。")

    interp = data.get("interpretation", [])
    if interp:
        st.subheader("🧭 利空原因解讀(依新聞)")
        for it in interp:
            with st.container(border=True):
                if it.get("market"):
                    st.markdown(f"**{it['market']}**")
                if it.get("cause"):
                    st.write(it["cause"])
                _render_evidence_news(it.get("evidence_news", []))
    elif not data.get("ai_ok", True):
        st.info("ℹ️ 利空原因解讀因 AI 配額/網路暫缺(原因待補);上方真實報價與大跌偵測不受影響。")

    us = data.get("us_view", {})
    if us:
        st.subheader("🇺🇸 對美股的看法")
        direction = us.get("direction", "中性")
        badge = {"偏多": "🟢", "偏空": "🔴", "中性": "⚪"}.get(direction, "")
        st.markdown(f"**研判方向:{badge} {direction}**")
        if us.get("reason"):
            st.write(us["reason"])
        focus = us.get("focus", [])
        if focus:
            st.markdown("**觀察焦點:** " + "、".join(str(s) for s in focus))

    imp = data.get("tw_impact", {})
    if imp:
        st.subheader("🇹🇼 對台股可能影響")
        direction = imp.get("direction", "中性")
        badge = {"偏多": "🟢", "偏空": "🔴", "中性": "⚪"}.get(direction, "")
        st.markdown(f"**研判方向:{badge} {direction}**")
        if imp.get("reason"):
            st.write(imp["reason"])
        sectors = imp.get("sectors", [])
        if sectors:
            st.markdown("**重點族群:** " + "、".join(str(s) for s in sectors))

    render_confluence(data)
    render_chip_events(data.get("upcoming_events", []))

    st.caption(
        "⚠️ 時間差僅為參考性連動,非必然因果;本頁由真實報價 + AI 新聞研判組成,僅供參考,非投資建議。"
    )


def render_confluence(intl: dict) -> None:
    """🔴 多重賣壓共振:美股大跌 + 四力≥2(讀 latest_chip / latest_margin,真實數據判定)。"""
    chip = load_json(CHIP_PATH)
    margin = load_json(MARGIN_PATH)
    fut_chip = load_json(FUT_CHIP_PATH)
    try:
        conf = update_data.detect_pressure_confluence(intl, chip, margin, fut_chip)
    except Exception:  # noqa: BLE001 — 偵測失敗不影響整頁
        return
    st.subheader("🔴 多重賣壓共振偵測")
    forces_txt = "、".join(f.get("detail", "") for f in conf.get("forces", [])) or "無"
    if conf.get("triggered"):
        st.error(f"⚠️ 已觸發!美股大跌 + 共振力量 {conf['count']}/4:{forces_txt}")
        st.caption("非單一利空,多股賣壓疊加 → 排程會自動推 LINE 預警。")
    else:
        gate = "✅" if conf.get("us_drops") else "❌"
        st.info(f"未觸發(美股大跌 {gate}、共振力量 {conf['count']}/4)。成立力量:{forces_txt}")
    st.caption("四力:外資提款(現貨賣超／台指期偏空／台幣貶值任一)、散戶斷頭(融資)、"
               "Fed 收緊(殖利率/美元)、配息賣壓。全為真實數據判定,非投資建議。")


def render_chip_events(events: list) -> None:
    """📅 可預測法人賣壓行事曆(純規則推算;ETF 除息檔數為真實資料)。"""
    if not events:
        return
    st.subheader("📅 可預測法人賣壓行事曆(預先知道的)")
    st.caption("以下為曆法/慣例可事先推算的籌碼事件;每日排程會在事件進入 3 個交易日窗口時推播 LINE 預告。")
    type_emoji = {"季底作帳": "🧾", "年底作帳": "🧾", "MSCI調整": "🌐",
                  "除權息旺季": "💰", "ETF除息潮": "📦"}
    for e in events:
        td = e.get("trading_days_until", 0)
        when = "今日" if td == 0 else (f"約 {td} 個交易日後" if td > 0 else "已發生")
        with st.container(border=True):
            st.markdown(
                f"{type_emoji.get(e.get('type', ''), '📌')} **{e.get('title', '')}**　"
                f"`{e.get('date', '')}`　·　{when}"
            )
            if e.get("detail"):
                st.caption(e["detail"])
            st.caption(f"來源:{e.get('source', '—')}")


def render_chip(data: dict) -> None:
    """📊 法人籌碼事後驗證:近 N 日三大法人買賣超(真實數字,單位:億元)。"""
    days = data.get("days", [])
    if not days:
        st.warning("尚無三大法人資料。")
        return
    st.caption(
        f"資料時間:{data.get('as_of', '—')} · 數字為證交所 BFI82U 真實買賣超(非 AI 估算),"
        "以下換算為億元(原始單位:元)。"
    )
    rows = []
    for d in days:  # days 由新到舊
        rows.append({
            "日期": d.get("date", ""),
            "外資": round(d.get("foreign", 0) / update_data.OKU, 1),
            "投信": round(d.get("trust", 0) / update_data.OKU, 1),
            "自營商": round(d.get("dealer", 0) / update_data.OKU, 1),
            "三大法人合計": round(d.get("total", 0) / update_data.OKU, 1),
        })
    df = pd.DataFrame(rows)

    latest = rows[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("外資(最新)", f"{latest['外資']:+.0f} 億")
    c2.metric("投信(最新)", f"{latest['投信']:+.0f} 億")
    c3.metric("自營商(最新)", f"{latest['自營商']:+.0f} 億")
    c4.metric("三大法人合計", f"{latest['三大法人合計']:+.0f} 億")

    st.subheader("📈 近期買賣超趨勢(億元)")
    chart_df = df.set_index("日期")[["外資", "投信", "自營商"]].iloc[::-1]  # 還原成由舊到新
    st.line_chart(chart_df)

    st.subheader("📋 每日明細(億元)")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(
        "🟢 正=買超、🔴 負=賣超。外資=外資及陸資+外資自營商;自營商=自行買賣+避險。"
        "單日大幅賣超即你問的『機構賣壓』可在此事後驗證。非投資建議。"
    )

    margin = load_json(MARGIN_PATH)
    if margin:
        st.subheader("💳 融資餘額(散戶槓桿/斷頭訊號)")
        m1, m2 = st.columns(2)
        m1.metric("融資餘額", f"{margin.get('margin_today', 0)/update_data.OKU:.0f} 億",
                  delta=f"{margin.get('margin_chg_pct', 0):+.2f}%")
        m2.metric("單日增減", f"{margin.get('margin_chg', 0)/update_data.OKU:+.0f} 億")
        st.caption(f"資料:{margin.get('date', '—')}(證交所 MI_MARGN,真實)。"
                   "融資大減=去槓桿/斷頭賣壓,為共振偵測四力之一。")

    fut = load_json(FUT_CHIP_PATH)
    if fut and fut.get("foreign_net_oi") is not None:
        st.subheader("📐 外資台指期留倉(期貨部位偏多/偏空)")
        stance = fut.get("stance", "中性")
        badge = {"偏多": "🟢 偏多", "偏空": "🔴 偏空", "中性": "⚪ 中性"}.get(stance, stance)
        f1, f2, f3 = st.columns(3)
        f1.metric("外資 期貨方向", badge, delta=f"{fut.get('foreign_net_oi', 0):+,} 口")
        f2.metric("投信 留倉淨額", f"{fut.get('trust_net_oi', 0):+,} 口")
        f3.metric("自營 留倉淨額", f"{fut.get('dealer_net_oi', 0):+,} 口")
        st.caption(
            f"資料:{fut.get('date', '—')}(期交所「三大法人台指期」未平倉口數淨額,真實)。"
            "⚠️ 這是**前一交易日盤後**的『留倉(現在仍持有的部位)』,正=淨多偏多、負=淨空偏空;"
            "與上方現貨買賣超互補:現貨看當日流量、期貨看持有方向。非投資建議。"
        )


# ---------------------------------------------------------------------------
# 全球人物追蹤(中文輸入 → 自動翻英 → 抓全球新聞 → 台美股關聯分析)
# ---------------------------------------------------------------------------

def render_focus_panel() -> None:
    """第一步:中文輸入 → Gemini 翻英 → 抓全球英文新聞。"""
    with st.container(border=True):
        st.markdown("#### 🔎 輸入關注對象(中文)")
        st.caption(
            "例如:川普、黃仁勳、輝達、AI 晶片。系統會自動翻成英文抓全球新聞,"
            "再請 Gemini 整理他說了什麼、衍伸哪些產業,以及可能牽動哪些台股/美股。"
        )
        term = st.text_input(
            "關注對象", key="focus_term_input", placeholder="川普 / 黃仁勳 / 輝達 / AI 晶片 …"
        )
        has_key = ensure_gemini_key()
        if st.button(
            "🌍 ① 翻譯並抓全球新聞",
            use_container_width=True,
            disabled=not has_key,
            help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
        ):
            if not term.strip():
                st.warning("請先輸入要追蹤的中文關鍵字。")
            else:
                with st.spinner("翻譯關鍵字並抓取全球新聞中…"):
                    try:
                        tr = update_data.translate_focus_query(term.strip())
                        news = update_data.fetch_focus_news(
                            tr.get("query_en", ""), tr.get("aliases"),
                            tr.get("query_zh", term.strip()), tr.get("zh_aliases"),
                        )
                        st.session_state["live_focus_translation"] = tr
                        st.session_state["live_focus_news"] = news
                        st.session_state["live_focus_term"] = term.strip()
                        st.session_state.pop("live_focus", None)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"抓取失敗:{exc}")
        if not has_key:
            render_key_hint()


def generate_live_focus() -> None:
    """第二步:對『已抓到的全球新聞』請 Gemini 做關聯分析。"""
    tr = st.session_state.get("live_focus_translation", {})
    news = st.session_state.get("live_focus_news", [])
    term = st.session_state.get("live_focus_term", tr.get("query_zh", ""))
    today = tz_utils.taiwan_today()
    st.session_state["live_focus"] = update_data.get_focus_analysis(
        term, tr.get("query_en", ""), news, today
    )
    st.session_state.pop("live_focus_news", None)


def render_focus(data: dict) -> None:
    col1, col2 = st.columns(2)
    col1.metric("關注對象", data.get("query_zh", "—"))
    col2.metric("英文檢索", data.get("query_en", "—"))
    if data.get("summary"):
        st.info(data["summary"])
    st.caption("由 AI 依全球新聞整理,可能有誤,僅供參考,非投資建議。")
    if data.get("first_seen"):
        st.caption(
            f"🗓️ 相關新聞 {data['first_seen']} ～ {data.get('last_seen', '')}"
            f"，共 {data.get('news_count', 0)} 則(回溯約 6 個月,實際範圍受 RSS 限制)。"
        )

    statements = data.get("key_statements", [])
    if statements:
        st.subheader("🗣️ 他說了什麼 / 關鍵動向")
        for s in statements:
            st.markdown(f"- {s}")

    industries = data.get("affected_industries", [])
    if industries:
        st.subheader("🏭 衍伸 / 受影響產業")
        st.markdown("　".join(f"`{i}`" for i in industries))

    stocks = data.get("stocks", [])
    if stocks:
        st.subheader("📈 可能牽動的個股(台股 / 美股)")
        st.dataframe(
            [
                {
                    "市場": s.get("market", ""),
                    "標的": s.get("name", ""),
                    "代號": s.get("ticker", ""),
                    "產業": s.get("sector", ""),
                    "則數": s.get("news_count", 0),
                    "首見": s.get("first_seen", ""),
                    "最近": s.get("last_seen", ""),
                    "傾向": s.get("sentiment", ""),
                    "原因": s.get("reason", ""),
                }
                for s in stocks
            ],
            use_container_width=True,
            hide_index=True,
        )
        for market in ("台股", "美股"):
            group = [s for s in stocks if s.get("market") == market]
            if not group:
                continue
            st.markdown(f"##### 🏷️ {market}（{len(group)} 檔）")
            for s in group:
                name = s.get("name", "")
                ticker = s.get("ticker", "")
                head = f"**{name}**" + (f"（{ticker}）" if ticker else "")
                sector = s.get("sector", "")
                senti = s.get("sentiment", "")
                emoji, _ = SENTIMENT_STYLE.get(senti, ("", "info"))
                with st.container(border=True):
                    st.markdown(
                        head
                        + (f"　·　{sector}" if sector else "")
                        + (f"　·　{emoji} {senti}" if senti else "")
                    )
                    cap = mention_caption(s)
                    if cap:
                        st.caption(cap)
                    if s.get("reason"):
                        st.write(s["reason"])
                    _render_evidence_news(s.get("evidence_news", []))
    else:
        st.info("本次新聞未對應到明確的台股/美股個股。")

    _render_evidence_news(data.get("evidence_news", []), "📰 全部佐證新聞")

    st.caption("⚠️ 本頁由 AI 自動整理新聞而成,可能有誤,僅供參考,非投資建議。")


# ---------------------------------------------------------------------------
# 個股健診(輸入單一個股 → 抓該股新聞 → 問:與新聞相關性 + 上漲是消息面還是基本面)
# ---------------------------------------------------------------------------

def render_stock_query_panel() -> None:
    """第一步:輸入個股 → Gemini 正規化中英名/代號/市場 → 抓該股新聞。"""
    with st.container(border=True):
        st.markdown("#### 🔎 輸入要健診的個股")
        st.caption(
            "可輸入中文名、英文名或代號(例:台積電 / 2330 / Nvidia / NVDA)。"
            "系統會自動判斷台股/美股、抓該股最近約 6 個月的中英文新聞,"
            "再請 Gemini 產出研究員報告風格健診:① 新聞相關性;② 股價與籌碼動向;③ 基本面與推升動能(題材);"
            "④ 護城河與競爭(龍頭/對手/技術門檻/產業上中下游供應鏈);⑤ 估值與風險 + 觀察指標 + 長期持有研判。"
            "(部分數字為 AI 估算、非即時,僅供參考)"
        )
        term = st.text_input(
            "個股", key="stockq_term_input", placeholder="台積電 / 2330 / Nvidia / NVDA …"
        )
        has_key = ensure_gemini_key()
        if st.button(
            "🔍 ① 辨識個股並抓新聞",
            use_container_width=True,
            disabled=not has_key,
            help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
        ):
            if not term.strip():
                st.warning("請先輸入個股名稱或代號。")
            else:
                with st.spinner("辨識個股並抓取相關新聞中…"):
                    try:
                        tr = update_data.translate_stock_query(term.strip())
                        news = update_data.fetch_stock_query_news(
                            tr.get("query_en", ""), tr.get("ticker", ""),
                            tr.get("aliases"), tr.get("query_zh", term.strip()),
                            tr.get("zh_aliases"),
                        )
                        st.session_state["live_stockq_translation"] = tr
                        st.session_state["live_stockq_news"] = news
                        st.session_state["live_stockq_term"] = term.strip()
                        st.session_state.pop("live_stockq", None)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"抓取失敗:{exc}")
        if not has_key:
            render_key_hint()


def generate_live_stock_query() -> None:
    """第二步:對『已抓到的該股新聞』請 Gemini 做健診分析。"""
    tr = st.session_state.get("live_stockq_translation", {})
    news = st.session_state.get("live_stockq_news", [])
    term = st.session_state.get("live_stockq_term", tr.get("query_zh", ""))
    today = tz_utils.taiwan_today()
    st.session_state["live_stockq"] = update_data.get_stock_query_analysis(
        tr.get("query_zh", term), tr.get("query_en", ""),
        tr.get("ticker", ""), tr.get("market", ""), news, today,
    )
    st.session_state.pop("live_stockq_news", None)


# ---------------------------------------------------------------------------
# 新聞策略(貼一則新聞 → 首席策略師 → 台股 ETF 進場/持有/出場決策;互動、不存檔)
# ---------------------------------------------------------------------------

def render_news_strategy_panel() -> None:
    """輸入框:貼新聞 → Gemini 產生四階段台股 ETF 策略。"""
    with st.container(border=True):
        st.markdown("#### 📰 貼上新聞 / 時事")
        st.caption(
            "貼一則新聞或時事敘述,Gemini 以「首席投資策略師」角度做深度因果鏈推導,"
            "轉化為台股 ETF 的進場/持有/出場實戰決策(四階段:因果鏈 → 三大陣營 → ETF 佈局 → 持有出場)。"
            "ETF 代號為 AI 整理,務必自行核對即時行情與溢價率;僅供參考,非投資建議。"
        )
        text = st.text_area(
            "新聞文本", key="newsetf_text_input", height=180,
            placeholder="例:美國商務部宣布對先進製程設備擴大出口管制,衝擊 AI 晶片供應鏈…",
        )
        has_key = ensure_gemini_key()
        if st.button(
            "🧠 產生 ETF 策略分析",
            use_container_width=True, disabled=not has_key,
            help=None if has_key else "需先在 Streamlit Secrets 設定 GEMINI_API_KEY",
        ):
            if len(text.strip()) < 15:
                st.warning("請貼上較完整的新聞內容(至少一兩句)。")
            else:
                with st.spinner("Gemini 策略分析中(約 15–40 秒)…"):
                    try:
                        st.session_state["live_newsetf"] = update_data.get_news_etf_strategy(
                            text.strip(), tz_utils.taiwan_today())
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"分析失敗:{exc}")
        if not has_key:
            render_key_hint()


def _render_camp(title: str, items: list, name_key: str) -> None:
    """渲染一個陣營(受害/受惠/外資回補)。"""
    st.markdown(f"**{title}**")
    if not items:
        st.caption("—(本則新聞此陣營著墨不明顯)")
        return
    for it in items:
        name = (it.get(name_key) or "").strip()
        reason = (it.get("reason") or "").strip()
        st.markdown(f"- **{name}**:{reason}" if name else f"- {reason}")


def _render_etf_list(title: str, items: list) -> None:
    """渲染一組 ETF 建議(進攻/防守)。"""
    st.markdown(f"**{title}**")
    if not items:
        st.caption("—")
        return
    for e in items:
        tk = (e.get("ticker") or "").strip()
        nm = (e.get("name") or "").strip()
        logic = (e.get("logic") or "").strip()
        head = f"{nm}（{tk}）" if tk else (nm or "(代號待確認)")
        st.markdown(f"- **{head}**:{logic}")


def render_news_strategy(data: dict) -> None:
    """渲染新聞策略四階段。"""
    if data.get("news_summary"):
        st.info(data["news_summary"])
    if data.get("category"):
        st.caption(f"事件類型:{data['category']}")
    st.caption("AI 依新聞推導,ETF 代號與數字請自行核對即時行情/溢價率;僅供參考,非投資建議。")

    p1 = data.get("phase1_causal", {})
    st.subheader("① 新聞本質與因果鏈推導")
    if p1.get("core_turn"):
        st.markdown(f"**核心轉折:** {p1['core_turn']}")
    if p1.get("first_order"):
        st.markdown(f"**第一層效應:** {p1['first_order']}")
    if p1.get("horizon_nature"):
        st.markdown(f"**性質研判:** {p1['horizon_nature']}")

    p2 = data.get("phase2_camps", {})
    st.subheader("② 台股供應鏈三大陣營")
    _render_camp("🔴 直接衝擊 / 利空受害", p2.get("victims", []), "industry")
    _render_camp("🟢 直接受惠 / 定價權", p2.get("beneficiaries", []), "industry")
    _render_camp("🔵 外資回補 / 長線外溢", p2.get("foreign_reflow", []), "sector")

    p3 = data.get("phase3_etf", {})
    st.subheader("③ 台股 ETF 實戰佈局")
    _render_etf_list("⚔️ 進攻 / 順勢追擊", p3.get("offense", []))
    _render_etf_list("🛡️ 防守 / 利空低接", p3.get("defense", []))
    if p3.get("safety_check"):
        st.warning(f"⚠️ 即時安全檢視:{p3['safety_check']}")

    p4 = data.get("phase4_playbook", {})
    st.subheader("④ 持有週期與出場劇本")
    if p4.get("holding_period"):
        st.markdown(f"**預計持有週期:** {p4['holding_period']}")
    sigs = p4.get("exit_signals", [])
    if sigs:
        st.markdown("**出場 / 獲利了結訊號:**")
        for s in sigs:
            st.markdown(f"- {s}")

    if data.get("data_notes"):
        st.caption(data["data_notes"])


def render_stock_query(data: dict) -> None:
    head = data.get("query_zh", "—")
    ticker = data.get("ticker", "")
    col1, col2, col3 = st.columns(3)
    col1.metric("個股", head + (f"（{ticker}）" if ticker else ""))
    col2.metric("市場", data.get("market", "—"))
    col3.metric("英文名", data.get("query_en", "—"))
    if data.get("summary"):
        st.info(data["summary"])
    cap = mention_caption(data)
    if cap:
        st.caption(cap)
    if data.get("first_seen"):
        st.caption(
            f"🗓️ 相關新聞 {data['first_seen']} ～ {data.get('last_seen', '')}"
            f"，共 {data.get('news_count', 0)} 則(回溯約 6 個月,實際範圍受 RSS 限制)。"
        )
    st.caption("由 AI 依真實新聞整理,可能有誤,僅供參考,非投資建議。")

    # ① 與目前新聞的直接相關性
    st.subheader("① 與目前新聞的直接相關性")
    level = data.get("relevance_level", "")
    level_emoji = {"高": "🟢 高", "中": "🟡 中", "低": "⚪ 低"}.get(level, level)
    if level_emoji:
        st.markdown(f"**相關度:{level_emoji}**")
    points = data.get("relevance_points", [])
    if points:
        for p in points:
            st.markdown(f"- {p}")
    else:
        st.caption("新聞中未見對本檔的直接著墨。")

    # ② 股價與籌碼動向
    pc = data.get("price_chip") or {}
    if any(pc.get(k) for k in ("price_action", "chip_flow", "technical")):
        st.subheader("② 股價與籌碼動向")
        if pc.get("price_action"):
            st.markdown(f"**盤面/量能:** {pc['price_action']}")
        if pc.get("chip_flow"):
            st.markdown(f"**法人/籌碼:** {pc['chip_flow']}")
        if pc.get("technical"):
            st.markdown(f"**技術面:** {pc['technical']}")

    # ③ 基本面與推升動能
    st.subheader("③ 基本面與推升動能")
    if data.get("operating_performance"):
        st.markdown(f"**營運績效:** {data['operating_performance']}")
    catalysts = data.get("catalysts", [])
    if catalysts:
        st.markdown("**推升動能/題材:**")
        for cat in catalysts:
            if not isinstance(cat, dict):
                continue
            title = cat.get("title", "")
            detail = cat.get("detail", "")
            line = f"- **{title}**" + (f":{detail}" if detail else "")
            st.markdown(line)
    nature = data.get("rally_nature", "")
    nature_emoji = {
        "短期消息面": "⚡ 短期消息面",
        "基本面可持續": "🏗️ 基本面可持續",
        "資料不足判斷": "❓ 資料不足判斷",
    }.get(nature, nature)
    if nature_emoji:
        st.markdown(f"**上漲性質:** {nature_emoji}")
    if data.get("rally_reason"):
        st.write(data["rally_reason"])

    _render_evidence_news(data.get("evidence_news", []))

    # ④ 護城河與競爭
    st.subheader("④ 護城河與競爭")
    st.caption("本段含產業結構常識(非僅來自新聞),數字未必即時,僅供參考。")
    lead = data.get("is_leader", "")
    lead_emoji = {
        "龍頭": "👑 龍頭",
        "前段班": "🥈 前段班",
        "中後段": "📉 中後段",
        "資料不足": "❓ 資料不足",
    }.get(lead, lead)
    moat = data.get("moat_level", "")
    moat_emoji = {
        "高": "🟢 高", "中": "🟡 中", "低": "🔴 低", "資料不足": "❓ 資料不足",
    }.get(moat, moat)
    c1, c2 = st.columns(2)
    if lead_emoji:
        c1.metric("市場地位", lead_emoji)
    if moat_emoji:
        c2.metric("技術門檻(護城河)", moat_emoji)
    if data.get("leader_reason"):
        st.markdown(f"**地位依據:** {data['leader_reason']}")
    if data.get("moat_reason"):
        st.markdown(f"**護城河來源:** {data['moat_reason']}")

    competitors = data.get("competitors", [])
    if competitors:
        st.markdown("**主要競爭對手:**")
        for c in competitors:
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            tk = c.get("ticker", "")
            note = c.get("note", "")
            line = f"- {name}" + (f"（{tk}）" if tk else "")
            if note:
                line += f" — {note}"
            st.markdown(line)

    sc = data.get("supply_chain") or {}
    seg_labels = [("upstream", "上游"), ("midstream", "中游"), ("downstream", "下游")]
    if any(sc.get(k) for k, _ in seg_labels):
        st.markdown("**產業上中下游供應鏈:**")
        for key, label in seg_labels:
            rows = sc.get(key) or []
            if not rows:
                continue
            names = "、".join(
                (r.get("name", "") + (f"（{r.get('ticker','')}）" if r.get("ticker") else ""))
                + (f":{r.get('role','')}" if r.get("role") else "")
                for r in rows if isinstance(r, dict) and r.get("name")
            )
            if names:
                st.markdown(f"- **{label}**:{names}")

    # ⑤ 估值與風險
    val = data.get("valuation") or {}
    risks = data.get("risks", [])
    watch = data.get("watch_points", [])
    if any(val.get(k) for k in ("level", "logic", "peer_note")) or risks or watch:
        st.subheader("⑤ 估值與風險")
        vlevel = val.get("level", "")
        vlevel_emoji = {
            "偏高": "🔴 偏高", "合理": "🟢 合理", "偏低": "🟡 偏低", "資料不足": "❓ 資料不足",
        }.get(vlevel, vlevel)
        if vlevel_emoji:
            st.markdown(f"**估值水位:** {vlevel_emoji}")
        if val.get("logic"):
            st.markdown(f"**估值邏輯:** {val['logic']}")
        if val.get("peer_note"):
            st.markdown(f"**同業估值區間:** {val['peer_note']}")
        if risks:
            st.markdown("**主要風險:**")
            for r in risks:
                st.markdown(f"- ⚠️ {r}")
        if watch:
            st.markdown("**後續觀察指標:**")
            st.markdown("　".join(f"`{w}`" for w in watch))

    if data.get("long_term_view"):
        st.success(f"**長期持有研判:** {data['long_term_view']}")

    if data.get("data_notes"):
        st.caption(f"📌 數字來源:{data['data_notes']}")
    st.caption("⚠️ 本頁由 AI 自動整理新聞而成,部分數字為 AI 估算、非即時,可能有誤,僅供參考,非投資建議。")


# ---------------------------------------------------------------------------
# 戰略報告
# ---------------------------------------------------------------------------

def render_report(report: dict) -> None:
    # §2.4 新鮮度:報告歸屬日落後今日過久 → 顯式警告,不讓過期資料偽裝成最新。
    note = freshness.stale_note(report.get("report_date"), STALE_REPORT_DAYS, "戰略報告")
    if note:
        st.warning(note)
    col1, col2, col3 = st.columns(3)
    col1.metric("報告日期", report.get("report_date", "—"))
    col2.metric("分析主題", report.get("topic", "—"))
    col3.metric("白話文來源", report.get("dictionary_source", "—"))
    st.divider()

    st.header("📰 第一階段:原始情報彙整")
    span = report.get("news_span", {})
    if span.get("first_seen"):
        st.caption(
            f"🗓️ 取材新聞 {span['first_seen']} ～ {span.get('last_seen', '')}"
            f"，共 {span.get('news_count', 0)} 則(回溯約 6 個月,實際範圍受 RSS 限制)。"
        )
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
    st.caption("依「資金 / 徵才 / 政策 / 技術」四種訊號綜合評估,熱度 0–100。新聞來源同時含台灣與美股。")
    st.caption(SIX_MONTH_SOURCE_CAPTION)
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
            cap = mention_caption(t)
            if cap:
                st.caption(cap)

            us_stocks = t.get("us_stocks", [])
            tw_stocks = t.get("tw_stocks", [])
            if us_stocks or tw_stocks:
                scol1, scol2 = st.columns(2)
                with scol1:
                    st.markdown("**🇺🇸 美股代表**")
                    if us_stocks:
                        for s in us_stocks:
                            nm = s.get("name", "") if isinstance(s, dict) else str(s)
                            tk = s.get("ticker", "") if isinstance(s, dict) else ""
                            st.markdown(f"- {nm}" + (f"（{tk}）" if tk else ""))
                    else:
                        st.caption("(本次未列出)")
                with scol2:
                    st.markdown("**🇹🇼 台股代表**")
                    if tw_stocks:
                        for s in tw_stocks:
                            nm = s.get("name", "") if isinstance(s, dict) else str(s)
                            tk = s.get("ticker", "") if isinstance(s, dict) else ""
                            st.markdown(f"- {nm}" + (f"（{tk}）" if tk else ""))
                    else:
                        st.caption("(本次未列出)")

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

            _render_evidence_news(t.get("evidence_news", []))

    st.caption("⚠️ 趨勢評估由 AI 自動生成,非投資建議。新聞屬同步/落後訊號,請搭配資金與徵才等領先指標判讀。")


# ---------------------------------------------------------------------------
# ETF 持股反查
# ---------------------------------------------------------------------------

def render_price_update_panel(current_prices: dict) -> None:
    """透過 NAS 代理抓台股收盤價(供價位篩選);結果存 session,可下載。"""
    with st.expander(f"💰 股價資料（目前 {len(current_prices)} 檔)— 點此更新", expanded=not current_prices):
        st.caption("透過代理抓臺灣證交所(上市)＋櫃買中心(上櫃)當日收盤價,供『股價範圍』篩選使用。")
        proxy = ensure_proxy()
        auto_pr = st.session_state.get("auto_save_github", True)
        if st.button("🔄 更新台股收盤價", use_container_width=True, disabled=not proxy):
            with st.spinner("透過代理抓台股收盤價中…"):
                logs: list[str] = []
                try:
                    data = price_fetcher.fetch_prices(proxy=proxy, log=logs.append)
                    st.session_state["price_data_live"] = data
                    st.success(f"完成!取得 {len(data.get('prices', {}))} 檔收盤價。")
                    if auto_pr:
                        save_to_github("stock_prices.json", data, f"({len(data.get('prices', {}))} 檔)")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"抓取失敗:{exc}")
                if logs:
                    st.code("\n".join(logs))
        if not proxy:
            st.warning("未偵測到 PROXY_URL,無法抓股價。請先在 Streamlit Secrets 設定。")
        # 存檔區:常駐顯示
        live_pr = st.session_state.get("price_data_live")
        price_data = live_pr or price_fetcher.load_prices() or {}
        n_pr = len(price_data.get("prices", {}))
        st.markdown("**💾 存檔股價資料庫**")
        st.caption(
            f"將存入本回合抓到的 {n_pr} 檔收盤價。" if live_pr
            else f"尚未在本回合抓取;可先按上方「🔄 更新台股收盤價」,或直接存 repo 既有的 {n_pr} 檔。"
        )
        _prices_str = json.dumps(price_data, ensure_ascii=False, indent=2)
        render_github_save("stock_prices.json", _prices_str, key="prices")
        st.download_button(
            "⬇️ 下載 stock_prices.json(備援:手動上傳)",
            data=_prices_str,
            file_name="stock_prices.json",
            mime="application/json",
        )


def render_etf_lookup(data: dict | None = None) -> None:
    from_cache = data is None  # 未指定 → 用 etf_data 快取的檔案來源(可重用反查快取)
    if from_cache:
        data = etf_data.get_holdings()
    if not data:
        st.warning("找不到 `etf_holdings.json` 或格式有誤。請確認檔案存在且為合法 JSON。")
        return

    etfs = data.get("etfs", {})
    # 檔案來源走快取的反查;即時抓取(live)的資料則即時反查,確保剛抓的馬上反映
    rows = etf_data.get_reverse_index() if from_cache else etf_holdings.reverse_index(data)

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
    if prices:  # §2.4 股價過期警示:抓取日落後過久 → 價位篩選恐用舊價,顯式提醒不阻斷
        pnote = freshness.stale_note(price_data.get("as_of"), PRICE_STALE_DAYS, "股價")
        if pnote:
            st.warning(pnote + "　價位範圍篩選結果僅供參考,請重新抓取最新收盤價。")
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
# 房市觀察
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def load_taiwan_geojson() -> dict | None:
    """讀取內建的台灣縣市 GeoJSON(離線、已正名為官方『臺』與桃園市)。"""
    if not GEOJSON_PATH.exists():
        return None
    try:
        return json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# 交通標籤配色(供長條圖額外標出高鐵/自強號縣市)
TRANSPORT_COLORS = {
    "高鐵+自強號": "#d62728",  # 紅:最便利
    "高鐵": "#ff7f0e",        # 橘
    "自強號": "#1f77b4",      # 藍
    "無軌道": "#9e9e9e",      # 灰
}


@st.cache_data(show_spinner=False)
def county_centroids() -> dict:
    """從 GeoJSON 估各縣市代表點(取點數最多的主多邊形外環平均),供地圖標記。"""
    geo = load_taiwan_geojson()
    out: dict[str, tuple] = {}
    if not geo:
        return out
    for f in geo["features"]:
        name = f["properties"]["name"]
        geom = f["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        best, best_len = None, -1
        for poly in polys:
            ring = poly[0]
            if len(ring) > best_len:
                best, best_len = ring, len(ring)
        if best:
            xs = [p[0] for p in best]
            ys = [p[1] for p in best]
            out[name] = (sum(xs) / len(xs), sum(ys) / len(ys))
    return out


def _price_values(prices: dict, kind: str) -> dict:
    """從房價資料取 {縣市: 每坪均價}(kind: 'resale' 成屋 / 'presale' 預售)。"""
    out: dict[str, float] = {}
    for county, info in (prices.get("counties") or {}).items():
        v = (info.get(kind) or {}).get("avg_ping_wan")
        if isinstance(v, (int, float)):
            out[county] = v
    return out


def _heat_values(analysis: dict) -> dict:
    """從 Gemini 分區標記取 {縣市: 熱度分}。"""
    out: dict[str, float] = {}
    for r in analysis.get("regions") or []:
        c, h = r.get("county"), r.get("heat_score")
        if c and isinstance(h, (int, float)):
            out[c] = h
    return out


def render_taiwan_choropleth(values: dict, legend: str, scale: str,
                             marker_counties: set | None = None,
                             marker_label: str = "高鐵站",
                             midpoint: float | None = None) -> None:
    """用 plotly 畫台灣縣市互動 choropleth;可在指定縣市疊★標記;沒裝 plotly 時退回表格。

    midpoint 不為 None 時(如年增率)以該值為發散色階中點(紅正/藍負)。
    """
    df = pd.DataFrame(
        [{"縣市": c, legend: v} for c, v in values.items()]
    ).sort_values(legend, ascending=False)
    if df.empty:
        st.info("尚無可上色的資料。")
        return
    geo = load_taiwan_geojson()
    try:
        import plotly.express as px
        import plotly.graph_objects as go
    except Exception:  # noqa: BLE001 — 未安裝 plotly:退回表格 + 長條圖
        st.caption("（未安裝 plotly,以表格替代地圖)")
        st.bar_chart(df.set_index("縣市"))
        st.dataframe(df, use_container_width=True, hide_index=True)
        return
    if not geo:
        st.warning("找不到 taiwan_counties.geo.json,改用長條圖顯示。")
        st.bar_chart(df.set_index("縣市"))
        return
    px_kwargs = {"color_continuous_scale": scale, "hover_data": {legend: ":.1f"}}
    if midpoint is not None:
        px_kwargs["color_continuous_midpoint"] = midpoint
    fig = px.choropleth(
        df, geojson=geo, locations="縣市", featureidkey="properties.name",
        color=legend, **px_kwargs,
    )
    # 鋪一層「全台 22 縣市」淺灰底圖:只標少數縣市時仍看得到台灣完整輪廓
    # (否則被提到的縣市會孤零零浮在白底上,像地圖不見了)。全有色時被上層蓋住,無影響。
    all_names = [f["properties"].get("name") for f in geo.get("features", [])
                 if f.get("properties", {}).get("name")]
    if all_names:
        fig.add_trace(go.Choropleth(
            geojson=geo, locations=all_names, featureidkey="properties.name",
            z=[0] * len(all_names), showscale=False,
            colorscale=[[0, "#ececec"], [1, "#ececec"]],
            marker_line_color="white", marker_line_width=0.5,
            hoverinfo="skip",
        ))
        # 移到最底層(有色縣市與 ★ 標記疊在上面)
        fig.data = (fig.data[-1],) + tuple(fig.data[:-1])
    # ★ 在指定縣市(高鐵/自強號)疊上標記,於地圖上額外標出
    if marker_counties:
        cents = county_centroids()
        pts = [(c, cents[c]) for c in marker_counties if c in cents]
        if pts:
            fig.add_trace(go.Scattergeo(
                lon=[p[1][0] for p in pts], lat=[p[1][1] for p in pts],
                text=[p[0] for p in pts], mode="markers", name=marker_label,
                marker={"size": 11, "color": "#111", "symbol": "star",
                        "line": {"width": 1, "color": "white"}},
                hovertemplate="%{text}<br>" + marker_label + "<extra></extra>",
            ))
    fig.update_geos(fitbounds="locations", visible=False)
    fig.update_layout(margin={"r": 0, "t": 0, "l": 0, "b": 0}, height=560,
                      dragmode=False,
                      legend={"yanchor": "top", "y": 0.98, "xanchor": "left", "x": 0.02})
    st.plotly_chart(fig, use_container_width=True)


def render_house_price_panel() -> None:
    """透過 NAS 代理抓內政部實價登錄,建立各縣市每坪房價庫 + 存檔。"""
    with st.container(border=True):
        st.markdown("#### 🛰️ 透過 NAS 代理更新各縣市房價(內政部實價登錄)")
        st.caption("經 PROXY_URL 代理抓內政部最新季別實價登錄,彙整各縣市『成屋/預售屋』"
                   "每坪均價(萬元/坪),並保留逐筆成交當佐證。房價為政府事實資料,非 AI 推測。")
        proxy = ensure_proxy()
        if not proxy:
            st.warning("未偵測到 PROXY_URL。實價登錄站會擋境外 IP,請先在 Streamlit Secrets 設定代理。")
        auto = st.session_state.get("auto_save_github", True)
        if st.button("🔄 立即抓取 / 更新各縣市房價", use_container_width=True, disabled=not proxy):
            with st.spinner("透過代理抓實價登錄季度資料中…(下載+解析約數十秒)"):
                logs: list[str] = []
                try:
                    data = housing_fetcher.fetch_house_prices(proxy=proxy, log=logs.append)
                    st.session_state["house_prices_live"] = data
                    st.success(f"完成!季別 {data.get('season', '—')},共 "
                               f"{len(data.get('counties', {}))} 縣市。")
                    if auto:
                        save_to_github("house_prices.json", data, f"(季別 {data.get('season', '')})")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"抓取失敗:{exc}")
                if logs:
                    with st.expander("📋 抓取明細"):
                        st.code("\n".join(logs))
        # 存檔區:常駐
        st.divider()
        st.markdown("**💾 存檔房價資料庫**")
        live = st.session_state.get("house_prices_live")
        price_data = live or housing_fetcher.load_house_prices() or {}
        n_c = len(price_data.get("counties", {}))
        st.caption(
            f"將存入本回合抓到的最新房價(季別 {price_data.get('season', '—')},{n_c} 縣市)。"
            if live else
            f"尚未在本回合抓取;可先按上方「🔄 立即抓取」,或直接存 repo 既有的 {n_c} 縣市。"
        )
        _str = json.dumps(price_data, ensure_ascii=False, indent=2)
        render_github_save("house_prices.json", _str, key="house_prices")
        st.download_button(
            "⬇️ 下載 house_prices.json(備援:手動上傳)",
            data=_str, file_name="house_prices.json", mime="application/json",
        )


def render_housing_live_panel() -> None:
    """房市觀察第一步:只抓房市新聞(冷熱/政策判讀另由 Gemini 按鈕觸發)。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時產生(免等每日排程)")
        st.caption("從房市新聞判讀預售/成屋冷熱、整理打房政策,並標出各縣市。"
                   "流程:① 先抓房市新聞 → ② 看過後再按 Gemini 判讀。")
        if st.button("🔄 ① 立即抓取房市新聞", use_container_width=True):
            with st.spinner("抓取房市新聞中…"):
                try:
                    st.session_state["live_housing_news"] = update_data.fetch_housing_news()
                    st.session_state.pop("live_housing", None)
                except Exception as exc:  # noqa: BLE001
                    st.session_state["live_housing_news"] = []
                    st.error(f"抓取失敗:{exc}")


def generate_live_housing() -> None:
    """房市觀察第二步:對『已抓到的房市新聞』+ 房價參考請 Gemini 判讀。"""
    news = st.session_state.get("live_housing_news", [])
    prices = st.session_state.get("house_prices_live") or housing_fetcher.load_house_prices()
    history = st.session_state.get("house_history_live") or housing_fetcher.load_house_price_history()
    today = tz_utils.taiwan_today()
    data = update_data.get_housing_analysis(news, prices, today, history)
    data["raw_news"] = news
    st.session_state["live_housing"] = data
    st.session_state.pop("live_housing_news", None)


def render_county_price_bar(values: dict, kind_label: str) -> None:
    """圖表①:各縣市每坪均價長條圖,依交通標籤(高鐵/自強號)上色額外標出。"""
    st.markdown(f"**📊 各縣市每坪均價長條圖（{kind_label}）**")
    rows = [
        {"縣市": c, "每坪(萬元)": v, "交通": housing_fetcher.transport_tag(c)}
        for c, v in values.items()
    ]
    df = pd.DataFrame(rows).sort_values("每坪(萬元)", ascending=False)
    try:
        import plotly.express as px
    except Exception:  # noqa: BLE001 — 退回 streamlit 內建長條圖(無法上色)
        st.bar_chart(df.set_index("縣市")["每坪(萬元)"])
        st.caption("（未安裝 plotly,無法依交通上色)")
        return
    fig = px.bar(
        df, x="縣市", y="每坪(萬元)", color="交通",
        color_discrete_map=TRANSPORT_COLORS,
        category_orders={"交通": list(TRANSPORT_COLORS.keys())},
    )
    fig.update_layout(height=420, margin={"r": 0, "t": 10, "l": 0, "b": 0},
                      xaxis_title="", legend_title="軌道交通")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("顏色標示交通便利度:🔴高鐵+自強號　🟠高鐵　🔵自強號　⚪無軌道。")


def render_transport_compare(values: dict) -> None:
    """交通便利(有高鐵/自強號)vs 無軌道縣市的平均每坪對比。"""
    rail = [v for c, v in values.items() if housing_fetcher.has_rail_transport(c)]
    norail = [v for c, v in values.items() if not housing_fetcher.has_rail_transport(c)]
    hsr = [v for c, v in values.items() if c in housing_fetcher.HSR_COUNTIES]
    cols = st.columns(3)
    cols[0].metric("🚄 有高鐵縣市 均價",
                   f"{sum(hsr) / len(hsr):.1f}" if hsr else "—",
                   help="設有高鐵站的縣市,每坪均價平均(萬元)")
    cols[1].metric("🚆 有軌道(高鐵/自強號)均價",
                   f"{sum(rail) / len(rail):.1f}" if rail else "—",
                   help="有高鐵站或自強號停靠的縣市")
    cols[2].metric("🚫 無軌道縣市 均價",
                   f"{sum(norail) / len(norail):.1f}" if norail else "—",
                   help="南投與離島等無台鐵/高鐵的縣市")


def render_house_price_history_panel() -> None:
    """圖表②:單一縣市不同年份的每坪均價折線圖(需歷年房價資料)。"""
    st.subheader("📈 單一縣市歷年每坪均價")
    # 抓取 / 更新歷年房價(較久,獨立按鈕)
    with st.expander("🛰️ 抓取 / 更新歷年房價(透過代理,較久)", expanded=False):
        st.caption("逐季抓近數年實價登錄,彙整各縣市『各西元年』每坪均價。下載量較大,請耐心等候。")
        proxy = ensure_proxy()
        years = st.slider("回溯年數", 2, 8, 5, key="house_hist_years")
        if not proxy:
            st.warning("未偵測到 PROXY_URL,無法抓取。")
        if st.button("🔄 抓取歷年房價", use_container_width=True, disabled=not proxy):
            with st.spinner(f"透過代理抓近 {years} 年實價登錄中…(可能數分鐘)"):
                logs: list[str] = []
                try:
                    data = housing_fetcher.fetch_house_price_history(
                        proxy=proxy, log=logs.append, years_back=years)
                    st.session_state["house_history_live"] = data
                    st.success(f"完成!涵蓋年份 {data.get('years', [])},{len(data.get('counties', {}))} 縣市。")
                    if st.session_state.get("auto_save_github", True):
                        save_to_github("house_price_history.json", data,
                                       f"(近 {years} 年)")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"抓取失敗:{exc}")
                if logs:
                    with st.expander("📋 抓取明細"):
                        st.code("\n".join(logs))
        # 存檔區
        hist_now = st.session_state.get("house_history_live") or housing_fetcher.load_house_price_history() or {}
        _hs = json.dumps(hist_now, ensure_ascii=False, indent=2)
        render_github_save("house_price_history.json", _hs, key="house_history")
        st.download_button("⬇️ 下載 house_price_history.json", data=_hs,
                           file_name="house_price_history.json", mime="application/json")

    history = st.session_state.get("house_history_live") or housing_fetcher.load_house_price_history()
    counties = (history or {}).get("counties") or {}
    if not counties:
        st.info("尚無歷年房價資料。請先在上方「🛰️ 抓取 / 更新歷年房價」抓取(需 PROXY_URL)。")
        return

    sel = st.selectbox("選擇縣市", sorted(counties.keys()), key="house_hist_county")
    tag = housing_fetcher.transport_tag(sel)
    block = counties.get(sel, {})
    # 組成 {年: {成屋, 預售}}
    years = history.get("years") or sorted(
        {y for k in ("resale", "presale") for y in (block.get(k) or {})}
    )
    rows = []
    for y in years:
        rows.append({
            "年份": y,
            "成屋": (block.get("resale") or {}).get(y),
            "預售": (block.get("presale") or {}).get(y),
        })
    df = pd.DataFrame(rows).set_index("年份")
    st.caption(f"{sel}（{tag}）　單位:萬元/坪　資料:內政部實價登錄")
    try:
        import plotly.express as px
        melted = df.reset_index().melt("年份", var_name="市場", value_name="每坪(萬元)")
        long = melted.dropna()
        dropped = len(melted) - len(long)
        if dropped:  # 顯式揭露丟棄筆數,不靜默
            st.caption(f"（{dropped} 個年份點無資料,折線略過未連接）")
        fig = px.line(long, x="年份", y="每坪(萬元)", color="市場", markers=True,
                      color_discrete_map={"成屋": "#1f77b4", "預售": "#d62728"})
        fig.update_layout(height=380, margin={"r": 0, "t": 10, "l": 0, "b": 0})
        st.plotly_chart(fig, use_container_width=True)
    except Exception:  # noqa: BLE001
        st.line_chart(df)

    # 圖表③延伸:各縣市房價年增率(YoY)地圖 + 排行
    render_house_price_yoy(history)


def render_house_price_yoy(history: dict) -> None:
    """各縣市房價年增率(YoY):最新年 vs 前一年每坪均價變化(發散色階地圖 + 排行)。"""
    counties = history.get("counties") or {}
    all_years = sorted(
        {y for c in counties.values() for k in c.values() for y in k}, key=int
    )
    if len(all_years) < 2:
        return  # 不足兩年不畫年增率
    st.divider()
    st.subheader("📉 各縣市房價年增率(YoY)")
    kind_label = st.radio("市場", ["成屋", "預售屋"], horizontal=True, key="yoy_kind")
    kind = "resale" if kind_label == "成屋" else "presale"
    y_cur, y_prev = all_years[-1], all_years[-2]
    rows = []
    for county, block in counties.items():
        m = block.get(kind) or {}
        pv, cv = m.get(y_prev), m.get(y_cur)
        if isinstance(pv, (int, float)) and pv and isinstance(cv, (int, float)):
            rows.append({
                "縣市": county, "交通": housing_fetcher.transport_tag(county),
                f"{y_prev}每坪": pv, f"{y_cur}每坪": cv,
                "YoY%": numutil.pct_change(cv, pv, 1),
            })
    if not rows:
        st.info(f"{kind_label} {y_prev}→{y_cur} 資料不足,無法計算年增率。")
        return
    st.caption(f"{kind_label}:{y_prev} → {y_cur} 每坪均價變化(🔴上漲 / 🔵下跌;★=高鐵縣市)。")
    values = {r["縣市"]: r["YoY%"] for r in rows}
    render_taiwan_choropleth(values, legend="YoY%", scale="RdBu_r", midpoint=0,
                             marker_counties=housing_fetcher.HSR_COUNTIES,
                             marker_label="高鐵站")
    st.dataframe(
        sorted(rows, key=lambda r: r["YoY%"], reverse=True),
        use_container_width=True, hide_index=True,
    )


def render_housing_price_map() -> None:
    """各縣市每坪房價地圖(成屋/預售切換)+ 排行表 + 逐筆佐證。"""
    prices = st.session_state.get("house_prices_live") or housing_fetcher.load_house_prices()
    if not prices or not prices.get("counties"):
        st.info("尚無房價資料。請先在上方「🛰️ 透過 NAS 代理更新各縣市房價」抓取(需 PROXY_URL)。")
        return

    st.subheader("🗺️ 各縣市每坪房價地圖")
    st.caption(f"資料來源:內政部實價登錄　季別:{prices.get('season', '—')}　"
               f"單位:{prices.get('unit', '萬元/坪')}　|　{prices.get('as_of', '')}")
    kind_label = st.radio("選擇市場", ["成屋(中古/新成屋)", "預售屋"], horizontal=True, key="house_map_kind")
    kind = "resale" if kind_label.startswith("成屋") else "presale"
    values = _price_values(prices, kind)
    if not values:
        st.info(f"本季{kind_label}無足夠住宅成交資料可上色。")
        return
    st.caption("地圖上★ = 設有高鐵站的縣市(交通便利,額外標出)。")
    render_taiwan_choropleth(values, legend="每坪(萬元)", scale="OrRd",
                             marker_counties=housing_fetcher.HSR_COUNTIES,
                             marker_label="高鐵站")

    # 排行表(含交通標籤)
    counties = prices.get("counties", {})
    st.markdown("**📋 各縣市每坪房價排行(萬元/坪)**")
    st.dataframe(
        [
            {
                "縣市": c,
                "交通": housing_fetcher.transport_tag(c),
                "成屋每坪": (counties[c].get("resale") or {}).get("avg_ping_wan"),
                "成屋中位數": (counties[c].get("resale") or {}).get("median_ping_wan"),
                "成屋筆數": (counties[c].get("resale") or {}).get("count"),
                "預售每坪": (counties[c].get("presale") or {}).get("avg_ping_wan"),
                "預售筆數": (counties[c].get("presale") or {}).get("count"),
            }
            for c in sorted(
                counties,
                key=lambda c: (counties[c].get(kind) or {}).get("avg_ping_wan") or 0,
                reverse=True,
            )
        ],
        use_container_width=True, hide_index=True,
    )

    # 圖表 1:各縣市每坪均價長條圖(依交通標籤上色)
    render_county_price_bar(values, kind_label)
    # 交通便利 vs 無軌道 均價對比
    render_transport_compare(values)

    # 當期逐筆佐證(實價登錄原始成交)— 與當期房價同區,放在進入歷年趨勢之前
    with st.expander("🔍 逐筆成交佐證(實價登錄原始資料)"):
        sel = st.selectbox("選擇縣市", list(counties.keys()), key="house_sample_county")
        block = counties.get(sel, {})
        for kkind, klabel in (("resale", "成屋"), ("presale", "預售屋")):
            samples = (block.get(kkind) or {}).get("samples") or []
            if samples:
                st.markdown(f"**{klabel}近期成交（{len(samples)} 筆樣本）**")
                st.dataframe(
                    [
                        {"行政區": s.get("district", ""), "型態": s.get("type", ""),
                         "每坪(萬)": s.get("ping_wan"), "總價(萬)": s.get("total_wan"),
                         "交易日": s.get("date", ""), "門牌": s.get("address", "")}
                        for s in samples
                    ],
                    use_container_width=True, hide_index=True,
                )
    st.caption("⚠️ 每坪均價由實價登錄住宅成交(房地,排除純車位)即時彙整,可能與其他統計口徑略有差異;僅供參考,非投資建議。")

    # 圖表 2:單一縣市歷年每坪均價 + 年增率(YoY)— 進入多年趨勢
    st.divider()
    render_house_price_history_panel()


def render_housing(analysis: dict | None) -> None:
    """房市觀察主畫面:房價地圖 +(若有)Gemini 冷熱/政策/分區判讀。"""
    # 1) 房價地圖(真實資料,獨立於 AI 判讀)
    render_housing_price_map()
    st.divider()

    if not analysis:
        st.info("尚無房市冷熱/政策判讀。可用上方「⚡ 即時產生」抓房市新聞後請 Gemini 判讀。")
        return

    # 2) 整體氛圍 + 預售/成屋冷熱
    st.subheader("🌡️ 房市冷熱判讀")
    overall = analysis.get("overall_sentiment", "—")
    emoji, _ = HOUSING_SENTIMENT_STYLE.get(overall, ("", "info"))
    st.metric("整體氛圍", f"{emoji} {overall}")
    if analysis.get("overall_summary"):
        st.caption(analysis["overall_summary"])
    c1, c2 = st.columns(2)
    for col, key, title in ((c1, "presale_market", "🏗️ 預售屋市場"),
                            (c2, "resale_market", "🏠 成屋 / 中古屋市場")):
        m = analysis.get(key) or {}
        s = m.get("sentiment", "—")
        e, _ = HOUSING_SENTIMENT_STYLE.get(s, ("", "info"))
        with col:
            with st.container(border=True):
                st.markdown(f"**{title}**　{e} {s}")
                st.caption(m.get("note", ""))

    # 3) 分區冷熱地圖(Gemini 熱度分)
    heat = _heat_values(analysis)
    if heat:
        st.subheader("🗺️ 各縣市新聞冷熱地圖")
        st.caption("依房市新聞判讀的相對熱度(0–100,越紅越熱);只標出新聞有提到的縣市。")
        render_taiwan_choropleth(heat, legend="新聞熱度", scale="RdYlBu_r")
        regions = sorted(analysis["regions"], key=lambda r: r.get("heat_score", 0), reverse=True)
        st.dataframe(
            [{"縣市": r.get("county", ""), "傾向": r.get("sentiment", ""),
              "熱度": r.get("heat_score", ""), "重點": r.get("note", "")} for r in regions],
            use_container_width=True, hide_index=True,
        )

    # 4) 打房政策
    policy = analysis.get("policy") or []
    if policy:
        st.subheader("🏛️ 打房政策與信用管制")
        for p in policy:
            with st.container(border=True):
                st.markdown(f"**{p.get('title', '')}**")
                st.write(p.get("impact", ""))

    # 5) 佐證新聞
    _render_evidence_news(analysis.get("evidence_news") or analysis.get("raw_news") or [])

    # 6) 🧠 Gemini AI 買方總結(放最下方:綜合整頁房價/趨勢/冷熱/政策/新聞)
    render_housing_ai_summary(analysis.get("ai_summary"))

    st.caption("⚠️ 冷熱與政策判讀由 AI 自動整理新聞而成,房價為實價登錄事實資料;僅供參考,非投資建議。")


# 買方影響配色
BUYER_IMPACT_STYLE = {
    "偏好": ("🟢 對買方偏有利", "success"),
    "中性": ("🟡 對買方中性", "info"),
    "偏壞": ("🔴 對買方偏不利", "error"),
}


def render_housing_ai_summary(ai_summary) -> None:
    """頁面最下方的 Gemini AI 買方總結(支援新版結構化 dict 與舊版單句字串)。"""
    if not ai_summary:
        return
    st.divider()
    st.subheader("🧠 Gemini AI 房市總結(買方視角)")
    st.caption("綜合本頁所有資料(各縣市房價、歷年趨勢、新聞冷熱、打房政策、最新新聞)由 Gemini 判讀。")

    # 向後相容:舊資料 ai_summary 是單句字串
    if isinstance(ai_summary, str):
        st.info(ai_summary)
        return

    impact = ai_summary.get("buyer_impact", "")
    for key, (label, _) in BUYER_IMPACT_STYLE.items():
        if key in str(impact):
            st.markdown(f"#### {label}")
            break

    blocks = [
        ("📈 未來房市趨勢", ai_summary.get("future_trend")),
        ("🏛️ 房市政策的轉變", ai_summary.get("policy_shift")),
        ("🛒 對買方的影響", ai_summary.get("buyer_advice")),
    ]
    for title, body in blocks:
        if body:
            with st.container(border=True):
                st.markdown(f"**{title}**")
                st.write(body)

    # 長期結構性因子(人口/少子化、餘屋供給、購屋負擔)— 與短期動能並陳
    structural = ai_summary.get("structural_factors") or []
    if structural:
        with st.container(border=True):
            st.markdown("**🧩 長期結構性因子(人口 / 供給 / 負擔)**")
            for f in structural:
                st.markdown(f"- {f}")

    regs = ai_summary.get("regulations") or []
    if regs:
        st.markdown("**📜 相關法規 / 措施**")
        st.markdown("　".join(f"`{r}`" for r in regs))

    if ai_summary.get("overview"):
        st.info(ai_summary["overview"])


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 整合 4 大頁(台股 / 美股 / 全球 / 台灣房市):AI 融合總結 + 一頁展開所有面板
#   讀唯讀的每日排程資料直接攤開;互動工具(健診/新聞策略/人物追蹤/ETF)收進 expander;
#   各頁最上方提供「AI 今日總結」(按鈕觸發、當次 session 依日期快取,避免每次重算)。
# ---------------------------------------------------------------------------

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


# ── 唯讀每日面板(直接攤開;即時重抓收進 expander)──────────────────────────
def sec_report() -> None:
    st.subheader("🌐 戰略報告")
    with st.expander("⚡ 即時重新抓取 / 產生戰略報告"):
        render_live_panel()
        if "live_news" in st.session_state and not st.session_state.get("live_report"):
            news = st.session_state["live_news"]
            if news:
                st.success(f"已抓到 {len(news)} 則真實外電,確認後再請 Gemini 分析:")
                if st.button("🧠 ② 用 Gemini 產生戰略分析 + 白話文", key="rpt_step2",
                             disabled=not ensure_gemini_key()):
                    with st.spinner("Gemini 分析中…"):
                        try:
                            generate_live_report(); st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"產生報告失敗:{exc}")
                render_news_cards(news)
            else:
                st.info("這次沒抓到新聞,稍後再試或調整關鍵字。")
    live = st.session_state.get("live_report")
    if live:
        st.success("⚡ 以下為剛剛即時產生的報告(尚未存檔)。")
        render_report(live)
        return
    multi = load_json(REPORTS_MULTI_PATH)
    multi_reports = (multi or {}).get("reports") or []
    if len(multi_reports) > 1:
        st.caption(f"📚 本日含 {len(multi_reports)} 個主題報告,可切換:")
        idx = st.selectbox(
            "選擇主題", range(len(multi_reports)),
            format_func=lambda i: multi_reports[i].get("topic", f"主題 {i + 1}"),
            key="macro_topic_pick",
        )
        render_report(multi_reports[idx]); return
    report = pick_report(REPORT_PATH, ARCHIVE_DIR)
    if report is None:
        st.info("尚無每日戰略報告。可用上方『即時抓取』取得。")
        return
    render_report(report)


def sec_trends() -> None:
    st.subheader("🔥 趨勢雷達 — 現在最紅的產業")
    with st.expander("⚡ 即時重新產生趨勢雷達"):
        render_trend_live_panel()
        if "live_trend_news" in st.session_state and not st.session_state.get("live_trends"):
            news = st.session_state["live_trend_news"]
            if news:
                st.success(f"已抓到 {len(news)} 則產業新聞:")
                if st.button("🧠 ② 用 Gemini 產生趨勢雷達", key="trd_step2",
                             disabled=not ensure_gemini_key()):
                    with st.spinner("Gemini 排名打分中…"):
                        try:
                            generate_live_trends(); st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"產生趨勢雷達失敗:{exc}")
                render_news_cards(news)
    live = st.session_state.get("live_trends")
    if live:
        st.success("⚡ 以下為剛剛即時產生的趨勢雷達。")
        render_trends(live); return
    history = load_trend_history(TRENDS_ARCHIVE_DIR)
    if history is not None and len(history.index) >= 2:
        st.caption("📈 產業熱度趨勢(歷史):哪條線持續往上,就是動能最強的產業。")
        st.line_chart(history)
    data = pick_report(TRENDS_PATH, TRENDS_ARCHIVE_DIR)
    if data is None:
        st.info("尚無趨勢雷達存檔。可用上方『即時產生』取得。")
        return
    render_trends(data)


def sec_tw_stocks() -> None:
    st.subheader("📈 台股觀察 — 值得關注的台股標的")
    with st.expander("⚡ 即時重新抓取台股觀察"):
        render_stock_live_panel()
        if "live_stock_news" in st.session_state and not st.session_state.get("live_stocks"):
            news = st.session_state["live_stock_news"]
            if news:
                st.success(f"已抓到 {len(news)} 則財經新聞:")
                if st.button("🧠 ② 用 Gemini 整理台股標的", key="tws_step2",
                             disabled=not ensure_gemini_key()):
                    with st.spinner("Gemini 整理台股標的中…"):
                        try:
                            generate_live_stocks(); st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"整理台股標的失敗:{exc}")
                render_news_cards(news)
    data = st.session_state.get("live_stocks") or pick_report(STOCKS_PATH, STOCKS_ARCHIVE_DIR)
    if data is None:
        st.info("尚無台股觀察存檔。可用上方『即時產生』取得。")
        return
    render_stocks(data)


def sec_us_stocks() -> None:
    st.subheader("📈 美股觀察 — 值得關注的美股標的")
    with st.expander("⚡ 即時重新抓取美股觀察"):
        render_us_stock_live_panel()
        if "live_us_stock_news" in st.session_state and not st.session_state.get("live_us_stocks"):
            news = st.session_state["live_us_stock_news"]
            if news:
                st.success(f"已抓到 {len(news)} 則財經新聞:")
                if st.button("🧠 ② 用 Gemini 整理美股標的", key="uss_step2",
                             disabled=not ensure_gemini_key()):
                    with st.spinner("Gemini 整理美股標的中…"):
                        try:
                            generate_live_us_stocks(); st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"整理美股標的失敗:{exc}")
                render_news_cards(news)
    data = st.session_state.get("live_us_stocks") or pick_report(US_STOCKS_PATH, US_STOCKS_ARCHIVE_DIR)
    if data is None:
        st.info("尚無美股觀察存檔。可用上方『即時產生』取得。")
        return
    render_us_stocks(data)


def sec_intl() -> None:
    st.subheader("🌏 國際盤預警(盤前) — 美股/台指期夜盤大跌的時間差訊號")
    with st.expander("⚡ 即時重新抓取國際盤"):
        render_intl_alert_live_panel()
        if "live_intl_quotes" in st.session_state and not st.session_state.get("live_intl_alert"):
            quotes_doc = st.session_state["live_intl_quotes"]
            st.caption(f"報價時間:{quotes_doc.get('as_of', '—')} · 真實市場報價(Yahoo Finance)")
            render_index_quotes(quotes_doc.get("quotes", {}))
            if st.button("🧠 ② 用 Gemini 解讀利空原因 + 對台股影響", key="intl_step2",
                         disabled=not ensure_gemini_key()):
                with st.spinner("Gemini 解讀中…"):
                    try:
                        generate_live_intl_alert(); st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"解讀失敗:{exc}")
    data = st.session_state.get("live_intl_alert") or pick_report(
        INTL_ALERT_PATH, INTL_ALERT_ARCHIVE_DIR)
    if data is None:
        st.info("尚無國際盤預警存檔。可用上方『即時產生』取得。")
        return
    render_intl_alert(data)
    render_confluence(data)


def sec_chip() -> None:
    st.subheader("📊 法人籌碼 — 三大法人買賣超(事後驗證真實賣壓)")
    data = pick_report(CHIP_PATH, CHIP_ARCHIVE_DIR)
    if data is None:
        st.info("尚無三大法人籌碼存檔。每日排程會自動更新。")
        return
    render_chip(data)


def sec_housing() -> None:
    render_house_price_panel()
    st.subheader("🏠 房市觀察 — 預售/成屋冷熱、打房政策與各縣市房價")
    with st.expander("⚡ 即時重新抓取房市判讀"):
        render_housing_live_panel()
        if "live_housing_news" in st.session_state and not st.session_state.get("live_housing"):
            news = st.session_state["live_housing_news"]
            if news:
                st.success(f"已抓到 {len(news)} 則房市新聞:")
                if st.button("🧠 ② 用 Gemini 判讀房市冷熱 + 打房政策", key="hou_step2",
                             disabled=not ensure_gemini_key()):
                    with st.spinner("Gemini 判讀中…"):
                        try:
                            generate_live_housing(); st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"產生房市觀察失敗:{exc}")
                render_news_cards(news)
    live = st.session_state.get("live_housing")
    render_housing(live or pick_report(HOUSING_PATH, HOUSING_ARCHIVE_DIR))


# ── 互動工具(收進 expander)────────────────────────────────────────────────
def tool_stock_query() -> None:
    render_stock_query_panel()
    if st.session_state.get("live_stockq"):
        render_stock_query(st.session_state["live_stockq"]); return
    if "live_stockq_news" in st.session_state:
        tr = st.session_state.get("live_stockq_translation", {})
        news = st.session_state["live_stockq_news"]
        ticker = tr.get("ticker", "")
        st.info(f"🔤 辨識為:**{tr.get('query_zh', '')}**"
                + (f"（{ticker}）" if ticker else "")
                + f"　·　{tr.get('market', '')}　·　英文:{tr.get('query_en', '')}")
        if news:
            st.success(f"已抓到 {len(news)} 則相關新聞,確認後再請 Gemini 健診:")
            if st.button("🧠 ② 用 Gemini 健診", key="sq_step2",
                         disabled=not ensure_gemini_key()):
                with st.spinner("Gemini 分析中…"):
                    try:
                        generate_live_stock_query(); st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"分析失敗:{exc}")
            render_news_cards(news)
        else:
            st.info("這次沒抓到相關新聞,換個名稱/代號或稍後再試。")


def tool_news_strategy() -> None:
    render_news_strategy_panel()
    if st.session_state.get("live_newsetf"):
        st.divider()
        render_news_strategy(st.session_state["live_newsetf"])


def tool_focus() -> None:
    render_focus_panel()
    if st.session_state.get("live_focus"):
        render_focus(st.session_state["live_focus"]); return
    if "live_focus_news" in st.session_state:
        tr = st.session_state.get("live_focus_translation", {})
        news = st.session_state["live_focus_news"]
        en = tr.get("query_en", "")
        st.info(f"🔤 中文「{tr.get('query_zh', '')}」→ 英文檢索:**{en}**")
        if news:
            st.success(f"已抓到 {len(news)} 則全球新聞,確認後再請 Gemini 整理:")
            if st.button("🧠 ② 用 Gemini 整理(他說了什麼 + 衍伸產業 + 台美股)", key="focus_step2",
                         disabled=not ensure_gemini_key()):
                with st.spinner("Gemini 分析中…"):
                    try:
                        generate_live_focus(); st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"分析失敗:{exc}")
            render_news_cards(news)
        else:
            st.info("這次沒抓到相關新聞,換個關鍵字或稍後再試。")
        return
    doc = pick_report(FOCUS_PATH, FOCUS_ARCHIVE_DIR)
    focuses = (doc or {}).get("focuses") or []
    if not focuses:
        st.info("尚無每日全球人物追蹤存檔。可在上方輸入中文關鍵字即時產生,或設 FOCUS_TOPICS。")
        return
    if len(focuses) > 1:
        st.caption(f"📚 本日含 {len(focuses)} 個追蹤對象,可切換:")
        idx = st.selectbox(
            "選擇追蹤對象", range(len(focuses)),
            format_func=lambda i: focuses[i].get("query_zh", f"對象 {i + 1}"),
            key="focus_topic_pick",
        )
        render_focus(focuses[idx])
    else:
        render_focus(focuses[0])


def sec_etf() -> None:
    st.subheader("🧩 ETF 工作台 — 持股反查 / 圖鑑(共用同一份快取資料)")
    tab1, tab2 = st.tabs(["🔎 持股反查 / 個股", "📚 ETF 圖鑑(組合配置)"])
    with tab1:
        render_etf_crawl_panel()
        render_etf_add_panel()
        render_etf_lookup(st.session_state.get("etf_data_live"))
    with tab2:
        render_etf_profiles()


# ── 4 大頁 ─────────────────────────────────────────────────────────────────
def page_tw() -> None:
    st.header("📊 台股")
    payload = {
        "國際盤預警": load_json(INTL_ALERT_PATH),
        "法人籌碼": load_json(CHIP_PATH),
        "台股觀察": load_json(STOCKS_PATH),
    }
    render_market_digest("台股", {k: v for k, v in payload.items() if v})
    st.divider(); sec_intl()
    st.divider(); sec_chip()
    st.divider(); sec_tw_stocks()
    st.divider()
    st.markdown("### 🛠 互動工具")
    with st.expander("🩺 個股健診 — 輸入個股,看它跟新聞的相關性與上漲性質"):
        tool_stock_query()
    with st.expander("📰 新聞策略 — 貼一則新聞,轉化為台股 ETF 進出場決策"):
        tool_news_strategy()


def page_etf() -> None:
    sec_etf()


def page_us() -> None:
    st.header("🇺🇸 美股")
    payload = {"美股觀察": load_json(US_STOCKS_PATH)}
    render_market_digest("美股", {k: v for k, v in payload.items() if v})
    st.divider(); sec_us_stocks()
    st.divider()
    st.markdown("### 🛠 互動工具")
    with st.expander("🩺 個股健診 — 美股也能查(輸入 Nvidia / NVDA …)"):
        tool_stock_query()


def page_global() -> None:
    st.header("🌍 全球")
    payload = {
        "戰略報告": load_json(REPORT_PATH),
        "趨勢雷達": load_json(TRENDS_PATH),
        "全球人物追蹤": load_json(FOCUS_PATH),
    }
    render_market_digest("全球", {k: v for k, v in payload.items() if v})
    st.divider(); sec_report()
    st.divider(); sec_trends()
    st.divider()
    st.subheader("🌍 全球人物追蹤 — 中文輸入,自動翻英抓全球新聞,看牽動哪些台美股")
    tool_focus()


def page_housing() -> None:
    st.header("🏠 台灣房市")
    payload = {"房市觀察": load_json(HOUSING_PATH)}
    render_market_digest("台灣房市", {k: v for k, v in payload.items() if v})
    st.divider(); sec_housing()


def main() -> None:
    st.set_page_config(page_title="全球政經戰略看板", page_icon="🌐", layout="wide")
    st.title("🌐 全球政經戰略每日看板")

    st.sidebar.header("📂 領域")
    view = st.sidebar.radio(
        "選擇", ["📊 台股", "🇺🇸 美股", "🌍 全球", "🏠 台灣房市", "🧩 ETF 工作台"])
    st.sidebar.caption("點一個領域,該領域所有面板一次展開,最上方有 AI 今日總結。")
    st.sidebar.divider()
    with st.sidebar:
        render_proxy_status()
        st.checkbox(
            "💾 抓取後自動存到 GitHub", value=True, key="auto_save_github",
            help="勾選後,各面板『即時抓取』完成即自動 commit 對應 JSON 回 repo。需設 GITHUB_TOKEN。",
        )

    if view == "📊 台股":
        page_tw()
    elif view == "🇺🇸 美股":
        page_us()
    elif view == "🌍 全球":
        page_global()
    elif view == "🏠 台灣房市":
        page_housing()
    else:
        page_etf()


if __name__ == "__main__":
    main()
