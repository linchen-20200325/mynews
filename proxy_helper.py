"""proxy_helper.py — NAS Squid 中繼站通用模組(參照基金專案 infra/proxy.py 移植)。

用途:讓部署在境外(Streamlit Cloud 美國 IP / GitHub Actions Azure IP)的應用,
     借道家用 NAS(台灣 IP)的 Squid Proxy 存取會封鎖境外 IP 的台灣網站
     (MoneyDJ ETF 成分股等),NAS 不通時自動降級直連。

設定來源(依序):
  1. 函式參數 explicit(明確傳入的 PROXY_URL)
  2. 環境變數 PROXY_URL / HTTPS_PROXY(GitHub Actions / CLI 用)
  3. Streamlit st.secrets:新格式 PROXY_URL,或舊格式 [proxy] section
     (Streamlit Cloud App 用;未安裝 streamlit 時自動略過)

對外 API:
  get_proxy_config(explicit=None) -> {"http": url, "https": url} | None
  fetch_url(url, ...) -> requests.Response | None   (含中繼 + 自動降級直連)
  make_retry_session() -> requests.Session
  reset_proxy_cache()
  mask_endpoint(url) -> "host:port"                 (隱藏帳密供顯示)
  check_proxy(probe_url=..., timeout=10) -> dict     (檢驗中繼站是否可以使用)

直接執行可做中繼站健檢:`python proxy_helper.py`(可用回 0、不可用回非 0,供 CI)。
PROXY_URL 只走環境變數 / Streamlit Secrets,切勿寫死進程式或進版控。
"""

from __future__ import annotations

import os
import sys
import time

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 預設健檢探測目標:MoneyDJ(本專案 ETF 成分股來源,會封境外 IP)。
DEFAULT_PROBE_URL = "https://www.moneydj.com/"

# ── 模組層級快取(TTL 300s,NAS 恢復後最多 5 分鐘自動生效)──
_PROXY_CFG_CACHE: "dict | None" = None
_PROXY_CFG_TS = 0.0
_PROXY_CFG_TTL = 300


def reset_proxy_cache() -> None:
    """手動清除快取,下次 get_proxy_config() 重新讀取環境變數 / secrets。"""
    global _PROXY_CFG_CACHE, _PROXY_CFG_TS
    _PROXY_CFG_CACHE = None
    _PROXY_CFG_TS = 0.0


def _from_env() -> str:
    """環境變數中的 PROXY_URL(GitHub Actions / CLI)。"""
    url = (os.environ.get("PROXY_URL")
           or os.environ.get("HTTPS_PROXY")
           or os.environ.get("https_proxy"))
    return (url or "").strip()


def _from_secrets() -> str:
    """Streamlit st.secrets 中的 PROXY_URL(新格式)或 [proxy](舊格式)。

    未安裝 streamlit、或無 secrets 時回空字串(不拋例外)。
    """
    try:
        import streamlit as _st  # 延遲匯入:CLI / Actions 無 streamlit 也能用
        if "PROXY_URL" in _st.secrets:
            return str(_st.secrets["PROXY_URL"]).strip()
        _p = _st.secrets["proxy"]
        return f"http://{_p['username']}:{_p['password']}@{_p['endpoint']}".strip()
    except Exception:  # noqa: BLE001 — 無 streamlit / 無 secrets → 降級
        return ""


def get_proxy_config(explicit: "str | None" = None) -> "dict | None":
    """取得 NAS Proxy 設定。

    回傳 {"http": url, "https": url},或 None(未設定 → 降級直連)。
    explicit 會跳過快取(供測試指定);其餘來源走 TTL 快取。
    """
    if explicit and explicit.strip():
        _u = explicit.strip()
        return {"http": _u, "https": _u}

    global _PROXY_CFG_CACHE, _PROXY_CFG_TS
    if _PROXY_CFG_CACHE is not None and (time.time() - _PROXY_CFG_TS) < _PROXY_CFG_TTL:
        return _PROXY_CFG_CACHE if _PROXY_CFG_CACHE else None

    url = _from_env() or _from_secrets()
    _PROXY_CFG_CACHE = {"http": url, "https": url} if url else {}
    _PROXY_CFG_TS = time.time()
    return _PROXY_CFG_CACHE if _PROXY_CFG_CACHE else None


