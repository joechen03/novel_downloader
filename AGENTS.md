# AGENTS.md

給 AI 開發助手（Claude Code、Codex、Cursor、Gemini CLI 等）的專案說明。使用者說明請見 [README.md](README.md)。

## 工作規則

- **一律使用繁體中文**：回覆、文件、程式註解、程式輸出訊息都用繁體中文。程式識別字、指令與參數名稱維持原樣。
- **修改時同步更新文件**：任何程式修改（新增/移除/更名參數、改變預設值、行為或架構調整、新的已知網站行為）都要在同一次修改中更新相關文件：
  - 使用者看得到的變化（參數、用法、注意事項）→ `README.md`
  - 架構、內部機制、開發注意事項 → `AGENTS.md`（本檔）
  - 相依套件 → `requirements.txt`
  - `CLAUDE.md` 只引用本檔，通常不需要修改。
- 不要加入自動繞過驗證碼 / Cloudflare 人機驗證的機制；遇到驗證一律交給使用者手動完成。

## 概覽

單一腳本的小說下載器，主程式為 `novel_downloader.py`，沒有套件結構或建置步驟。測試在 `tests/`。

相依套件見 `requirements.txt`（Selenium 4 使用 Selenium Manager，不需指定 chromedriver），另需本機 Chrome。需要 Python 3.10+（使用 `str | None`、海象運算子）。

程式在本機產生、已列入 `.gitignore` 的檔案：

| 路徑 | 內容 |
|---|---|
| `chrome_profile/` | 專用的 Chrome 設定檔（`CHROME_PROFILE`，第一次執行自動建立），保存 Cookie 與 Cloudflare 驗證狀態 |
| `<輸出檔所在資料夾>/.novel_cache/<輸出檔名>.jsonl` | 章節暫存（`ChapterCache`） |
| `site_rates.json` | 各網站學到的穩定速度（`SITE_RATES_FILE`） |

## 驗證方式

```bash
python -m unittest discover -s tests -v                    # 自動化測試（不開瀏覽器、不連網，約 1 秒）
python novel_downloader.py <目錄網址> --list              # 目錄判斷是否正確
python novel_downloader.py <目錄網址> --end 3 -o <暫存目錄>/test.txt  # 小範圍端到端下載
```

- **每次修改都要跑自動化測試**。測試用標準函式庫 `unittest` 撰寫（不需安裝 pytest，有裝也能直接跑 `pytest`）。
- `tests/test_novel_downloader.py`：
  - `SITE_CASES`：真實網站目錄頁樣本（`tests/fixtures/*.html`）的書名、網站名、章數、首末章，並確認沒有誤判成分頁目錄。修改目錄/書名/網站名的規則後，這些一定要繼續通過。
  - 其餘為合成 HTML 的單元測試：`url_pattern`、目錄（最新章節區塊、去重）、分頁目錄、正文擷取（廣告、符號行、重複標題）、一章多頁、暫存、組合 txt、品質檢查、調速記錄、簡轉繁 `FIX_RULES`（未安裝 opencc 時略過）。
- 新增已支援網站時，用 `python tests/save_fixture.py <目錄網址> <名稱>` 存下目錄頁樣本（會展開完整目錄、移除 script/style，檔頭註解記錄實際網址），再把預期結果加進 `SITE_CASES`。章節頁有版權內容，不要存成樣本，改用合成 HTML 測試。
- 在 Windows 擷取輸出時要設 `PYTHONIOENCODING=utf-8`，否則中文會變亂碼。
- 測試輸出檔請用 `-o` 放在暫存目錄（暫存會建在輸出檔旁的 `.novel_cache/`），不要覆蓋專案中的成品 txt。
- 對真實網站測試時避免大量請求（容易觸發 Cloudflare，見下方「已知網站行為」）。
- 強制結束程式（例如 `timeout`）可能留下佔用 `chrome_profile` 的 Chrome 程序，下次啟動會出現「無法啟動 Chrome」。只關閉命令列含 `novel_downloader\chrome_profile` 的 chrome.exe，不要關掉使用者自己的 Chrome。

