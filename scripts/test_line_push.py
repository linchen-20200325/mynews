#!/usr/bin/env python3
"""scripts/test_line_push.py — 測試 LINE Messaging API 推播是否暢通。

用途:設好 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_TO 後,一鍵送一則測試訊息,
     驗證金鑰/對象是否正確(國際盤大跌預警走的是同一條推播管線)。

用法:
    LINE_CHANNEL_ACCESS_TOKEN=xxx LINE_TO=yyy python scripts/test_line_push.py
    python scripts/test_line_push.py "自訂訊息內容"

行為:成功 exit 0;未設定環境變數 / LINE 回非 200 → exit 1 並印出原因。
     不存任何檔、不呼叫 Gemini,純粹驗證推播通道。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# scripts/ 在子目錄,直接執行時把專案根目錄加入 import path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import update_data  # noqa: E402 — 重用既有 LINE 推播管線


def main() -> int:
    if not (os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_TO")):
        print(
            "錯誤:請先設定環境變數 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_TO。\n"
            "  token  :LINE Developers Console → Messaging API → Channel access token\n"
            "  LINE_TO:Basic settings → Your user ID(或群組/聊天室 id)",
            file=sys.stderr,
        )
        return 1

    msg = sys.argv[1] if len(sys.argv) > 1 else "✅ 國際盤大跌預警 — LINE 測試推播 OK"
    try:
        update_data._push_line_text(msg)
    except Exception as exc:  # noqa: BLE001
        print(f"LINE 推播失敗:{exc}", file=sys.stderr)
        return 1
    print(f"已送出 LINE 測試訊息:{msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
