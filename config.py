"""config.py — 環境變數讀取的單一真相源(SSOT)。

職責:
  - env_bool / env_int / env_float:型別安全的 os.environ 讀取輔助函式
  - *_enabled():所有功能開關(ENABLE_* 旗標)集中定義,消除散落各處的重複判斷式

零 Streamlit、零 paths 相依;可被任何模組安全 import。
"""

from __future__ import annotations

import os


# ---------------------------------------------------------------------------
# 型別安全讀取輔助
# ---------------------------------------------------------------------------

def env_bool(name: str, default: bool = True) -> bool:
    """讀布林旗標:0/false/no(去空白、不分大小寫)→ False;未設或空字串 → default。

    空字串必須視同未設定:GitHub Actions 會把未定義的 ``vars.X`` 注入成空字串
    (而非不設定該變數),若把空字串當 True,預設 False 的旗標(如 PUSH_ALL_DAYS)
    會被誤開啟——2026-07-12 假日推播守門被旁通即此根因。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no")


def env_int(name: str, default: int) -> int:
    """讀整數環境變數;解析失敗或空值回 default。"""
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    """讀浮點數環境變數;解析失敗或空值回 default。"""
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def env_str(name: str, default: str = "") -> str:
    """讀字串環境變數;未設或空值回 default。"""
    return os.environ.get(name) or default


# ---------------------------------------------------------------------------
# 功能開關(ENABLE_* 旗標)
# ---------------------------------------------------------------------------

def trend_radar_enabled() -> bool:
    return env_bool("ENABLE_TREND_RADAR")


def stock_picker_enabled() -> bool:
    return env_bool("ENABLE_STOCK_PICKER")


def us_stock_picker_enabled() -> bool:
    return env_bool("ENABLE_US_STOCK_PICKER")


def intl_alert_enabled() -> bool:
    return env_bool("ENABLE_INTL_ALERT")


def intl_alert_line_enabled() -> bool:
    return env_bool("ENABLE_INTL_ALERT_LINE")


def chip_enabled() -> bool:
    return env_bool("ENABLE_CHIP")


def chip_line_enabled() -> bool:
    return env_bool("ENABLE_CHIP_LINE")


def confluence_line_enabled() -> bool:
    return env_bool("ENABLE_CONFLUENCE_LINE")


def focus_enabled() -> bool:
    return env_bool("ENABLE_FOCUS")


def housing_enabled() -> bool:
    return env_bool("ENABLE_HOUSING")


def watch_enabled() -> bool:
    """第二個 bot 的 token 與推播對象都設了才啟用個股盯盤;否則整段略過。"""
    return bool(os.environ.get("LINE_WATCH_TOKEN") and os.environ.get("LINE_WATCH_TO"))


# ---------------------------------------------------------------------------
# 啟動自檢:功能開關 / 金鑰在否總表(F8)
# ---------------------------------------------------------------------------

def summary_lines() -> list[str]:
    """啟動時的『功能開關 / 金鑰在否』總表(給排程 log)。

    只印 on/off 與 有/缺,**絕不印任何金鑰值**(§硬規則:金鑰不得進版控/log)。
    讓設定錯誤(如空字串誤關功能、漏設某 Secret)開機即現形,不再靜默失效。
    """
    def onoff(b: bool) -> str:
        return "on " if b else "off"

    def has(*names: str) -> str:
        return "有" if any((os.environ.get(n) or "").strip() for n in names) else "缺"

    main_bot = bool(env_str("LINE_CHANNEL_ACCESS_TOKEN") and env_str("LINE_TO"))
    return [
        "── 設定總表(啟動自檢;只印 on/off·有/缺,絕不印金鑰值)──",
        f"  金鑰:Gemini={has('GEMINI_API_KEY', 'GEMINI_API_KEYS')}  PROXY={has('PROXY_URL')}",
        f"  推播:主 bot={onoff(main_bot)} 盯盤 bot={onoff(watch_enabled())}"
        f"  DASHBOARD_URL={has('DASHBOARD_URL')}  HEARTBEAT_PING_URL={has('HEARTBEAT_PING_URL')}",
        f"  分析:趨勢={onoff(trend_radar_enabled())} 台股精選={onoff(stock_picker_enabled())}"
        f" 美股精選={onoff(us_stock_picker_enabled())} 焦點={onoff(focus_enabled())} 房市={onoff(housing_enabled())}",
        f"  國際盤={onoff(intl_alert_enabled())}(LINE {onoff(intl_alert_line_enabled())})"
        f" 籌碼={onoff(chip_enabled())}(LINE {onoff(chip_line_enabled())}) 共振LINE={onoff(confluence_line_enabled())}",
        f"  假日全推 PUSH_ALL_DAYS={onoff(env_bool('PUSH_ALL_DAYS', False))}",
    ]