## 架構

設計目標是**不針對特定網站**：不寫死任何 XPath / selector，全部靠啟發式規則。修改規則後要用 `--list` 和小範圍 `--end N` 驗證。

### 解析

- **展開完整目錄**（`expand_toc`，在 `main` 解析目錄前呼叫）：若頁面有文字符合 `_EXPAND_TOC_TEXT`（查看完整、完整目錄、全部章節…）的按鈕/連結就用 JS 點擊，等 `<a>` 數量增加（AJAX 載入，如 ixdzs）或網址路徑改變（連到另一個目錄頁）後重新解析；只有 `#錨點` 改變不算換頁。解析時以 `driver.current_url` 作為 base URL。
- **目錄判斷**（`extract_chapter_links`）：把同網域的 `<a>` 依網址格式分組（`url_pattern`：目錄路徑的數字換成 `#`，最後一段只保留副檔名，因為章節 ID 可能是英數混合，如 czbooks 的 `sk2e5`、`sknjc`），取數量最多的一組；整個容器的連結都在其他地方出現過的（「最新章節」區塊）會被丟掉；最後去重時保留「最後一次出現」的位置（`_dedup_keep_last`），讓順序從第 1 章開始。可傳入 `pattern` 只取指定格式的連結（分頁目錄用）。找到的章節少於 `MIN_TOC_LINKS` 時，`main` 改用 next 模式。
- **分頁目錄**（`collect_paginated_toc`，在 `--reverse` 之前呼叫）：`find_toc_pages` 從 `<option value>` 和文字像頁碼的連結（`_TOC_PAGE_TEXT`：數字、下一頁、尾頁、第N頁、1-100章…）找出其他目錄頁；網址必須包含書號（目錄路徑中 3 位以上的數字，避免抓到別本書的目錄），沒有書號時要在目錄資料夾底下；排除已知章節網址（不能用 `url_pattern` 排除，因為 `index_2.html` 和章節 `123.html` 格式相同）。其他頁用 `fetch_many_with_verify` 下載，只取和第一頁同格式的章節連結，沒有新章節的頁面不算；會繼續從新頁面找下一頁，最多 `MAX_TOC_PAGES` 頁。合併（`merge_toc_pages`）依網址中的數字排序（`_natural_key`），再保留最後出現的位置去重（每頁都有的「最新章節」會落在最後一頁）。**目前只有合成 HTML 測試，尚未找到真實的分頁目錄網站驗證。**
- **正文擷取**（`extract_content`）：挑出「直接」包含最多文字的元素（`_direct_text_score`），把 `<br>` / `<p>` 轉成換行，刪除符合 `AD_PATTERNS` 的短行（< 60 字）及純符號行（如「←→」）。除非 `inplace=True`，否則會先複製 soup —— 之後還要找導覽連結的呼叫端（例如 `download_by_next` → `find_link`）依賴這個複製行為。
- **輸出檔名**：預設「書名_網站名.txt」，兩者都會經過 `to_traditional`（簡體站也輸出繁體檔名）。書名來自 `extract_book_name`：`og:novel:book_name` → `og:title`/`<title>` 中《》內的文字（取 `_`/`|` 前第一段）→ 文字出現在 `<title>` 中的 h1/h2 → 標題第一段，最後去掉【完結+番外】等標記。不要直接用 `og:title`：czbooks 的 og:title 是一整串 SEO 字串。網站名來自 `extract_site_name`：`og:site_name` → `<title>` 以 `-_|` 分割的最後一段，反覆去掉 `_SITE_SUFFIX` 字尾（中文、繁體、免費、小說、閱讀、網…）→ 太長或取不到時用網域主名稱（去掉 www/m/tw/cn）。
- **標題**：優先用目錄上的名稱；若章節頁標題以它開頭且更長，改用章節頁標題。
- **正文開頭重複標題**：`extract_content` 會略過前 3 行中「包含標題、且長度不超過標題 + 30 字」的行（例如 hjwzw 的「書名第1章 xxx」）。
- **一章多頁**（`find_continuation`）：只有網址是 `<同一 stem>_N` / `-N` 時才跟隨「下一頁」。
- **簡轉繁**：統一在 `write_chapter` → `to_traditional` 處理（所有下載模式都經過這裡；暫存存的是未轉換的原文），不要在解析階段轉換，以免影響標題比對。流程：
  1. OpenCC `t2s` 先轉成純簡體，再用 `--convert` 指定的設定（預設 `s2tw`）轉回繁體。網站的「機器繁體」常是繁簡混雜（「戰斗」「腦海里」「白發」），直接 s2t 對不到詞庫；先統一成簡體才能用完整詞彙判斷。實測這樣也會順便修正網站原本的錯字（白發→白髮、干凈→乾淨、墻→牆）。
  2. 套用 `FIX_RULES`（正規表示式）補 OpenCC 詞庫處理不到的錯誤（死斗、通道里、几乎、聊天后、一齣…）。新增規則時要確認不會誤改正確用法（歇斯底里、音譯人名「諾里斯」、公里、皇后、一齣好戲），可用 `setup_converter('s2tw')` 後直接呼叫 `to_traditional` 測試。
  3. 引號 “”‘’ → 「」『』（不需 OpenCC）。
  - OpenCC 為選用相依：`setup_converter` 匯入失敗時只提示、不中斷，引號仍會轉換。
  - 輸出中仍會出現的「丑角、人云亦云、伙食、天台、氣喘吁吁、岩石、游泳、倒霉」是正確的台灣用字，不是殘留簡體。