def mask_endpoint(url: str) -> str:
    """從 proxy url 取出 host:port(隱藏帳密),供畫面顯示。"""
    if not url:
        return ""
    tail = url.split("@", 1)[-1]          # 去掉 user:pwd@
    return tail.split("//", 1)[-1]        # 去掉 scheme://


def make_retry_session() -> requests.Session:
    """建立帶 5xx 指數退避的 Session。

    read=0:read-timeout 不在 urllib3 層重試(交給外層降級直連處理),避免逾時被放大;
    status=2:保留伺服器暫時 5xx(500/502/503/504)的重試韌性。
    """
    _retry = Retry(
        total=2, connect=1, read=0, status=2,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=_retry))
    s.mount("http://", HTTPAdapter(max_retries=_retry))
    return s


def fetch_url(
    url: str,
    headers: "dict | None" = None,
    params: "dict | None" = None,
    timeout: int = 20,
    retries: int = 3,
) -> "requests.Response | None":
    """通用 HTTP GET(含 NAS Proxy 中繼 + 自動降級直連)。

    行為矩陣:
      Proxy 正常    → 走 NAS,SSL verify=False(Squid CONNECT 相容)
      407 帳密錯誤  → 立即回傳 None,不重試
      403 封鎖 ×2   → 提前跳出,降級直連
      ProxyError    → 降級直連
      逾時          → 降級直連
      無 Proxy 設定 → 直連,SSL verify=True
    """
    import random as _rnd

    _proxy = get_proxy_config() or {}
    _verify = not bool(_proxy)
    _hdr = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    if headers:
        _hdr.update(headers)

    sess = make_retry_session()
    _perr = 0   # ProxyError 計數
    _block = 0  # 403 計數
    _tmo = 0    # 逾時計數

    for attempt in range(retries):
        try:
            r = sess.get(url, headers=_hdr, params=params,
                         timeout=timeout, proxies=_proxy, verify=_verify)
            if r.status_code == 407:
                print("[proxy] 407 Auth Failed — 確認 secrets 帳密")
                return None
            if r.status_code == 403:
                _block += 1
                time.sleep(_rnd.uniform(2.5, 6.0))
                if _block >= 2:
                    break
                continue
            if r.status_code == 200:
                return r
        except requests.exceptions.ProxyError as e:
            _perr += 1
            print(f"[proxy] ProxyError attempt {attempt + 1}: {e}")
            time.sleep(2)
        except requests.exceptions.Timeout:
            _tmo += 1
            print(f"[proxy] Timeout attempt {attempt + 1}: {url[:60]}")
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            print(f"[proxy] Error: {e}")
            break

    # 降級直連(proxy 連不上 / 被擋 / 逾時時,改不經 proxy 直接連)
    if _proxy and (_perr > 0 or _block >= 2 or _tmo > 0):
        print(f"[proxy] 降級直連:{url[:80]}")
        try:
            r_dc = sess.get(url, headers=_hdr, params=params,
                            timeout=timeout, proxies={}, verify=True)
            if r_dc.status_code == 200:
                print("[proxy] 直連成功")
                return r_dc
        except Exception as e_dc:  # noqa: BLE001
            print(f"[proxy] 直連失敗:{e_dc}")

    return None


def fetch_json(
    url: str,
    *,
    headers: "dict | None" = None,
    timeout: int = 20,
) -> "dict | list | None":
    """GET JSON 兩段降級 SSOT：fetch_url(proxy→direct) 後解 JSON。

    非 200、非 JSON、或 fetch_url 回 None，一律回 None。
    業務規則(如「必須是 list」)由呼叫端自行判斷。
    """
    _hdr = {"Accept": "application/json"}
    if headers:
        _hdr.update(headers)
    resp = fetch_url(url, headers=_hdr, timeout=timeout)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 — 200 但非 JSON
        return None


