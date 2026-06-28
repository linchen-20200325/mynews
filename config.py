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
    """讀布林旗標:值為 0/false/no(不分大小寫)→ False,其餘(含空字串回退)→ default。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() not in ("0", "false", "no")


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
