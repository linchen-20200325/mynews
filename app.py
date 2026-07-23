"""app.py — Streamlit 入口:側邊欄路由 → 各頁模組。

本地執行: streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

import line_notify
from app_core import render_proxy_status
from pages.tw import page_tw
from pages.us import page_us
from pages.global_ import page_global
from pages.housing import page_housing
from pages.ai_brain import page_ai_brain
from pages.etf import page_etf
from pages.diagnostics import page_diagnostics


def _safe_render(page_fn, name: str) -> None:
    """頁面級斷路器:單一頁 render 中途拋例外時就地顯示友善降級橫幅,不讓整站噴紅色 traceback。

    每則 LINE 推播末尾都掛看板連結,一個壞頁不該反過來拖垮整站信任(F4)。此處攔截頁面
    渲染的一般例外→友善提示+除錯細節收進 expander;側邊欄與其他頁不受影響,換頁重跑即恢復。
    只攔 Exception:Streamlit 的 st.rerun()/st.stop() 走 ScriptControlException(BaseException
    子類),不會被吞,控制流照常運作。
    """
    try:
        page_fn()
    except Exception as exc:  # noqa: BLE001 — 頁面級兜底:任何 render 例外都不該炸掉整站
        st.error(
            f"⚠️「{name}」頁暫時無法載入。側邊欄與其他頁不受影響——"
            "可切換其他領域,或到 🩺 資料診斷 查各資料源狀態。"
        )
        with st.expander("🔧 錯誤細節（供除錯）"):
            st.exception(exc)


def _pyarrow_guard() -> None:
    """啟動守門:偵測到 pyarrow≥25 就開站即大聲告警,而非等 st.dataframe 重繪時整站靜默 segfault。

    requirements.txt 已釘 `pyarrow<25`(見 GOTCHAS「cp314 × 未鎖依賴」的雲端 segfault 事故);
    此為「部署環境 pin 萬一漂版」的最後一道可見警報——把神祕當機變成開站即現形的診斷訊息。
    偵測本身失敗絕不反過來拖垮 app。
    """
    try:
        import pyarrow
        major = int(pyarrow.__version__.split(".")[0])
    except Exception:  # noqa: BLE001 — 守門偵測失敗不得炸掉 app
        return
    if major >= 25:
        st.error(
            f"⚠️ 偵測到 pyarrow {pyarrow.__version__}(≥25)—— `requirements.txt` 已釘 <25,"
            "但目前部署環境漂到此版。此版本在 `st.dataframe` 重繪時會 segfault 整站,"
            "請確認部署環境的 `pyarrow<25` 有生效(詳 GOTCHAS.md)。"
        )


def main() -> None:
    st.set_page_config(page_title="全球政經戰略看板", page_icon="🌐", layout="wide")
    # 隱藏 Streamlit 自動偵測 pages/ 產生的多頁導覽列（已有自訂 radio 導覽）
    st.markdown(
        "<style>[data-testid='stSidebarNav']{display:none}</style>",
        unsafe_allow_html=True,
    )
    _pyarrow_guard()  # pin 漂版守門:pyarrow≥25 開站即告警(見 GOTCHAS 雲端 segfault)
    st.title("🌐 全球政經戰略每日看板")
    st.caption(f"{line_notify.MORNING_TAGLINE} — 資料為 AI/工具自動生成,僅供參考,非投資建議")

    st.sidebar.header("📂 領域")
    view = st.sidebar.radio(
        "選擇", ["📊 台股", "🇺🇸 美股", "🌍 全球", "🏠 台灣房市", "🧩 ETF 工作台",
               "🧠 AI 決策大腦", "🩺 資料診斷"])
    st.sidebar.caption("點一個領域,該領域所有面板一次展開,最上方有 AI 今日總結。")
    st.sidebar.divider()
    with st.sidebar:
        render_proxy_status()
        st.checkbox(
            "💾 抓取後自動存到 GitHub", value=False, key="auto_save_github",
            help="勾選後,各面板『即時抓取』完成即自動 commit 對應 JSON 回 repo(需設 GITHUB_TOKEN)。"
                 "預設關閉:抓取與存檔解耦,免去每次抓取多等 2~8 秒的同步 commit;"
                 "內容未變更時會自動略過 commit。",
        )

    if view == "📊 台股":
        _safe_render(page_tw, "台股")
    elif view == "🇺🇸 美股":
        _safe_render(page_us, "美股")
    elif view == "🌍 全球":
        _safe_render(page_global, "全球")
    elif view == "🏠 台灣房市":
        _safe_render(page_housing, "台灣房市")
    elif view == "🧠 AI 決策大腦":
        _safe_render(page_ai_brain, "AI 決策大腦")
    elif view == "🩺 資料診斷":
        _safe_render(page_diagnostics, "資料診斷")
    else:
        _safe_render(page_etf, "ETF 工作台")


if __name__ == "__main__":
    main()
