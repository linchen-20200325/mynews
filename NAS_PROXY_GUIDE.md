# NAS 中繼站使用說明書(mynews)

> 版本:v1.0 ｜ 移植自基金看板專案的 `infra/proxy.py` 作法。
> 適用:部署在 Streamlit Cloud / GitHub Actions(境外 IP),需抓取會封鎖境外 IP 的台灣網站
> (本專案為 **MoneyDJ ETF 成分股**)的 Python 應用。

---

## 一、為什麼需要 NAS 中繼站?

```
Streamlit Cloud(美國 IP) / GitHub Actions(Azure 美國 IP)
        │
        │  直連 → MoneyDJ 對境外 IP 回 403 封鎖
        ▼
    NAS 中繼站(台灣 IP / 你家 DDNS)
        │  Squid CONNECT 隧道(Port 3128)
        ▼
  MoneyDJ / 任何台灣網站 ✅
```

`etf_fetcher.py` 需要從 MoneyDJ 抓 ETF 成分股建立 `etf_holdings.json`,但 MoneyDJ 會對
境外 IP 回 403。NAS 中繼站作為台灣本地 HTTP Proxy(Squid),讓雲端應用借道台灣 IP 存取。

---

## 二、程式架構(本專案已內建)

| 檔案 | 角色 |
|------|------|
| `proxy_helper.py` | NAS 中繼站核心模組:讀設定、`fetch_url`(中繼+自動降級直連)、`check_proxy`(健檢) |
| `etf_fetcher.py` | `get_proxies()` 已改走 `proxy_helper.get_proxy_config()` |
| `app.py` | 側邊欄顯示中繼站狀態 + 「🧪 檢驗中繼站連線」按鈕 |
| `.github/workflows/proxy_check.yml` | 在雲端(Actions)手動檢驗中繼站是否可用 |
| `.github/workflows/update_etf.yml` | 每月透過 `PROXY_URL` 抓 MoneyDJ 成分股 |

設定來源(`proxy_helper.get_proxy_config` 依序嘗試):
1. 函式參數 `explicit`
2. 環境變數 `PROXY_URL` / `HTTPS_PROXY`(GitHub Actions / CLI)
3. Streamlit `st.secrets`:新格式 `PROXY_URL`,或舊格式 `[proxy]` section

---

## 三、NAS 端建置(給人類操作)

| 項目 | 需求 |
|------|------|
| 硬體 | Synology NAS(任何型號,能跑 Docker 或套件中心即可) |
| 套件 | DSM 套件中心安裝 **Squid**(SynoCommunity),或 Docker 跑 Squid |
| 網路 | 路由器開通 **Port 3128 TCP** 轉發至 NAS 內網 IP |
| DDNS | Synology DDNS(`yourname.synology.me`)或自訂域名 |

**Squid 最小設定(`squid.conf`):**

```
http_port 3128

auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic realm NAS Proxy
acl authenticated proxy_auth REQUIRED
acl CONNECT method CONNECT

http_access allow CONNECT authenticated
http_access allow authenticated
http_access deny all
```

**本機 curl 測試:**

```bash
curl -x http://帳號:密碼@yourname.synology.me:3128 https://www.moneydj.com/ -I
# 預期:HTTP/1.1 200 OK
```

---

## 四、設定(secrets / 環境變數)

### Streamlit Cloud(看板)
App settings → Secrets 貼入(**不要** commit `secrets.toml`):

```toml
GEMINI_API_KEY = "..."
PROXY_URL = "http://帳號:密碼@yourname.synology.me:3128"
```

本機開發:複製 `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml` 填入。

### GitHub Actions(每月自動建庫)
repo → Settings → Secrets and variables → Actions → 新增 `PROXY_URL`。
`update_etf.yml` / `proxy_check.yml` 會以同名環境變數注入。

---

## 五、檢驗中繼站是否可以使用

三種方式都呼叫 `proxy_helper.check_proxy()`(實際對 MoneyDJ 發一次請求並計時):

1. **網頁**:看板側邊欄 → 「🧪 檢驗中繼站連線」按鈕(或 ETF 反查頁的同名按鈕)。
2. **CLI / 本機**:
   ```bash
   export PROXY_URL="http://帳號:密碼@yourname.synology.me:3128"
   python proxy_helper.py        # 可用 → exit 0;不可用 → exit 非 0(供 CI)
   ```
3. **雲端(Actions)**:Actions 分頁 → 「Check NAS Proxy」→ Run workflow。

`check_proxy()` 回傳結構化結果:`ok` / `mode`(proxy|direct)/ `endpoint` / `status_code`
/ `elapsed_ms` / `bytes` / `detail`(人類可讀說明)。

---

## 六、行為矩陣

| 狀況 | 程式行為 |
|------|---------|
| 有 PROXY_URL,NAS 正常 | 走 NAS 中繼,SSL `verify=False`(Squid CONNECT 相容) |
| 無 proxy 設定 | 直連,SSL `verify=True` |
| NAS 關機(ProxyError) | 自動降級直連 |
| MoneyDJ 封鎖(403 ×2) | 提前跳出,降級直連 |
| 帳密錯誤(407) | 立即回傳 None,不重試 |
| NAS 恢復 | TTL 300s 過期後自動重接 |

---

## 七、故障排除

- **`ProxyError: Cannot connect to proxy`** → 路由器 Port 3128 未開,或 Squid 未啟動;先跑第三節 curl。
- **`407 Proxy Auth Failed`** → `PROXY_URL` 帳密有誤,或 Squid `passwd` 未更新。
- **`SSL: CERTIFICATE_VERIFY_FAILED`** → proxy 模式應 `verify=False`;確認 `get_proxy_config()` 有回非 None。
- **NAS 恢復後仍走直連** → 快取未過期;呼叫 `proxy_helper.reset_proxy_cache()` 或等 5 分鐘。

---

## 八、移植到其他專案 Checklist

```
□ 複製 proxy_helper.py 到專案根目錄
□ requirements.txt 含 requests、urllib3
□ .streamlit/secrets.toml 填入 PROXY_URL(或 [proxy] section);.gitignore 已含 .streamlit/secrets.toml
□ 抓取改用 proxy_helper.fetch_url() 或 get_proxy_config()
□ python proxy_helper.py 健檢通過
□ Streamlit Cloud / Actions Secrets 同步設定 PROXY_URL
```