def check_proxy(probe_url: str = DEFAULT_PROBE_URL, timeout: int = 10) -> dict:
    """檢驗 NAS 中繼站是否可以使用。

    實際對 probe_url 發一次請求(有設 proxy 就走 proxy),回傳結構化結果:
      {
        "ok":          bool,         # 是否成功取得 200
        "configured":  bool,         # 是否有設定 PROXY_URL
        "mode":        "proxy"|"direct",
        "endpoint":    "host:port",  # 已隱藏帳密
        "status_code": int | None,
        "elapsed_ms":  int,
        "bytes":       int,
        "detail":      str,          # 人類可讀的結果說明
      }
    """
    cfg = get_proxy_config()
    configured = bool(cfg)
    mode = "proxy" if configured else "direct"
    endpoint = mask_endpoint(cfg["http"]) if configured else ""
    result = {
        "ok": False, "configured": configured, "mode": mode,
        "endpoint": endpoint, "status_code": None,
        "elapsed_ms": 0, "bytes": 0, "detail": "",
    }

    proxies = cfg or {}
    verify = not bool(proxies)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    start = time.time()
    try:
        r = requests.get(probe_url, headers=headers, proxies=proxies,
                         verify=verify, timeout=timeout)
        result["elapsed_ms"] = int((time.time() - start) * 1000)
        result["status_code"] = r.status_code
        result["bytes"] = len(r.content or b"")
        result["ok"] = (r.status_code == 200)
        if result["ok"]:
            via = f"NAS 中繼({endpoint})" if configured else "直連(未設 proxy)"
            result["detail"] = (
                f"✅ 中繼站可用 — 經 {via} 取得 {probe_url} "
                f"({result['elapsed_ms']}ms,{result['bytes']:,} bytes)"
            )
        elif r.status_code == 407:
            result["detail"] = "❌ 407 Proxy 認證失敗 — 檢查 PROXY_URL 的帳號/密碼"
        elif r.status_code == 403:
            result["detail"] = (
                f"❌ 403 被來源封鎖({mode}) — "
                + ("NAS 出口 IP 也被擋,或" if configured else "境外 IP 被擋;建議設 PROXY_URL,")
                + "確認 NAS 能連到目標站"
            )
        else:
            result["detail"] = f"⚠️ 非預期狀態碼 {r.status_code}({mode})"
    except requests.exceptions.ProxyError as e:
        result["elapsed_ms"] = int((time.time() - start) * 1000)
        result["detail"] = f"❌ 連不上 NAS 中繼站({endpoint}) — Squid 未啟動或 Port 3128 未開放:{e}"
    except requests.exceptions.Timeout:
        result["elapsed_ms"] = int((time.time() - start) * 1000)
        result["detail"] = f"❌ 逾時({timeout}s,{mode}) — NAS 或來源網站無回應"
    except Exception as e:  # noqa: BLE001
        result["elapsed_ms"] = int((time.time() - start) * 1000)
        result["detail"] = f"❌ 連線異常({mode}):{type(e).__name__}: {e}"

    return result


def main() -> int:
    """CLI / CI 入口:檢驗中繼站是否可以使用,印出結果並回傳 exit code。"""
    cfg = get_proxy_config()
    if cfg:
        print(f"[proxy] 已設定 PROXY_URL → {mask_endpoint(cfg['http'])}")
    else:
        print("[proxy] 未設定 PROXY_URL(將以直連模式測試;部署時建議設定以走 NAS 中繼)")

    print(f"[proxy] 健檢中… 探測 {DEFAULT_PROBE_URL}")
    res = check_proxy()
    print(res["detail"])
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