### 下載模式

- **快速模式（預設，`download_fast`）**：不切換頁面，而是透過 `execute_async_script` 執行 `_FETCH_JS`，讓 Chrome 自己平行 `fetch()` 多個章節網址，沿用瀏覽器的 TLS 憑證信任、Cookie 與 Cloudflare 通行狀態（uukanshu.cc 憑證過期，純 Python HTTP 會連線失敗）。JS 端會先移除 `<script>/<style>` 以減少回傳資料量，並依 Content-Type 或 `<meta charset>` 解碼。
  - 原始 HTML 交給 `ProcessPoolExecutor` 用頂層函式 `parse_chapter_html` 解析：此函式必須可被 pickle，Windows 上一定要保留 `if __name__ == "__main__"`。
  - 以 `--batch-size`（預設 50）章為一批：抓第一頁 → 抓各章的後續分頁（一章多頁）→ 下載失敗或正文太短（< `MIN_CONTENT_LEN`，例如 JS 動態產生內容）的章節退回 `fetch_chapter`（Selenium 開啟頁面）→ 整批存進暫存。中斷時最多損失一批。
  - `fetch_chapter` 的 `RuntimeError` 只略過該章，但 `BlockedError`（`RuntimeError` 子類別）必須往上拋。
- **慢速模式（`--slow`，`download_by_toc`）**：逐章 `fetch_chapter` → 每章存進暫存。
- **有目錄時的共同流程（`download_toc_book`）**：
  1. 讀取暫存，略過 `--start`～`--end` 範圍內已下載的章節（`--refresh` 時不略過，新內容會蓋掉舊的）。
  2. 若沒有暫存、輸出檔已存在且 `--start > 1`（舊版下載的檔案），先把原檔改名為 `<名稱>.bak.txt`，避免被只含後半段的新檔覆寫。
  3. 下載（快速或慢速模式）。`KeyboardInterrupt` 和一般 `RuntimeError` 在這裡攔下並提示重新執行即可接續；`BlockedError` 往上交給 `main`。
  4. 不論是否完成（`finally`），都用 `write_book` 把暫存中**整本**已下載的章節依目錄順序重新寫成 txt（不只這次的範圍），並列出缺少的章節範圍。
  5. 記錄網站速度（見下方），再用 `check_quality` 列出可疑章節。
