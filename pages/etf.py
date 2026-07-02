"""pages/etf.py — ETF 工作台:持股反查 / ETF 圖鑑 / 成分股更新。"""
from __future__ import annotations

import json

import streamlit as st

import etf_data
import etf_fetcher
import etf_holdings
import etf_profile_fetcher
import freshness
import price_fetcher
import proxy_helper
from app_core import (
    ensure_proxy,
    save_to_github,
    render_github_save,
    render_crawl_summary,
    PRICE_STALE_DAYS,
)

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
    # 渲染層二次過濾：清除快取內殘留的 ETF 代號列（stale cache 安全網）
    rows = [r for r in rows if not etf_holdings._ETF_CODE_RE.match(r["ticker"]) and r["ticker"] not in etfs]

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

def sec_etf() -> None:
    st.subheader("🧩 ETF 工作台 — 持股反查 / 圖鑑(共用同一份快取資料)")
    tab1, tab2 = st.tabs(["🔎 持股反查 / 個股", "📚 ETF 圖鑑(組合配置)"])
    with tab1:
        render_etf_crawl_panel()
        render_etf_add_panel()
        render_etf_lookup(st.session_state.get("etf_data_live"))
    with tab2:
        render_etf_profiles()

def page_etf() -> None:
    sec_etf()
