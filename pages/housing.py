"""pages/housing.py — 台灣房市頁:實價登錄 + 房市觀察 + 縣市熱力圖 + 就業×空屋率地圖。"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import housing_fetcher
import numutil
import taiwan_map_data
import freshness
import update_data
import tz_utils
import ui_helpers
from app_core import (
    HOUSING_PATH,
    HOUSING_ARCHIVE_DIR,
    HOUSING_REG_PATH,
    GEOJSON_PATH,
    HOUSING_SENTIMENT_STYLE,
    ensure_gemini_key,
    fetch_live_news_cached,
    render_news_cards,
    pick_report,
    load_json,
    save_to_github,
    render_github_save,
    render_market_digest,
    ensure_proxy,
    _render_evidence_news,
)

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
        auto = st.session_state.get("auto_save_github", False)
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
                    st.session_state["live_housing_news"] = fetch_live_news_cached("housing")
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
                    if st.session_state.get("auto_save_github", False):
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
    note = freshness.stale_note(prices.get("as_of"), update_data.HOUSE_STALE_DAYS, "實價登錄房價")
    if note:
        st.warning(note)
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

_REG_BUYER_IMPACT_STYLE = {
    "偏好": ("🟢", "success"),
    "中性": ("🟡", "info"),
    "偏壞": ("🔴", "error"),
}

_REG_TREND_STYLE = {
    "趨嚴": ("🔒 趨嚴（打炒房）", "error"),
    "趨鬆": ("🔓 趨鬆（刺激買氣）", "success"),
    "持平": ("⚖️ 持平", "info"),
}

_REG_STATUS_EMOJI = {
    "已施行": "✅",
    "修法中": "🔄",
    "討論中": "💬",
    "已廢止": "❌",
    "穩定施行中": "✅",
}


def _housing_reg_days_old(data: dict) -> int | None:
    """回傳法規月報距今幾天；無法解析回 None。"""
    date_str = data.get("report_date", "")
    if not date_str:
        return None
    try:
        from datetime import date
        rep = date.fromisoformat(date_str)
        today = date.fromisoformat(tz_utils.taiwan_today())
        return (today - rep).days
    except Exception:  # noqa: BLE001
        return None


def render_housing_regulation_live_panel() -> None:
    """法規月報：手動觸發立即抓取並請 Gemini 整理。"""
    with st.container(border=True):
        st.markdown("#### ⚡ 即時更新房產法規月報")
        st.caption("抓取近 35 天台灣房產法規相關新聞，請 Gemini 整理各法規現況與對買方影響。"
                   "建議每月觸發一次即可（法規異動頻率低）。")
        if st.button("🔄 立即抓取房產法規新聞並整理", use_container_width=True,
                     disabled=not ensure_gemini_key()):
            with st.spinner("抓取法規新聞 → Gemini 整理中…（約 20–40 秒）"):
                try:
                    from update_data import fetch_housing_reg_news, get_housing_regulation_analysis
                    news = fetch_housing_reg_news()
                    today = tz_utils.taiwan_today()
                    data = get_housing_regulation_analysis(news, today)
                    st.session_state["live_housing_reg"] = data
                    # 存檔
                    save_to_github(str(HOUSING_REG_PATH), data, f"（法規月報 {today}）")
                    ym = today[:7]  # YYYY-MM
                    save_to_github(f"data/housing_reg/{ym}.json", data, f"（法規月報封存 {ym}）")
                    st.success(f"完成！整理了 {len(data.get('regulations', []))} 項法規。")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"更新失敗：{exc}")


def render_housing_regulation(data: dict | None) -> None:
    """渲染房產法規月報主畫面。"""
    if not data:
        st.info("尚無房產法規月報。請按上方「⚡ 即時更新」產生（或等下次月排程）。")
        return

    days = _housing_reg_days_old(data)
    trend_key = data.get("trend", "持平")
    trend_label, trend_type = _REG_TREND_STYLE.get(trend_key, ("⚖️ 持平", "info"))

    col1, col2 = st.columns([2, 1])
    with col1:
        st.metric(
            "整體法規趨勢", trend_label,
            help=(
                "🔒 趨嚴（打炒房）：政府積極限制炒作、貸款條件收緊，對投資客偏壞但有助抑制泡沫。\n"
                "🔓 趨鬆（刺激買氣）：放寬管制、降低貸款門檻，對首購族偏有利。\n"
                "⚖️ 持平：法規無重大異動，市場靠供需自行調節。"
            ),
        )
    with col2:
        if days is not None:
            fresh_label = "🟢 本月" if days <= 31 else f"⚠️ {days} 天前"
            st.metric("資料更新", fresh_label,
                      help=f"報告日期：{data.get('report_date', '—')}")

    if data.get("summary"):
        getattr(st, trend_type)(data["summary"])

    # 法規卡片
    regs = data.get("regulations") or []
    if not regs:
        st.info("本月無法規更新資料。")
        return

    st.markdown(f"**📜 現行主要法規（共 {len(regs)} 項）**")
    for reg in regs:
        impact_key = reg.get("buyer_impact", "中性")
        emoji, container_type = _REG_BUYER_IMPACT_STYLE.get(impact_key, ("🟡", "info"))
        status = reg.get("status", "")
        status_icon = _REG_STATUS_EMOJI.get(status, "📋")
        with st.container(border=True):
            col_a, col_b = st.columns([3, 1])
            with col_a:
                st.markdown(f"**{status_icon} {reg.get('name', '—')}**　"
                            f"`{status}`　{reg.get('effective_date', '')}")
            with col_b:
                st.markdown(f"{emoji} **{impact_key}**")
            if reg.get("description"):
                st.caption(reg["description"])
            if reg.get("impact_note"):
                getattr(st, container_type)(reg["impact_note"])

    _render_evidence_news(data.get("evidence_news") or [])
    st.caption("⚠️ 法規整理由 AI 自動彙整真實新聞而成；法規細節以立法院/官方公告為準，非法律建議。")


def sec_regulation() -> None:
    """房產法規月報區塊：顯示月更頻率提示 + 最新月報內容。"""
    st.subheader("📜 房產法規月報")
    st.caption("每月整理一次台灣主要房產法規（平均地權、囤房稅、信用管制、新青安等）現況與買方影響。")
    ui_helpers.render_spec_card(
        name="房產法規月報",
        source="Google News 房產法規相關新聞（近 35 天）＋ Gemini AI 整理，非官方法律文件",
        freq="每月更新一次（法規異動頻率低，可手動觸發更新）",
        bull="趨鬆（政府降低買房門檻、放寬信用管制）→ 對首購族偏有利",
        bear="趨嚴（打炒房持續、央行升息）→ 貸款成本上升、預售屋限制多",
        note="法規細節以行政院、立法院官方公告為準，本頁為 AI 摘要僅供參考，非法律建議。",
    )

    live = st.session_state.get("live_housing_reg")
    stored = load_json(HOUSING_REG_PATH)
    current = live or stored
    days = _housing_reg_days_old(current) if current else None

    with st.expander(
        "⚡ 立即更新法規月報" + (f"　（上次更新：{days} 天前）" if days is not None else ""),
        expanded=(days is None or days > 31),
    ):
        render_housing_regulation_live_panel()

    render_housing_regulation(current)


@st.cache_data(show_spinner=False, ttl=3600)
def _load_employment_vacancy_cached() -> pd.DataFrame:
    return taiwan_map_data.load_df()


def sec_population_map() -> None:
    """就業人口熱區 × 未來就業轉向區域 — 台灣地圖分析面板。"""
    st.subheader("🗺️ 就業人口熱區 × 未來就業轉向區域")
    st.caption(
        "資料來源說明：就業人口使用**勞保投保人數縣市別統計**（勞動部），"
        "空屋率使用**低度使用（用電）住宅比率**（內政部不動產資訊平台）。"
        "目前顯示 Mock 示範資料，接入真實資料只需替換 `taiwan_map_data._mock_df()`。"
    )

    df = _load_employment_vacancy_cached()
    emp_map = df.set_index("county")["employment"].to_dict()
    vacancy_map = df.set_index("county")["vacancy_rate"].to_dict()

    tab1, tab2, tab3 = st.tabs(
        ["👷 就業人口熱區", "🏚️ 空屋率地圖", "🔀 雙變數 + 全縣市明細"])

    with tab1:
        st.markdown("##### 各縣市就業人口分佈（勞保投保人數）")
        st.caption("顏色越深代表就業人口越多；六都與新竹科學園區所在縣市為主要就業核心。")
        emp_wan = {c: v / 10_000 for c, v in emp_map.items()}
        render_taiwan_choropleth(emp_wan, "就業人口（萬人）", "Blues")
        top5 = df.nlargest(5, "employment")[["county", "employment_wan", "vacancy_rate"]]
        top5.columns = ["縣市", "就業人口（萬人）", "空屋率（%）"]
        st.markdown("**就業人口前 5 大縣市**")
        st.dataframe(top5, use_container_width=True, hide_index=True)

    with tab2:
        st.markdown("##### 各縣市低度使用（空屋）住宅比率")
        st.caption(
            "顏色越紅代表空屋率越高；離島與東部縣市通常空屋率偏高，"
            "反映人口外流與投資型空置共同推升。"
        )
        render_taiwan_choropleth(
            vacancy_map, "空屋率（%）", "YlOrRd",
        )
        high_vac = df.nlargest(5, "vacancy_rate")[["county", "vacancy_rate", "employment_wan"]]
        high_vac.columns = ["縣市", "空屋率（%）", "就業人口（萬人）"]
        st.markdown("**空屋率前 5 高縣市**")
        st.dataframe(high_vac, use_container_width=True, hide_index=True)

    with tab3:
        st.markdown("##### 就業人口 vs 空屋率 — 雙變數對比（氣泡圖）")
        st.caption(
            "X 軸：空屋率（越右越高）；Y 軸：就業人口（萬人，越上越多）；"
            "氣泡大小：就業人口規模；🔴 紅色氣泡 = **未來就業轉向潛力區**"
            "（空屋率高 + 就業人口少，人口外流風險 / 政策重點轉型區域）。"
        )
        try:
            import plotly.express as px
        except Exception:  # noqa: BLE001
            st.info("未安裝 plotly，無法顯示散佈圖。")
            return

        df_plot = df.copy()
        df_plot["類別"] = df_plot["is_transition"].map(
            {True: "🔴 就業轉向潛力區", False: "🔵 就業穩定區"}
        )
        fig = px.scatter(
            df_plot,
            x="vacancy_rate",
            y="employment_wan",
            size="employment_wan",
            color="類別",
            color_discrete_map={"🔴 就業轉向潛力區": "#e63946", "🔵 就業穩定區": "#457b9d"},
            text="county",
            hover_data={"vacancy_rate": ":.1f", "employment_wan": ":.1f",
                        "transition_score": ":.2f", "類別": True},
            labels={"vacancy_rate": "空屋率（%）", "employment_wan": "就業人口（萬人）"},
            size_max=55,
        )
        fig.update_traces(textposition="top center", textfont_size=10)
        fig.update_layout(
            height=520,
            margin={"t": 20, "b": 20},
            legend_title_text="區域分類",
        )
        st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.markdown("##### 📋 全台 22 縣市完整明細（依轉向分數排序）")
        st.caption(
            "此表動態讀取 `taiwan_map_data.load_df()`（SSOT），會隨資料來源自動更新。"
            "**轉向分數** = 空屋率 ÷（就業人口正規化 + 0.15），分數越高代表"
            "「高空屋率 + 低就業人口」越明顯；🔴 為就業轉向潛力區（≥第 60 百分位）。"
        )
        full = df.sort_values("transition_score", ascending=False).reset_index(drop=True)
        full.insert(0, "排名", full.index + 1)
        full["轉向潛力區"] = full["is_transition"].map({True: "🔴 是", False: "🔵 否"})
        show = full[["排名", "county", "employment_wan", "vacancy_rate",
                     "transition_score", "轉向潛力區"]].copy()
        show.columns = ["排名", "縣市", "就業人口（萬人）", "空屋率（%）", "轉向分數", "轉向潛力區"]
        st.dataframe(
            show,
            use_container_width=True,
            hide_index=True,
            height=810,  # 22 列全展開,免捲動
            column_config={
                "就業人口（萬人）": st.column_config.ProgressColumn(
                    "就業人口（萬人）", format="%.1f",
                    min_value=0.0, max_value=float(full["employment_wan"].max()),
                ),
                "空屋率（%）": st.column_config.ProgressColumn(
                    "空屋率（%）", format="%.1f%%",
                    min_value=0.0, max_value=float(full["vacancy_rate"].max()),
                ),
                "轉向分數": st.column_config.NumberColumn("轉向分數", format="%.2f"),
            },
        )
        c1, c2, c3 = st.columns(3)
        c1.metric("縣市總數", f"{len(full)} 個")
        c2.metric("🔴 轉向潛力區", f"{int(full['is_transition'].sum())} 個")
        c3.metric("平均空屋率", f"{full['vacancy_rate'].mean():.1f}%")
        st.download_button(
            "⬇️ 下載全縣市明細 CSV",
            data=show.to_csv(index=False).encode("utf-8-sig"),
            file_name="taiwan_employment_vacancy.csv",
            mime="text/csv",
            use_container_width=True,
        )

        with st.expander("📖 資料接入指南（如何替換真實政府資料）"):
            st.markdown(
                """
