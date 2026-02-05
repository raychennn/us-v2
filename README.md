# US Stocks 強弱勢/蓄勢掃描器（NASDAQ + NYSE TG 通知）

這個專案把「橫截面相對強弱（RS Rank / 多視窗一致性）」與「形態/趨勢過濾（Trend + VCP / PowerPlay + Rebound）」結合，目標是從 **NASDAQ + NYSE** 找出：

- **強勢延伸**：相對強勢持續領先，且技術面出現可操作的收斂/整理訊號
- **第二段蓄勢**：第一段相對強勢後，進入盤整/回調冷卻，但結構未死，等待第二波

> 價格資料來源：Yahoo Finance（`yfinance`）。
>
> **全部計算都基於 Adjusted Price（`auto_adjust=True`）**，避免拆股/配息造成 RS 失真。

---

## 0) 一鍵更新全市場 watchlist（獨立腳本，不綁主程式依賴）

這個專案支援自動生成 **NASDAQ + NYSE 全市場**的 `watchlist.txt`（同時 best-effort 排除 ETF / ADR / 非普通股）。

### 來源
使用 Nasdaq Trader 提供的 Symbol Directory：
- `nasdaqlisted.txt`（NASDAQ）
- `otherlisted.txt`（NYSE 等）

### 生成 watchlist

```bash
python scripts/generate_watchlist.py --output watchlist.txt
```

可選參數：
- `--other-exchanges N,A,P`：指定 `otherlisted.txt` 中要保留的交易所（預設只保留 `N`=NYSE）
- `--include-nasdaq` / `--include-other`：是否納入 NASDAQ/OTHER
- `--debug-csv scripts/watchlist_debug.csv`：輸出剃除原因（方便你檢查漏網之魚）

> 產出的 ticker 會把 `.` 自動轉成 `-`（例如 `BRK.B` → `BRK-B`）以提升 Yahoo/yfinance 相容性。

---

## 1) Universe（掃描範圍）與剃除規則

### A. 市場範圍
- `watchlist.txt` 內的所有 ticker（建議由上面的獨立腳本定期更新）

### B. 硬性剃除（掃描前即排除）
1) **股價 < 15 美金** → 剃除（`MIN_PRICE_USD`）
2) **5 日平均成交金額 < 10,000,000 美金** → 剃除（`MIN_ADV5_DOLLAR`）
   - 定義：`ADV5_$ = mean( Close * Volume, last 5 trading days )`

### C. 商品類型剃除（由 watchlist 腳本 best-effort 排除）
- ETF / ETN / Fund / Trust
- ADR / ADS / Depositary
- Warrant / Rights / Unit / Preferred 等非普通股

> 若你想再加一層「用 Yahoo metadata 二次剃除」（更準但更慢），可以在 `config.py` 開啟 `ENABLE_YF_TYPE_FILTER=True`，只針對候選池少量 ticker 拉 metadata。

---

## 2) Benchmark（QQQ vs ^GSPC 自動選擇）

每次掃描會在 **QQQ** 與 **^GSPC** 之間選擇「當下更強」的當作 benchmark。

- 比較視窗：**20D 與 60D 加權**（可調）
- 比較指標：累積報酬（以 adjusted price 計算）

> 注意：**本專案不會**因 benchmark 下跌而取消掃描或發通知（你要求的「benchmark 下跌通報」已移除）。

---

## 3) RS / MRS（相對強弱）定義

以 adjusted close 計算：

- `log_rs = log(AdjClose_symbol) - log(AdjClose_benchmark)`
- `log_rs_ma(N) = SMA(log_rs, N)`
- `MRS_N = ( exp(log_rs - log_rs_ma(N)) - 1 ) * 100`

視窗：`1D / 5D / 20D / 60D`

- 1D 使用「當日相對報酬」作為短期衝刺衡量。

---

## 4) 篩選流程（兩階段下載，為了效能與穩定性）

### Stage 0：讀取 watchlist
- 從 `watchlist.txt` 讀入 ticker

### Stage 1（第一輪：海選 / 候選池）
- 只下載較短 period（預設 `6mo`）降低 Yahoo 壓力
- 先套用硬剃除（`MIN_PRICE_USD`, `MIN_ADV5_DOLLAR`）
- 以 60D RS（相對報酬）做橫截面排序，取 Top `RS_RANK_TOP_PCT` → **Candidate Pool**

### Stage 2（第二輪：精選 / 技術面）
- 只對 Candidate Pool 下載較長 period（預設 `1y`）
- 計算四視窗 gate（1/5/20/60）命中數：`MTF_hits >= MTF_GATE_MIN_HITS`
- 再用：Trend + VCP / PowerPlay + Rebound + Momentum 分類輸出

---

## 5) 技術面（Trend + VCP / PowerPlay）

### Trend
預設用均線排列（可在 `config.py` 調整）：
- `Close > SMA_FAST > SMA_MID > SMA_SLOW`

### VCP（波動收斂）
用收盤價標準差的比例做收斂判斷（可調）：
- `sd5/sd20`, `sd5/sd60`, `sd20/sd60`

### Power Play
爆量突破 + 突破後小幅整理（可調）：
- 量能：`Volume` 相對於均量或前一日倍數
- 突破 lookback、整理窗口等

---

## 6) Rebound（第二段蓄勢）

用 `MRS_20` 或 `MRS_60` 表示「曾強 → 冷卻 → 未死」。

