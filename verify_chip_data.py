"""verify_chip_data.py — 三大法人買賣超「真實資料」探針。

用途:用既有 proxy_helper.fetch_url() 向證交所抓一天的三大法人買賣超,
     證明『真實籌碼資料抓得到、欄位長怎樣』,供日後正式接入前一次性驗證。
     全程真實數字、零 AI 估算(符合硬規則:籌碼數字只能來自真實來源)。

抓兩個端點:
  BFI82U — 三大法人買賣超「總額」(外資/投信/自營,大盤層級)
  T86    — 三大法人買賣超「個股」明細(僅印筆數與前幾檔,驗證個股層級可用)

用法:
  python verify_chip_data.py            # 抓最近一個交易日(自動回推週末)
  python verify_chip_data.py 20250603   # 指定日期 YYYYMMDD

需 PROXY_URL(環境變數 / Streamlit secrets)走 NAS 中繼較穩;未設則直連。
可用回 0,抓不到回非 0(供 CI / GitHub Actions 把關)。注意:本開發沙箱
網路被白名單封鎖,務必在『運行環境(接 NAS)或 Actions』執行才有真實結果。
"""

from __future__ import annotations

import datetime as _dt
import sys

import proxy_helper

BFI82U = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={d}&type=day&response=json"
T86 = "https://www.twse.com.tw/rwd/zh/fund/T86?date={d}&selectType=ALL&response=json"


def recent_trading_day() -> str:
    """回最近一個工作日(YYYYMMDD)。只跳週末;遇國定假日則該日無資料,改傳日期重跑。"""
    d = _dt.date.today()
    while d.weekday() >= 5:  # 5=六, 6=日
        d -= _dt.timedelta(days=1)
    return d.strftime("%Y%m%d")


def _fetch_json(url: str) -> dict | None:
    """走 proxy_helper 抓 JSON;非 200 / 非 JSON / None 一律回 None。"""
    r = proxy_helper.fetch_url(url, timeout=20, retries=3)
    if r is None:
        return None
    try:
        return r.json()
    except Exception as exc:  # noqa: BLE001 — 來源偶回 HTML 錯誤頁
        print(f"  [warn] 回應非 JSON:{exc}")
        return None


def _ok(payload: dict | None) -> bool:
    """證交所 JSON 有資料時 stat == 'OK';假日/無資料會是中文錯誤訊息。"""
    return bool(payload) and payload.get("stat") == "OK" and bool(payload.get("data"))


def probe(date_str: str) -> bool:
    """抓 BFI82U + T86,印出真實欄位;兩者皆有資料才算通過。"""
    print(f"== 驗證日期:{date_str} ==")

    print("\n[1/2] BFI82U 三大法人買賣超總額")
    total = _fetch_json(BFI82U.format(d=date_str))
    if _ok(total):
        print("  fields:", "、".join(total.get("fields", [])))
        for row in total["data"]:
            print("   ", row)
    else:
        msg = (total or {}).get("stat") if total else "連線失敗(None)"
        print(f"  ❌ 無資料 / 抓取失敗:{msg}")

    print("\n[2/2] T86 三大法人買賣超個股明細")
    stock = _fetch_json(T86.format(d=date_str))
    if _ok(stock):
        rows = stock["data"]
        print("  fields:", "、".join(stock.get("fields", [])))
        print(f"  個股筆數:{len(rows)};前 3 檔:")
        for row in rows[:3]:
            print("   ", row)
    else:
        msg = (stock or {}).get("stat") if stock else "連線失敗(None)"
        print(f"  ❌ 無資料 / 抓取失敗:{msg}")

    passed = _ok(total) and _ok(stock)
    print("\n=>", "✅ 真實籌碼資料可抓取" if passed
          else "❌ 抓取失敗(假日無資料?或網路被封鎖,請於接 NAS 的環境重跑)")
    return passed


def main() -> int:
    date_str = sys.argv[1] if len(sys.argv) > 1 else recent_trading_day()
    cfg = proxy_helper.get_proxy_config()
    print(f"[proxy] {'走 NAS 中繼 ' + proxy_helper.mask_endpoint(cfg['http']) if cfg else '未設 PROXY_URL,直連模式'}\n")
    return 0 if probe(date_str) else 1


if __name__ == "__main__":
    sys.exit(main())
