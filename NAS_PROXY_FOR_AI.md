# NAS Squid Proxy 中繼站 — AI 移植說明書(mynews)

> **給 AI 的指示**:本文件是「一次性移植任務說明書」。照著步驟執行即可把 NAS 中繼站
> (含健康檢查)植入任何 Streamlit / Actions 專案,不需詢問使用者。

## 任務概述

讓部署在境外 IP 的應用借道家用 NAS(台灣 IP)的 Squid Proxy 存取會封境外 IP 的台灣網站
(本專案為 MoneyDJ ETF 成分股),NAS 不通時自動降級直連,並可隨時檢驗中繼站是否可用。

**要做的事:**
1. 建立 `proxy_helper.py`(核心模組,含 `check_proxy()` 健檢 + CLI)。
2. `requirements.txt` 確認含 `requests`、`urllib3`。
3. `.streamlit/secrets.toml.example` 提供 `PROXY_URL` 範本;`.gitignore` 含 `.streamlit/secrets.toml`。
4. 抓取流程改走 `proxy_helper`(本專案 `etf_fetcher.get_proxies()` 已委派)。
5. 介面/CI 加上「檢驗中繼站是否可以使用」入口。

## Step 1:`proxy_helper.py`

放在專案根目錄,完整內容見本 repo 的 `proxy_helper.py`。重點 API:

- `get_proxy_config(explicit=None)` — 依序讀 explicit → 環境變數 `PROXY_URL`/`HTTPS_PROXY`
  → Streamlit `st.secrets`(新格式 `PROXY_URL` 或舊格式 `[proxy]`);回 `{"http","https"}` 或 `None`。
- `fetch_url(url, headers, params, timeout, retries)` — 中繼 + 自動降級直連;
  407 不重試、403×2 降級、ProxyError/逾時降級。
- `make_retry_session()` / `reset_proxy_cache()` / `mask_endpoint(url)`。
- `check_proxy(probe_url=DEFAULT_PROBE_URL, timeout=10)` — 實際發請求測試,回結構化 dict。
- `python proxy_helper.py` — CLI 健檢,可用回 exit 0、不可用回非 0(供 CI)。

> 與單純 Streamlit 版的差異:本版同時支援 **環境變數**(GitHub Actions / CLI)與
> **st.secrets**(Streamlit),且未安裝 streamlit 時不會拋例外。

## Step 2:requirements.txt

```
requests>=2.31
urllib3>=2.0
```

## Step 3:secrets / 環境變數

`.streamlit/secrets.toml`(本機)或 Streamlit Cloud Secrets:

```toml
PROXY_URL = "http://你的帳號:你的密碼@yourname.synology.me:3128"
```

GitHub Actions:repo Secrets 新增 `PROXY_URL`,workflow 以 `env: PROXY_URL: ${{ secrets.PROXY_URL }}` 注入。

## Step 4:在程式中使用

```python
import proxy_helper

# 取 proxies dict 給 requests
proxies = proxy_helper.get_proxy_config() or {}
resp = requests.get(url, proxies=proxies, verify=not bool(proxies), timeout=20)

# 或直接用內建抓取(自動降級直連)
resp = proxy_helper.fetch_url("https://www.moneydj.com/etf/x/Basic/Basic0007.xdjhtm?etfid=...")
```

## Step 5:檢驗中繼站是否可以使用

- **網頁**:sidebar 加
  ```python
  res = proxy_helper.check_proxy()
  (st.success if res["ok"] else st.error)(res["detail"])
  ```
- **CLI / CI**:`python proxy_helper.py`(exit code 反映可用性)。
- **Actions**:加 `proxy_check.yml`(workflow_dispatch)跑 `python proxy_helper.py`。

## 完成驗證 Checklist

```
□ proxy_helper.py 建立於根目錄
□ requirements.txt 含 requests / urllib3
□ .streamlit/secrets.toml.example 含 PROXY_URL;.gitignore 含 .streamlit/secrets.toml
□ python -m py_compile proxy_helper.py etf_fetcher.py app.py  通過
□ python proxy_helper.py  能執行並回報中繼站狀態
□ Streamlit Cloud / Actions Secrets 已設定 PROXY_URL
```