- **章節暫存（`ChapterCache`）**：JSON Lines，一行一章 `{"url", "title", "content"}`，以章節網址為鍵（目錄增加新章節或 `--reverse` 都不影響），同網址以最後一筆為準。每章寫入後 `flush`；讀取時略過壞掉的行（中斷時寫到一半），續寫前若檔尾不是換行會先補換行。正文為空的章節不存，下次會重抓。
- **品質檢查（`check_quality`）**：以字數中位數為基準，標出少於 30%（且至少 `MIN_CONTENT_LEN`）、多於 4 倍（且超過 2 萬字）的章節，以及去掉空白後前 200 字和前面某章相同的章節。實測抓到 hjwzw 第 173 章「第一卷總結兼請假」的內容其實是第 73 章（網站本身的錯誤）。
- **next 模式（`download_by_next`）**：跟隨「下一章」連結，網址格式改變（例如回到目錄）時停止。每章直接寫檔，不使用暫存（續傳用 `--append`）。
- **Selenium 指令 120 秒逾時**：限速時 `fetch_many` 會把網址切段，讓每次 `execute_async_script` 約 60 秒內完成。修改批次或限速邏輯時要注意這點。

### 自動調速、Cloudflare 與視窗

- JS 端把回應分成三類：403 或 Cloudflare 驗證頁特徵 → `__CHALLENGE__`；429/503 → `__RATELIMIT__`；其他 → HTML。
- `fetch_many_with_verify` 以「約 10 秒的量」（`rate × 10` 頁，最少 10）為一段逐段下載，並自動調速（參數 `RATE_*` 在檔案中段）：
  - 限流：等 `RATELIMIT_WAIT` 秒，速度 × `RATE_DOWN`（不限速時改為 `RATE_START`），被擋頁面優先重抓。
  - 整段順利：速度 × `RATE_UP`（上限 `RATE_MAX`），逐步逼近網站上限。
  - 人機驗證，或連續 3 段整段都被限流：降速（不限速 → 每秒 2 頁 → 每次減半，最低 0.2）並降低同時下載數，再呼叫 `wait_for_human`。
  - 之前曾把 503 一律當成人機驗證並永久降到每秒 2 頁，導致 hjwzw 100 章要 21 秒；分開處理後約 2 秒。
- **記住網站速度**：`fetch_many_with_verify` 把每段的速度記到 `state["ok"]`（整段順利）或 `state["limited"]`（被限流/驗證；不限速時不記）。`learned_rate` 取「低於所有被擋速度的最高順利速度」，下載結束時以網域為鍵存進 `site_rates.json`；從頭到尾不限速也沒被擋時不記錄。`main` 在 `--rate` 未指定（預設 `None`）時讀取這個值當起始速度；`--rate 0` 表示明確不限速。目錄分頁與章節下載共用同一個 `state`（`args.state`）。
- **Chrome 設定檔**：預設為程式旁的 `chrome_profile/`（不再寫死使用者電腦的路徑）。`build_driver` 遇到 `SessionNotCreatedException` 時改拋 `RuntimeError` 並提示設定檔可能被佔用。
- `wait_for_human` 會把平常最小化的視窗叫出來（`WINDOW["minimized"]`），輪詢到驗證頁消失後再縮回去。
- 直接封鎖頁由 `is_blocked_page` 偵測並拋出 `BlockedError`，`main` 會提示原因（例如 headless 被擋）。
- `is_challenge_page` 只比對 Cloudflare 的技術特徵（`challenge-platform`、`cf-chl` 等），不比對「驗證」等中文字，避免小說內文誤判。

## 已知網站行為

測試狀態總覽（2026-11，優先支援繁體網站）：

