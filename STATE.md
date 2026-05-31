# STATE.md — 專案戰情室

> 最後更新:2026-05-31(B5 房市排程實測、B6 房市併入 LINE 推播、B7 歷年房價改增量更新)

## 當前環境

- 語言/執行:Python 3.11
- 相依:`google-genai`、`streamlit`、`pandas`、`requests`(見 `requirements.txt`);RSS 爬蟲用標準函式庫
- 自動化:
  - `daily_update.yml`:每日 UTC 00:00(台灣 08:00)產戰略報告/趨勢雷達/台股觀察/房市觀察
  - `update_etf.yml`:每月 1 號透過代理抓 MoneyDJ + 實價登錄,更新 ETF 成分股 + 台股收盤價 + ETF 圖鑑 + 各縣市房價
- 金鑰/設定(GitHub Secrets 或 Streamlit Secrets):
  - `GEMINI_API_KEY`(必,支援複數 key 容錯)
  - `PROXY_URL`(NAS 代理,供 ETF 爬蟲走 MoneyDJ;格式 http://帳:密@host:3128)
  - `GITHUB_TOKEN`(選,看板「💾 直接存到 GitHub」用;fine-grained PAT 限本 repo、Contents 讀寫)
  - `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO`(選)
- 看板:Streamlit Community Cloud(`*.streamlit.app`),共 6 頁
  (戰略報告 / 趨勢雷達 / 台股觀察 / 房市觀察 / ETF持股反查 / ETF圖鑑)

## 架構摘要

RSS 爬蟲抓真實新聞 → Gemini 全包分析;另有 ETF 成分股反查(透過 NAS 代理抓 MoneyDJ)。
抓新聞:動態分類頭條(世界/財經)為主 + 聚焦關鍵字(聯準會/股匯債/地緣軍事)為輔。

## 已完成 ✅

- [x] `news_fetcher.py` — 標準函式庫 RSS/Atom 爬蟲;繁中/台灣來源;動態分類頭條 + 聚焦關鍵字;
      去 HTML/去重/時間排序;每則標 `origin`(來源管道)
- [x] `update_data.py` — Gemini 全包:四維度戰略分析 + 白話文、趨勢雷達、台股觀察;
      多把金鑰容錯 `get_gemini_keys()`;穩健 JSON 清理 + 驗證;失敗隔離
- [x] 看板 6 頁(`app.py`):
      - 戰略報告:① 抓新聞 → ② Gemini 分析+白話文(兩步手動)
      - 趨勢雷達:① 抓產業新聞 → ② Gemini 排名打分
      - 台股觀察:① 抓財經新聞 → ② Gemini 整理(總表/利多/利空/觀望 + 趨勢/夕陽),
        並併入「ETF 持有檔數」交叉參照
      - ETF 持股反查:輸入個股代號/名稱 → 反查被哪些 ETF 持有;🛰️ 代理按鈕即時建庫;
        篩選(被幾檔 ETF 持有 + 股價範圍);網頁新增(只輸代號、批次、自動抓名稱)/移除 ETF
      - 房市觀察:① 抓房市新聞 → ② Gemini 判讀預售/成屋冷熱 + 打房政策 + 縣市標記;
        🛰️ 代理抓內政部實價登錄各縣市每坪房價(成屋/預售),plotly 互動 choropleth 台灣地圖
        (房價/新聞冷熱可切換、高鐵縣市★標記)+ 排行表(含交通)+ 逐筆成交佐證;
        圖表:各縣市均價長條圖(依高鐵/自強號上色)、交通便利 vs 無軌道均價對比、
        單一縣市歷年每坪折線圖(house_price_history.json,逐季抓多年彙整)
      - 房市觀察強化:房市冷熱/打房政策併入每日 LINE 推播;歷年房價改「增量更新」
        (內部 `_acc` 累計 + `seasons_included`,每月只補最新季,省時省流量);
        每日排程房市步驟已模擬實測(產合法 latest_housing.json、濾非法縣市、帶入真實房價參考)
      - ETF 圖鑑:抓 MoneyDJ 基本資料建庫,篩選器(型態/區域/配息頻率/配息月份/
        主題理念/策略/內扣費用)
- [x] `etf_holdings.py` / `etf_holdings.json` — 個股→ETF 反查(純資料)
- [x] `etf_fetcher.py` / `etf_sources.json` — 透過 `PROXY_URL` 代理抓 MoneyDJ 成分股建庫
      (requests + proxies、HTML 表格解析、單檔失敗不影響其他、抓不到保留既有)
- [x] 金鑰診斷:Streamlit 讀不到金鑰時列出 Secrets 名稱與正確 TOML 寫法
- [x] 每日/每月 GitHub Actions 排程 + 自動 commit;LINE 推播(最佳努力)
- [x] 文件:`CLAUDE.md` / `STATE.md` / `README.md`
- [x] NAS 中繼站(參照基金 `infra/proxy.py` 移植):`proxy_helper.py` 統一讀設定
      (explicit > 環境變數 > st.secrets,新格式 `PROXY_URL` + 舊格式 `[proxy]`)、
      `fetch_url` 中繼+自動降級直連;`etf_fetcher.get_proxies()` 改委派 proxy_helper
- [x] **檢驗中繼站是否可用**:`proxy_helper.check_proxy()`(實際探測 MoneyDJ + 計時),
      三入口 — 看板側邊欄/ETF 頁「🧪 檢驗中繼站連線」按鈕、`python proxy_helper.py` CLI、
      `.github/workflows/proxy_check.yml`(雲端手動健檢)
- [x] ETF 收錄清單擴充至 66 檔台股 ETF(含 5 檔主動式 00980A~00984A,集中於末段)
- [x] `price_fetcher.py` — 透過代理抓臺灣證交所(上市 STOCK_DAY_ALL)+ 櫃買(上櫃 opendata)
      收盤價,存 `stock_prices.json`;單一來源失敗不影響另一個
- [x] ETF 反查頁兩個篩選:① 至少被幾檔 ETF 持有(滑桿)② 股價範圍(滑桿,有股價才啟用,
      可勾「只看有股價」);表格加「股價」欄、即時顯示符合檔數、下載篩選結果;
      「💰 股價資料」面板可按鈕更新收盤價並下載
- [x] `update_etf.yml` 每月一併抓股價(失敗不影響成分股),commit etf_holdings.json + stock_prices.json
- [x] ETF 清單管理(`etf_fetcher`):網頁新增(只輸代號、批次貼、自動抓名稱、重複/格式檢查)、
      移除(多選);本機直接寫檔、雲端唯讀則下載 etf_sources.json commit 回 repo
- [x] `etf_profile_fetcher.py` / `etf_profiles.json` — 透過代理抓 MoneyDJ **Basic0004(簡介頁)**,
      依真實欄位精準解析:型態←投資標的、區域←投資區域、配息頻率←配息頻率、經理費←經理費(%)、
      總費用←總管理費用(%)、殖利率←殖利率(%)、規模←ETF規模、追蹤指數、經理人、發行商、策略敘述、主題標籤;
      沿用同一份 ETF 清單;含「🔬 診斷單檔欄位」(可選 0004/0003/0001 頁)校正工具
- [x] ETF 圖鑑頁篩選器:型態、投資區域、配息頻率、配息月份、主題理念、策略、總管理費用上限、
      ETF 市價範圍;表格含市價/配息月/殖利率/總費用/規模/經理人/發行商/追蹤指數
- [x] 配息月份:抓 MoneyDJ **Basic0005 配息記錄頁**取真實除息月份(每列只取除息日,
      避免混入發放日);抓不到才退回頻率推測(月配=1~12 確定,季配等標 *)。
      頁面位置由 WebSearch 查得(WebFetch 抓 MoneyDJ 會 403,故由 NAS 代理抓)
- [x] ETF 市價:從 Basic0004 解析 ETF市價/ETF淨值(price/nav)入圖鑑
- [x] `github_store.py` + 看板「💾 直接存到 GitHub」按鈕(常駐顯示):用 GITHUB_TOKEN 經
      Contents API 把 etf_holdings/etf_sources/etf_profiles/stock_prices.json 一鍵 commit 回 repo,
      免手動下載上傳;token 偵測容錯(GITHUB_TOKEN/GH_TOKEN/[github]),未設時提示並列 Secrets 名稱
- [x] **側邊欄全域設定「💾 抓取後自動存到 GitHub」**(預設開):勾一次,成分股/圖鑑/股價
      三個抓取完成都自動 commit 回 repo,解決 session 被清掉而誤存回 seed 的問題
- [x] 成分股真實庫已建立:`etf_holdings.json` = 61 檔(MoneyDJ via proxy),涵蓋全市場個股
- [x] **房市觀察(第 6 頁)**:`housing_fetcher.py`(房市新聞 + 內政部實價登錄各縣市每坪房價,
      成屋 `_a`/預售 `_b`,每坪=單價元平方公尺×3.305785,排除車位/店辦/土地、過濾離群值,
      逐季往前試到抓得到、走 NAS 代理);`update_data.get_housing_analysis()`(Gemini 判讀
      預售/成屋冷熱 + 打房政策 + 22 縣市標記);`taiwan_counties.geo.json`(g0v 圖資簡化正名、
      120KB);plotly 互動 choropleth(房價/冷熱切換)+ 排行 + 逐筆佐證;一鍵存 GitHub;
      每日排程產 `latest_housing.json`、每月排程抓 `house_prices.json`
- [x] **代碼淨化與收尾完成**:全專案 pyflakes 零警告;清理 `etf_profile_fetcher.py`
      未使用的 `import io` 與函式內重複 `HTMLParser` 局部 import(只減不改,邏輯無損、通過驗證)
- [x] **Gemini AI 分析上線**:已設 `GEMINI_API_KEY`,戰略報告頁實測產出四維度分析 + 白話文
      (白話文來源=gemini),確認 RSS 爬蟲 → Gemini 全流程通暢

## 已驗證上線(實測通過)✅

- [x] 成分股實測:已透過代理抓 MoneyDJ 並一鍵自動存檔,`etf_holdings.json` = 78 檔真實成分股
- [x] 股價實測:TWSE/TPEx 已抓並自動存檔,`stock_prices.json` ≈ 7176 檔收盤價
- [x] 主動式 ETF(A 結尾)成分股改抓 Basic0007B;抓取摘要顯示成功/失敗清單

## 待辦 / 可優化 ⏳

- [ ] 在 Streamlit 按「🌐 匯入全台股 ETF 清單」一鍵帶入全市場 ETF,再抓成分股/圖鑑(全市場化)
- [ ] 在 Streamlit 按「🔄 抓取 ETF 圖鑑」全清單重抓 Basic0004/0005,確認型態/配息/費用/月份正確並自動存檔
- [x] 設定 `GEMINI_API_KEY`(三頁 AI 分析:戰略報告/趨勢雷達/台股觀察 + 每日排程)— 已完成,戰略報告實測通過
- [ ] (選)在 repo Secrets 設 `PROXY_URL` 讓每月排程自動抓 ETF + 股價(NAS 防火牆需放行 Actions IP)
- [ ] 仍抓不到的個別 ETF:依抓取摘要失敗清單,查正確 etfid/頁面後修正
- [ ] 可考慮多主題報告
- [ ] 手動刪除已合併的 `claude/brave-ramanujan-fTxA0` 分支(雲端 git 代理擋刪分支 403,
      請於 PR #16 頁面或 GitHub 分支列表手動刪)

## 已知問題 / 注意事項 ⚠️

- 沙箱/本機無外網時,RSS 與 MoneyDJ 都抓不到;Streamlit Cloud / Actions 網路正常。
- MoneyDJ 實際 HTML 結構/`etfid` 需用線上回應驗證,解析器可能需微調一次。
- ETF 成分股屬事實資料;請維持合理抓取頻率並留意來源網站服務條款。
- `PROXY_URL` 含帳密,只走環境變數/Secrets;`.streamlit/secrets.toml` 已 gitignore,不可進版控。
- 「金鑰讀不到」多為命名/格式問題:名稱需為 `GEMINI_API_KEY`,複數 key 用逗號或陣列。
- Gemini 模型(`gemini-2.5-flash`)若不可用,以 `GEMINI_MODEL` 覆寫。
- ETF 圖鑑配息月份:Basic0004 只有頻率,真實月份改抓 Basic0005 配息記錄頁;若該頁無近期
  配息記錄(新成立 ETF)則退回頻率推測值(表格標 *)。
- 成分股抓取約有 ~11 檔抓不到(0058/0059 規模小、00948/00950 及多檔 00xxxA 主動式為
  新發行),屬 MoneyDJ 該檔尚無/不提供成分股表,**非程式 bug**;仍保留在清單,待來源
  補上資料後下次重抓即自動補入,失敗摘要會持續列出可追蹤。
- 所有產出為 AI/工具自動生成,僅供參考,非投資建議。