**① 就業人口（勞保投保人數）**
- 下載：[勞動部勞保局統計查詢](https://www.bli.gov.tw/0015094.html) → 選「縣市別」→ 下載 Excel/CSV
- 欄位對齊：`縣市別` → `county`，`被保險人數` → `employment`（去千分位逗號轉 `int`）
- 文字清洗：`台` → `臺`（`str.replace("台", "臺")`），移除「合計」列

**② 空屋率（低度使用住宅比率）**
- 下載：[內政部不動產資訊平台](https://pip.moi.gov.tw/) → 住宅統計 → 低度使用住宅
- 欄位對齊：`縣市別` → `county`，`低度使用住宅比率(%)` → `vacancy_rate`（轉 `float`）
- 清洗：同上 臺/台 正規化，移除全國合計列

**接入步驟**：替換 `taiwan_map_data._mock_df()` 的回傳值，`load_df()` 呼叫端無需改動。
                """
            )


def page_housing() -> None:
    st.header("🏠 台灣房市")
    ui_helpers.render_intro_banner(
        page_key="housing",
        title="台灣房市頁",
        steps=[
            "先看 🗺️ **各縣市房價地圖**：成屋 vs 預售屋每坪均價（實價登錄真實數據）。",
            "再看 🌡️ **房市冷熱判讀**：AI 依新聞研判各縣市熱度與打房政策方向。",
            "最後看 📜 **房產法規月報**：平均地權、囤房稅、新青安等法規現況與對你的影響。",
        ],
    )
    payload = {"房市觀察": load_json(HOUSING_PATH)}
    render_market_digest("台灣房市", {k: v for k, v in payload.items() if v})
    st.divider(); sec_housing()
    st.divider(); sec_regulation()
    st.divider(); sec_population_map()