| 網站 | 語言 | 狀態 |
|---|---|---|
| tw.hjwzw.com（黃金屋） | 繁（機器轉換） | ✅ 正常，自動調速 |
| uukanshu.cc（UU看書） | 繁 | ✅ 正常，但約每 30 請求需人機驗證，很慢 |
| czbooks.net（小說狂人） | 繁 | ✅ 正常 |
| ixdzs.tw（愛下電子書） | 繁 | ✅ 正常（需展開完整目錄） |
| 69shuba.com（69書吧） | 簡 | ❌ 書頁（`/book/<id>.htm`）抓到的是推薦書；正文多數需瀏覽器重載且很短。尚未處理 |
| 81txt.net（八一中文，xbiquge.so 轉址） | 簡 | ❌ 抓到其他書的連結、正文很短。尚未處理 |
| piaotia.com（飄天文學）、hetushu.com（和圖書） | — | ⚠ 首頁即要求人機驗證，未測 |
| ttkan.co（天天看小說） | 簡 | ⚠ 自訂「正在验证浏览器」頁，`is_challenge_page` 認不出，未測 |

### czbooks.net（小說狂人）

測試對象：`https://czbooks.net/n/sklkm61fg`（《折月亮》，236 章）。章節網址為 `/n/<書ID>/<英數章節ID>?chapterNumber=N`。og:title 是 SEO 字串，書名需從《》取出。每章約 1700 字，5 章 0.7 秒。

- 章名只有「第N頁」是網站本身的設計：網站把整本書按固定長度切頁，章節頁的 `<h1>` 也是「《書名》第N頁」，正文中沒有章名，無法取得真正的章名。

### ixdzs.tw（愛下電子書）

測試對象：`https://ixdzs.tw/read/586712/`（757 章）。書頁只列最新 100 章（新→舊），點「查看完整章節目錄>>>」後以 AJAX（POST `/novel/html`）載入完整目錄，由 `expand_toc` 處理。有 `og:novel:book_name` 與 `og:site_name`。

### tw.hjwzw.com（黃金屋）

測試對象：`https://tw.hjwzw.com/Book/Chapter/47403`（《術師手冊》，1381 章；章節網址為 `/Book/Read/47403,<id>`）。

- 限流方式：一次同時約 20 個請求後回傳 503（沒有 Retry-After，不需人機驗證），幾秒後即恢復。實測穩定每秒 15 頁不會被擋、每秒 25 頁會被擋。
- 自動調速下：100 章約 2 秒；整本 1381 章約 140 秒（中途被限流 3 次）。改為每批存暫存後，300 章約 32 秒，學到的速度約每秒 19.5 頁。
- 第 173 章「第一卷總結兼請假」網站上的內容是第 73 章的重複（網站錯誤，品質檢查會標出）。
- `<title>` 為「術師手冊/聽日/txt下載-黃金屋中文」，網站名稱取為「黃金屋」。
- 內容是從簡體機器轉換的繁體，會殘留簡體字（那么、什么、圣、愿、戰斗、腦海里、白發…）並使用 “” 引號，靠上述簡轉繁流程處理。
- 每章正文開頭會重複一行「書名第N章 標題」，已由重複標題過濾處理。

### uukanshu.cc

測試對象：`https://uukanshu.cc/book/17472/`（《術師手冊》，1370 章）。

- 不論是否限速（每秒 1 頁、0.5 頁都測過），大約每 30 個請求就出現 Cloudflare 驗證，每次約需 2 分鐘才放行；實際速度約每秒 0.25 頁（整本約 90 分鐘）。把 Accept 標頭改成瀏覽器開網頁用的標準值沒有幫助。
- headless Chrome 會被直接封鎖（「Sorry, you have been blocked」）；最小化的一般視窗可正常使用。
- 最後兩章番外夾雜其他小說的段落，是網站本身的問題，不是擷取錯誤。