- 曾強：lookback 內 `max(MRS) >= 門檻`
- 冷卻：`current <= max * cooldown_ratio`
- 未死：`current >= support_min`

建議門檻用「分位數」而非固定值（已提供參數）。

---

## 7) 最終輸出分類（5 區）

每檔股票只會出現在一個分類（依優先序歸類）：

1) ⭐️ **MTF + Rebound + VCP/PP**
2) 🧱 **MTF + VCP/PP**
3) ♻️ **Rebound + VCP/PP**
4) 🪢 **RS 血統 + 強確認（VCP+PP 同時）**
5) 🍄 **RS 血統 + VCP/PP + Momentum 🚀**

---

## 8) 自動偵測「是否有新交易日」

背景 loop 會定期（預設每 10 分鐘）抓 `TRADING_DAY_ANCHOR`（預設 `QQQ`）最新日 K：
- 若最新日 K 的日期 **未更新** → 不掃描、不推播
- 若日期 **更新（新交易日）** → 觸發掃描並推播，完成後寫入 SQLite

這個設計可以避免：
- 週末/休市日重複推播
- 夏令時間（DST）導致排程時間偏移

---

## 9) Telegram 指令

- `/now`：立即掃描一次（不管是否新交易日）
- `/check TICKER`：單股逐項檢查（回傳 ✅/❌ 與關鍵數值）
- `/help`：顯示說明

### TradingView 匯入檔（Telegram 附件）

每次掃描（`/now` 或偵測到新交易日）會額外附上一個可匯入 TradingView Watchlist 的 txt 檔：
- 檔名：`US-RS_YYMMDD.txt`（例如 `US-RS_260205.txt`）
- 內容：使用 `###` 分隔 5 個分類（⭐️ 🧱 ♻️ 🪢 🍄），每行一個 ticker

可選環境變數（見 `app/config.py`）：
- `TV_SYMBOL_PREFIX`：為每個 ticker 加上前綴（例如 `NASDAQ:` / `BINANCE:`）
- `TV_SYMBOL_SUFFIX`：為每個 ticker 加上後綴（例如 `.P`）
- `TV_EXPORT_INCLUDE_EMPTY_SECTIONS`：是否輸出空分類（預設 true）
- `TV_EXPORT_FILENAME_PREFIX`：檔名前綴（預設 `US-RS_`）

#### 自動 NASDAQ/NYSE 前綴（你這次新增的需求）

若你不設定 `TV_SYMBOL_PREFIX`（保持空字串），本專案會 **自動** 在 TradingView 匯入檔內把 ticker
補上對應的交易所前綴（例如 `NASDAQ:AAPL` / `NYSE:IBM`）。

特點：
- **有快取**：結果會寫入 SQLite（`tv_exchange_cache`），避免每次都打 Yahoo metadata。
- **有并發上限**：避免大量 metadata 查詢造成 Zeabur 轉速過慢/卡死。
- **Best-effort**：查不到交易所就不加前綴，不會讓 bot 崩潰。

相關環境變數：
- `TV_EXCHANGE_AUTO_PREFIX`：是否啟用（預設 true）
- `TV_EXCHANGE_CACHE_MAX_AGE_DAYS`：快取有效天數（預設 30）
- `TV_EXCHANGE_RESOLVE_CONCURRENCY`：metadata 查詢并發（預設 5）
- `TV_EXCHANGE_RESOLVE_MAX_RETRIES`：metadata 重試次數（預設 2）
- `TV_EXCHANGE_RESOLVE_BACKOFF_BASE_SEC`：重試 backoff 底數（預設 1.2）

可選：
- `TV_CONVERT_YAHOO_DASH_TO_DOT`：把 Yahoo class share（`BRK-B`）轉成 TradingView 常見格式（`BRK.B`）

---

## 10) Zeabur 部署（建議）

### 服務類型（很重要）

- **推薦：Worker / Background 服務**（不需要對外 HTTP 端口），最適合 Telegram long polling。
- 若你用 **Web Service**（平台會做 health check），本專案會自動在環境變數 **PORT** 上啟動 `http://0.0.0.0:$PORT/healthz`（標準庫實作，無額外依賴），避免容器被判定不健康而被重啟。

### 環境變數
必填：
- `TG_TOKEN`
- `TG_CHAT_ID`

> ⚠️ 安全提醒：請不要在 log/截圖中曝光 `TG_TOKEN`（Bot token）。若不小心貼出，請立刻到 BotFather **revoke** / 重新產生 token，並更新 Zeabur 環境變數。

可選（都在 `app/config.py`）：
- `POLL_INTERVAL_SEC`（預設 600）
- `YF_DOWNLOAD_CHUNK_SIZE`（預設 200）
- `RS_RANK_TOP_PCT`（預設 0.10）
- `MTF_GATE_MIN_HITS`（預設 3）
- `MIN_PRICE_USD`（預設 15）
- `MIN_ADV5_DOLLAR`（預設 10000000）

### 建議資源
- 記憶體至少 512MB（全市場掃描建議 1GB）

---

## 11) 快速開始

1) 生成 watchlist
```bash
python scripts/generate_watchlist.py --output watchlist.txt
```

2) 本機跑（需要設定 TG_TOKEN/TG_CHAT_ID）
```bash
pip install -r requirements.txt
python -m app.main
```

3) 部署 Zeabur
- 直接用 Dockerfile

