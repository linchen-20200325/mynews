"""數值熱區 code review 的回歸測試:鎖定 3 個最易出錯的輸入。

純離線(不碰網路),直接餵 payload/CSV bytes 給解析函式。
跑法:  python3 test_numeric_audit.py   (零相依,自帶 assert 與計分)
"""
from __future__ import annotations

import index_fetcher
import housing_fetcher


# ── 風險 ① 前收為 0 → 必須回 None,不可 ZeroDivisionError ──────────────────
def test_parse_chart_zero_prev_no_div0():
    payload = {"chart": {"result": [{
        "meta": {"regularMarketPrice": 100.0, "chartPreviousClose": 0},
        "indicators": {"quote": [{"close": [100.0]}]},
    }]}}
    # 前收 0:index_fetcher.py:114 的守門必須擋下,回 None(而非除零爆掉)
    assert index_fetcher._parse_chart(payload) is None


# ── 風險 ② 假日/下市:收盤序列全 None 且無 meta → 回 None,不可當 0 ────────
def test_parse_chart_all_none_closes():
    payload = {"chart": {"result": [{
        "meta": {},  # 無 regularMarketPrice / previousClose
        "indicators": {"quote": [{"close": [None, None, None]}]},
    }]}}
    # 全 None 過濾後 closes=[]，len<2 且 prev 缺 → 必須回 None(沉默填 0 是 bug)
    assert index_fetcher._parse_chart(payload) is None


# ── 風險 ③ CSV 全是車位/離群值 → 濾光後 _summarize count=0、avg=None,不可崩 ──
_HEADER = ("鄉鎮市區,交易標的,建物型態,單價元平方公尺,總價元,交易年月日,"
           "土地位置建物門牌\n")


def _csv(*rows: str) -> bytes:
    return (_HEADER + "".join(r + "\n" for r in rows)).encode("utf-8-sig")


def test_summarize_all_filtered_out():
    # 第1列=車位(交易標的不以「房地」起頭→濾)、第2列=住宅但每坪>400萬離群(濾)
    csv_bytes = _csv(
        "信義區,車位,坡道平面,500000,1000000,1140101,某路1號",
        "信義區,房地(土地+建物),住宅大樓,99999999,9999999999,1140102,某路2號",
    )
    rows = housing_fetcher.parse_price_csv(csv_bytes, "台北市")
    assert rows == [], f"應全部濾掉,實得 {rows}"
    summ = housing_fetcher._summarize(rows)
    assert summ["count"] == 0
    assert summ["avg_ping_wan"] is None      # 顯式 None,非靜默 0
    assert summ["median_ping_wan"] is None
    assert summ["samples"] == []


def test_summarize_keeps_valid_and_unit_is_wan_per_ping():
    # 一筆合法住宅:單價 302500 元/㎡ → 每坪 = 302500×3.305785÷10000 ≈ 100.0 萬/坪
    csv_bytes = _csv(
        "信義區,房地(土地+建物),住宅大樓,302500,30000000,1140103,某路3號",
        "信義區,車位,坡道平面,500000,1000000,1140104,某路4號",  # 車位應被濾
    )
    rows = housing_fetcher.parse_price_csv(csv_bytes, "台北市")
    assert len(rows) == 1, f"只該留 1 筆住宅,實得 {len(rows)}"
    ping = rows[0]["ping_wan"]
    assert abs(ping - 100.0) < 0.05, f"量綱錯:每坪應≈100萬,實得 {ping}"  # 浮點容差
    summ = housing_fetcher._summarize(rows)
    assert summ["count"] == 1 and abs(summ["avg_ping_wan"] - 100.0) < 0.05


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 — 把崩潰也算失敗,別吞
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{passed}/{len(tests)} passed")
    import sys
    sys.exit(0 if passed == len(tests) else 1)
