# GOTCHAS — 踩過的坑（事故記憶庫）

> 每條 ＝ 症狀 → 根因 → 對策。動工碰到對應領域**先讀本檔**；結案踩新坑就追加一條。
> 這裡是血淚教訓，請優先信任，勝過「聽起來合理」的直覺。（見 CLAUDE.md §6）

## 雲端部署（Streamlit Cloud）

### cp314 × 未鎖依賴 → 整站 Segfault
- **症狀**：部署後反覆整站當機（`Segmentation fault`，原生層、無 Python 堆疊），開站數秒即死、每次重啟必復發。
- **根因**：雲端 venv **只在環境層變更時**才重建（僅改 code 的部署沿用舊 venv）。任何觸發重建的變更（新增 `packages.txt`／改 `requirements.txt`）會**一次抓進多個當下最新 wheel**；原生套件的 cp314 新 wheel 可能載入即段錯。頭號嫌犯 pyarrow：`st.dataframe` 必經其 C++ 核心，且有 arm64 載入段錯前科（apache/arrow#44342）。
- **對策**：關鍵原生套件（pyarrow/numpy）**常態鎖上限**。要解鎖或動任何觸發重建的變更 → **一次只解一個、每步合併後看雲端**（相依序列，見 §3 DAG）。字型（`packages.txt` 的 `fonts-noto-cjk`）本身無罪，鎖版在時可安全恢復。
- **實證（2026-07-23，A/B 對照）**：cp311 沙箱下 pyarrow **25.0.0 → 段錯誤（exit 139）**、**24.0.0 → 乾淨**（同一組擬真變體）→ **pyarrow 25 為確定兇手（非消去法）**。觸發需「擬真 DataFrame（datetime/object/NaN 欄）＋ 多次 Streamlit rerun」；trivial 3 列 df ＋ 2 rerun **不觸發**（故最小重現要夠真）。`app.py::_pyarrow_guard()` 偵測 pyarrow≥25 開站即 `st.error` 告警，把靜默 segfault 變可見診斷。

### packages.txt 格式
- **症狀/對策**：`packages.txt` **一行一套件、不可加註解**，否則雲端 apt 解析失敗、字型裝不上。

### 沙箱驗不到雲端
- **症狀**：想 WebFetch 看板確認部署 → 403（CONNECT tunnel failed）。
- **根因**：本環境網路政策擋 `*.streamlit.app`（代理層 403）；且沙箱 **cp311**、雲端 **cp314** → cp314 專屬崩潰本機無法重現。
- **對策**：雲端驗收**靠使用者看板回報**（能開＋季節圖渲染＋無紅字）或請他貼 Manage app 建置日誌；誠實標「無法沙箱驗」。

## 環境變數 / Secrets

### env_bool 空字串誤判
- **症狀**：預設 False 的旗標（如 `PUSH_ALL_DAYS`）在雲端恆 True，守門失效。
- **根因**：GitHub Actions 對**未定義的 `vars.X` 注入空字串**（非「未設定」）；`env_bool` 只把 `None` 當未設。
- **對策**：`env_bool` 空/純空白一律回 default。測環境變數必測**四態：未設／真／假／空字串**。

### Secret vs Variable
- **對策**：機密（token／ping URL）走 **Secrets**；非機密且需露出的（`DASHBOARD_URL` 看板連結）走 **Variables**。分頁不同、別放錯。

## NAS 觸發架構

### NAS 是觸發器、不是執行器
- **症狀**：以為要在 NAS 上設某 env／跑 `update_data`。
- **根因**：`scripts/nas_trigger.py` 只對 GitHub API 發 `workflow_dispatch`；`update_data.py` **一律在 GitHub Actions 內執行**（NAS 06:00 主觸發、GitHub schedule 06:40/07:30 兜底）。
- **對策**：管線用的 env **只需設 repo Secret/Variable**、不必碰 NAS。NAS 端 `nas_line_bot.py`/`nas_trigger.py` 刻意零專案相依（SSOT 例外、就地註明）。

## 外部監控

### hc-ping.com GET 一次 ＝ 一次假存活
- **症狀**：想「探活」healthchecks 心跳 URL。
- **根因**：對 `hc-ping.com/<uuid>` 發 GET **就等於回報一次今天存活**，會蓋掉真正的 DOWN 警報。
- **對策**：**嚴禁 WebFetch/curl 心跳 URL**。它是輕機密、非看板網址，別設成 `DASHBOARD_URL`。

## GitHub API / PR 操作

### API rate limit
- **症狀**：MCP 合併/改 PR 回「API rate limit already exceeded」。
- **根因**：MCP 反覆重連 + webhook 事件 + 高頻 PR 操作燒掉每小時上限。
- **對策**：高頻操作**省呼叫**（跳過非必要 get）；撞牆**別空轉重試（§5）**，改請使用者**在 UI 合**（瀏覽器不受該 API 限制）。

### squash 慣例 + 分支重用
- **對策**：本 repo PR 一律 **squash**。同分支跨 PR 重用時，squash 後 origin 分支落後成孤兒 commit → 下次推用 `--force-with-lease`（**僅在分支只含已併入歷史時**安全）。follow-up 先 `git checkout -B <branch> origin/main` 重建到最新 main。
