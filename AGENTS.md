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

小說下載器。執行入口是根目錄的 `novel_downloader.py`（只有幾行，呼叫 `novel.cli.main`），程式本體在 `novel/` 套件，沒有建置步驟。測試在 `tests/`。

| 模組 | 內容 | 相依 |
|---|---|---|
| `novel/config.py` | 共用常數與門檻（路徑、`AD_PATTERNS`、`REMOVE_TAGS`、雜訊行門檻…） | — |
| `novel/converter.py` | 簡轉繁 `setup_converter` / `to_traditional`、`FIX_RULES` | — |
| `novel/browser.py` | 啟動 Chrome、`kill_stale_chrome`、`load_soup`、人機驗證、`BlockedError`、`acquire_lock` | config |
| `novel/fetcher.py` | 快速模式的瀏覽器內 `fetch()`（啟動 + 輪詢）、自動調速、`site_rates.json`、`fetch_binary` | browser |
| `novel/toc.py` | 目錄頁：章節連結、書名/網站名/作者、`expand_toc`、分頁目錄、分卷、封面網址 | fetcher |
| `novel/chapter.py` | 章節頁：`extract_content`、`title_key`、一章多頁、`parse_chapter_html` | browser |
| `novel/storage.py` | `ChapterCache`、書籍資訊檔、封面檔、`safe_filename` | config |
| `novel/exporter.py` | `collect_book`（含刪除雜訊行）、`write_txt`、`write_epub`、`export_book` | converter, storage |
| `novel/quality.py` | `check_quality`、`chapter_number` | storage |
| `novel/downloader.py` | 快速/慢速/next 模式、`run_and_export`、`download_cover` | 以上各模組 |
| `novel/cli.py` | 參數、多本書與 `--update-all`、`process_book`（單本書的流程） | 以上各模組 |

- 模組之間只能單向相依（上表由上往下），不要產生循環 import。
- **會被測試修改的模組層級設定，必須定義在實際使用它的模組裡**：`from .config import X` 是複製一份，測試改 `config.X` 不會影響已匯入的模組。目前 `POLL_INTERVAL`、`SITE_RATES_FILE` 因此放在 `fetcher.py`；測試要改 `fetcher.POLL_INTERVAL`。`CONVERTER`、`WINDOW` 是 dict，各模組共用同一個物件，修改內容即可。
- 解析用的子進程（Windows 用 spawn）會重新匯入 `novel_downloader.py`，所以入口檔一定要保留 `if __name__ == "__main__"`；`parse_chapter_html` 必須是可 pickle 的頂層函式。

相依套件見 `requirements.txt`（Selenium 4 使用 Selenium Manager，不需指定 chromedriver；需要 `Service(popen_kw=...)`，已在 4.51 測試），另需本機 Chrome。需要 Python 3.10+（使用 `str | None`、海象運算子）。EPUB 用標準函式庫 `zipfile` 產生，鎖定用 `msvcrt` / `fcntl`，都沒有額外相依。

程式在本機產生、已列入 `.gitignore` 的檔案：

| 路徑 | 內容 |
|---|---|
| `chrome_profile/` | 專用的 Chrome 設定檔（`CHROME_PROFILE`，第一次執行自動建立），保存 Cookie 與 Cloudflare 驗證狀態；內含鎖定檔 `novel_downloader.lock` |
| `downloads/` | 預設輸出資料夾（`DOWNLOAD_DIR`） |
| `<輸出檔所在資料夾>/.novel_cache/<輸出檔名>.jsonl` | 章節暫存（`ChapterCache`） |
| `<輸出檔所在資料夾>/.novel_cache/<輸出檔名>.book.json` | 書籍資訊（網址與下載設定，`--update-all` 用） |
| `<輸出檔所在資料夾>/.novel_cache/<輸出檔名>.cover.<jpg/png/…>` | 封面圖檔（EPUB 用） |
| `site_rates.json` | 各網站學到的穩定速度（`SITE_RATES_FILE`） |

`downloads/` 中的 `術師手冊_*.txt` 是使用者的成品，不要提交、不要覆蓋。

## 驗證方式

```bash
python -m unittest discover -s tests -v                    # 自動化測試（不開瀏覽器、不連網，約 15 秒）
python novel_downloader.py <目錄網址> --list              # 目錄判斷是否正確
python novel_downloader.py <目錄網址> --end 3 -o <暫存目錄>/test.txt  # 小範圍端到端下載
```

- **每次修改都要跑自動化測試**。測試用標準函式庫 `unittest` 撰寫（不需安裝 pytest，有裝也能直接跑 `pytest`）。
- `tests/test_novel_downloader.py`：
  - `SITE_CASES`：真實網站目錄頁樣本（`tests/fixtures/*.html`）的書名、網站名、章數、首末章，並確認沒有誤判成分頁目錄。修改目錄/書名/網站名的規則後，這些一定要繼續通過。
  - 手機版黃金屋的範圍目錄樣本（`hjwzw_mobile_index`：14 個「第0000--0100章」連結；`hjwzw_mobile_list`：其中一頁的 100 章），測 `is_toc_index`。
  - 其餘為合成 HTML 的單元測試：`url_pattern`、目錄（最新章節區塊、去重）、分頁目錄、`find_toc_link`、正文擷取（廣告、符號行、重複標題、整頁包在 `<form>` 裡）、一章多頁、暫存、組合 txt、品質檢查、調速記錄、簡轉繁 `FIX_RULES`（未安裝 opencc 時略過）。
- `tests/test_flow.py`：用 `FakeDriver` 模擬網站，測整個下載流程（`download_toc_book` / `download_next_book`）：中斷（`interrupt_after` 第 N 次呼叫後丟 `KeyboardInterrupt`）→ 續傳只抓剩下的章節、回傳狀態、`--refresh`、慢速模式、快速模式失敗退回瀏覽器、舊檔備份、next 模式續傳與連載更新、範圍目錄合併、雜訊行刪除與 `clean=False`、自動重抓可疑章節（暫時性錯誤被修正、網站錯誤只重抓一次、`--refresh` 清除標記、`no_retry`）。另有 EPUB（所有 XML 必須合法、mimetype 必須是第一個且不壓縮；`check_epub_consistency` 檢查 manifest/spine/目錄連結/id/playOrder 的一致性；分卷、封面）、分卷偵測、封面網址、書籍資訊與 `build_jobs`、網址清單、鎖定、預估時間、`chapter_number`、`output_base`。`FakeDriver.execute_script` 依 `js is fetcher._FETCH_START_JS` / `fetcher._POLL_JS` 判斷是哪個呼叫，改這兩段 JS 的名稱或參數時要一起改。測試會把 `fetcher.POLL_INTERVAL` 設成 0、`fetcher.SITE_RATES_FILE` 指到暫存目錄。
- 測試直接匯入各模組（`from novel import toc, exporter, …`），用 `模組.名稱` 呼叫。
- **EPUB 已通過官方 epubcheck 5.4.0（EPUB 3.4 規則）**：ixdzs、ttkan、69shuba 各 3 章（含封面）、hjwzw 100 章，以及一本含分卷、封面、`& < > "` 與控制字元的合成書，全部 0 錯誤 0 警告（2026-11）。改了 `write_epub` 後建議重新驗證：epubcheck 需要 Java（這台電腦沒有安裝，可用 Eclipse Adoptium 的免安裝 JRE zip，epubcheck 的 jar 可從 `pip download epubcheck` 的 wheel 中取出），執行 `java -jar epubcheck.jar 書.epub`。平常的自動化測試靠 `check_epub_consistency` 檢查結構。
- 新增已支援網站時，用 `python tests/save_fixture.py <目錄網址> <名稱>` 存下目錄頁樣本（會展開完整目錄、移除 script/style，檔頭註解記錄實際網址），再把預期結果加進 `SITE_CASES`。章節頁有版權內容，不要存成樣本，改用合成 HTML 測試。
- 在 Windows 擷取輸出時要設 `PYTHONIOENCODING=utf-8`，否則中文會變亂碼。
- 測試輸出檔請用 `-o` 放在暫存目錄（暫存會建在輸出檔旁的 `.novel_cache/`），不要覆蓋 `downloads/` 中的成品。一次給多個網址時不能用 `-o`，輸出會進 `downloads/`，測完要移走。
- 同時只能執行一個下載器（共用 `chrome_profile` 的鎖）；寫測試用的探測腳本時也要 `acquire_lock`，否則會和正在執行的下載器互相干擾。
- 對真實網站測試時避免大量請求（容易觸發 Cloudflare，見下方「已知網站行為」）。
- 強制結束程式（例如 `timeout`）可能留下佔用 `chrome_profile` 的 Chrome 程序；下次啟動時 `build_driver` 會自動清掉（見下方）。手動清理時只關閉命令列含 `novel_downloader\chrome_profile` 的 chrome.exe，不要關掉使用者自己的 Chrome。
- **測試真正的 Ctrl+C**（Windows）：從 Claude Code 等工具啟動的程序，Ctrl+C 處理常被停用且會被子程序繼承，直接送訊號測不出來。做法是用 `CREATE_NEW_CONSOLE` 開新主控台，在裡面先呼叫 `SetConsoleCtrlHandler(None, False)` 重新啟用 Ctrl+C、自己用 Python 函式（不是 `SIG_IGN`，它會被子程序繼承）擋掉訊號，啟動下載器後用 `GenerateConsoleCtrlEvent(0, 0)` 對整個主控台送 Ctrl+C。檢查：沒有錯誤堆疊、幾秒內結束、沒有殘留 Chrome、已下載的章節有輸出。

## 架構

設計目標是**不針對特定網站**：不寫死任何 XPath / selector，全部靠啟發式規則。修改規則後要用 `--list` 和小範圍 `--end N` 驗證。

### 解析

- **單本書的流程**（`process_book`）：載入網址 → 書名/網站名/作者 → `expand_toc` → `extract_chapter_links` →（章節少於 `FEW_CHAPTERS` 且有「目錄」連結時改開目錄頁）→ `collect_paginated_toc` → `--reverse` → 分卷 → 記錄書籍資訊 → 下載封面（EPUB 時）→ `download_toc_book`；章節少於 `MIN_TOC_LINKS` 時改用 next 模式。
- **書籍介紹頁 → 目錄頁**（`find_toc_link`）：找到的章節少於 `FEW_CHAPTERS`（30）時，若頁面有文字剛好是「目錄」「章節目錄」等（`_TOC_LINK_TEXT`，完全相符）的連結、同網站、不是目前這頁，且有書號時網址包含書號，就開啟它；那一頁的章節比較多才採用。手機版黃金屋的書頁只列最新 5 章，靠這個進入目錄頁。
- **展開完整目錄**（`expand_toc`，在解析目錄前呼叫）：若頁面有文字符合 `_EXPAND_TOC_TEXT`（查看完整、完整目錄、全部章節…）的按鈕/連結就用 JS 點擊，等 `<a>` 數量增加（AJAX 載入，如 ixdzs）或網址路徑改變（連到另一個目錄頁，如 69shuba 書頁的「完整目录」→ `/book/<id>/`）後重新解析；只有 `#錨點` 改變不算換頁。解析時以 `driver.current_url` 作為 base URL。
- **目錄判斷**（`extract_chapter_links`）：把同網域的 `<a>` 依網址格式分組（`url_pattern`：目錄路徑的數字換成 `#`，最後一段只保留副檔名，因為章節 ID 可能是英數混合，如 czbooks 的 `sk2e5`、`sknjc`），取數量最多的一組；整個容器的連結都在其他地方出現過的（「最新章節」區塊）會被丟掉；最後去重時保留「最後一次出現」的位置（`_dedup_keep_last`），讓順序從第 1 章開始。可傳入 `pattern` 只取指定格式的連結（分頁目錄用）。找到的章節少於 `MIN_TOC_LINKS` 時，`process_book` 改用 next 模式。
- **分頁目錄**（`collect_paginated_toc`，在 `--reverse` 之前呼叫）：`find_toc_pages` 從 `<option value>` 和文字像頁碼的連結（`_TOC_PAGE_TEXT`：數字、下一頁、尾頁、第N頁、1-100章…）找出其他目錄頁；網址必須包含書號（目錄路徑中 3 位以上的數字，避免抓到別本書的目錄），沒有書號時要在目錄資料夾底下；排除已知章節網址（不能用 `url_pattern` 排除，因為 `index_2.html` 和章節 `123.html` 格式相同）。其他頁用 `fetch_many_with_verify` 下載，只取和第一頁同格式的章節連結，沒有新章節的頁面不算；會繼續從新頁面找下一頁，最多 `MAX_TOC_PAGES` 頁。合併（`merge_toc_pages`）依網址中的數字排序（`_natural_key`），再保留最後出現的位置去重（每頁都有的「最新章節」會落在最後一頁）。`find_toc_pages` 已在 81txt.net 的真實分頁目錄（下拉選單「第1-100章」…共 10 頁）上確認能找出所有分頁，但該網站的目錄頁沒有章節連結，所以這種「每頁都有章節 + 頁碼」的合併只有合成 HTML 測試。
- **範圍目錄**（`is_toc_index` → `collect_toc_index`，在 `collect_paginated_toc` 開頭判斷）：目錄首頁只列出「第0000--0100章」這種範圍連結、各連到一頁章節清單時，`extract_chapter_links` 會把範圍連結當成章節。80% 以上的名稱符合 `_RANGE_ONLY`（`_RANGE_TEXT`：第N-M章，分隔符號可重複，如 `--`）時，改為下載每一頁、取同格式的章節連結，依首頁順序合併再去重。**已在手機版黃金屋（14 頁、1381 章，和電腦版一致）實際下載驗證。**
- **分卷**（`extract_volumes`，結果放在 `meta["volumes"]`，`{章節網址: 卷名}`，只用在 EPUB 目錄）：依文件順序走過目錄頁，記住最近一個「不在連結裡、2～50 字、符合 `_VOLUME_HEADING`（第X卷/部、卷X、正文卷、作品相關）」的文字，套用到之後的章節連結（同一章出現多次以最後的位置為準，避免「最新章節」區塊影響）。分不到兩卷時，改從章節名稱開頭的「第X卷 卷名 第N章」（`_TITLE_VOLUME`）取出；還是不到兩卷就不分卷。分頁目錄只看第一頁。目前測試過的網站都沒有分卷，只有合成 HTML 測試。
- **封面**（`find_cover_url` → `download_cover`）：`og:image` → alt/title 含書名的 `<img>` → 網址像封面的 `<img>`（cover、bookimg…），排除 nocover/default 等預設圖。先在書籍介紹頁（展開目錄前）找，找不到再看目錄頁。只在 `--format` 含 EPUB 時下載，用 `fetch_binary`（`execute_async_script`，單一小檔，15 秒逾時）；圖片在其他網域時 fetch 會被 CORS 擋，改為先開啟圖片網址再抓，**之後一定要 `load_soup` 回到原頁面**，因為快速模式要在小說網站的頁面上 `fetch()`。以開頭位元組（`image_type`）判斷格式，不是圖片就放棄。存在暫存資料夾，下次不重抓。實測 ixdzs、ttkan（圖片都在其他網域）、69shuba 都能取得封面；hjwzw 目錄頁沒有封面圖。
- **正文擷取**（`extract_content`）：先刪除 `REMOVE_TAGS`；其中 `WRAPPER_TAGS`（form、header、footer、nav、aside）若包著超過 `WRAPPER_KEEP_LEN`（500）字，只拿掉外框、保留內容 —— 手機版黃金屋是 ASP.NET 網站，整頁包在 `<form>` 裡，整個刪掉會讓正文消失。再挑出「直接」包含最多文字的元素（`_direct_text_score`），把 `<br>` / `<p>` 轉成換行，刪除符合 `AD_PATTERNS` 的短行（< 60 字）及純符號行（如「←→」）。除非 `inplace=True`，否則會先複製 soup —— 之後還要找導覽連結的呼叫端（例如 `download_by_next` → `find_link`）依賴這個複製行為。
- **輸出檔名**：`output_base` 決定不含副檔名的路徑：沒給 `-o` 時為 `DOWNLOAD_DIR/書名_網站名`；`-o` 結尾是 `.txt`/`.epub` 時去掉副檔名，其他（例如 `Mr.Wang`）原樣保留；副檔名由 `--format` 決定。書名和網站名都會經過 `to_traditional`（簡體站也輸出繁體檔名）。書名來自 `extract_book_name`：`og:novel:book_name` → `og:title`/`<title>` 中《》內的文字（取 `_`/`|` 前第一段）→ 文字出現在 `<title>` 中的 h1/h2 → 標題第一段，最後由 `_clean_book_name` 去掉【完結+番外】等標記，以及 `_BOOK_NAME_SUFFIX` 字尾（最新章節列表、章節目錄、全文閱讀…，69shuba 的 h1 是「書名最新章节列表」）。作者（`extract_author`，EPUB 用）取 `og:novel:author` / `book:author` / `meta[name=author]`。不要直接用 `og:title`：czbooks 的 og:title 是一整串 SEO 字串。網站名來自 `extract_site_name`：`og:site_name` → `<title>` 以 `-_|` 分割的最後一段，反覆去掉 `_SITE_SUFFIX` 字尾（中文、繁體、免費、小說、閱讀、網…）→ 太長或取不到時用網域主名稱（去掉 www/m/tw/cn）。
- **標題**（`better_title`）：優先用目錄上的名稱；若章節頁標題以它開頭且更長，改用章節頁標題。
- **標題比對一律用 `title_key`**：去掉空白，有 OpenCC 時轉成簡體。69shuba 的目錄頁在瀏覽器中被轉成繁體，但用 `fetch()` 抓的章節原始 HTML 是簡體；網站也常用 `&nbsp;` 或全形空白。`title_key` 自己建立 OpenCC 轉換器（`_KEY_CONVERTER`），因為解析用的子進程不會執行 `setup_converter`。
- **正文開頭的雜訊**：`extract_content` 在保留前 3 行之前，會略過「`title_key` 包含標題、且長度不超過標題 + 30 字」的行（hjwzw 的「書名第1章 xxx」、69shuba 重複兩次的標題），以及符合 `_HEADER_META` 的短行（< 40 字，例如 69shuba 的「2025-12-10 作者： xxx」）。
- **一章多頁**（`find_continuation`）：只有網址是 `<同一 stem>_N` / `-N` 時才跟隨「下一頁」。
- **每章都有的雜訊行**（`find_repeated_lines`，在 `collect_book` 輸出時處理，暫存保留原文）：去掉頭尾空白後完全相同、長度至少 `REPEATED_LINE_MIN_LEN`（4）字、出現在至少 `REPEATED_LINE_RATIO`（50%）章節的行（每章只算一次），章數少於 `REPEATED_LINE_MIN_CHAPTERS`（5）時不處理。用來處理 `AD_PATTERNS` 沒列到的網站宣傳文字和「(本章完)」。實測黃金屋 600 章中正文重複最多的行（「嗯？」）只出現在 7% 的章節，離門檻很遠；69shuba 每章結尾的「(本章完)」是 100%。`--keep-repeated`（`meta["clean"] = False`）可關閉。刪除的行會在輸出時列出前 3 種。
- **簡轉繁**：統一在輸出時（`collect_book` → `to_traditional`）處理，所有下載模式與輸出格式都經過這裡；暫存存的是未轉換的原文。不要在解析階段轉換。流程：
  1. OpenCC `t2s` 先轉成純簡體，再用 `--convert` 指定的設定（預設 `s2tw`）轉回繁體。網站的「機器繁體」常是繁簡混雜（「戰斗」「腦海里」「白發」），直接 s2t 對不到詞庫；先統一成簡體才能用完整詞彙判斷。實測這樣也會順便修正網站原本的錯字（白發→白髮、干凈→乾淨、墻→牆）。
  2. 套用 `FIX_RULES`（正規表示式）補 OpenCC 詞庫處理不到的錯誤（死斗、通道里、几乎、聊天后、一齣…）。新增規則時要確認不會誤改正確用法（歇斯底里、音譯人名「諾里斯」、公里、皇后、一齣好戲），可用 `setup_converter('s2tw')` 後直接呼叫 `to_traditional` 測試。
  3. 引號 “”‘’ → 「」『』（不需 OpenCC）。
  - OpenCC 為選用相依：`setup_converter` 匯入失敗時只提示、不中斷，引號仍會轉換。
  - 輸出中仍會出現的「丑角、人云亦云、伙食、天台、氣喘吁吁、岩石、游泳、倒霉」是正確的台灣用字，不是殘留簡體。

### 下載模式

- **快速模式（預設，`download_fast`）**：不切換頁面，而是讓 Chrome 自己平行 `fetch()` 多個章節網址，沿用瀏覽器的 TLS 憑證信任、Cookie 與 Cloudflare 通行狀態（uukanshu.cc 憑證過期，純 Python HTTP 會連線失敗）。JS 端會先移除 `<script>/<style>` 以減少回傳資料量，並依 Content-Type 或 `<meta charset>` 解碼；每個請求 30 秒逾時。
  - **啟動 + 輪詢**（`fetch_many`）：`execute_script(_FETCH_START_JS)` 啟動一批下載後立刻回傳 job id（結果存在 `window.__ndJobs`），Python 每 `POLL_INTERVAL`（0.3 秒）用 `_POLL_JS` 查詢是否完成。**不要改回 `execute_async_script` 等整批完成**：chromedriver 一次只處理一個指令，長指令會讓 Ctrl+C 後的 `driver.quit()` 排隊等到整批抓完（實測約 10 秒），而且 Python 在等網路回應時收不到 Ctrl+C；也會受 Selenium 120 秒指令逾時限制。輪詢時若 job 不見了（頁面被切換），整批當成失敗。
  - 原始 HTML 交給進程池用頂層函式 `parse_chapter_html` 解析：此函式必須可被 pickle，Windows 上入口檔一定要保留 `if __name__ == "__main__"`。
  - 每批完成時印出進度與預估剩餘時間（`eta_text`：已用時間 ÷ 已完成數 × 剩餘數，少於 3 章不估計）；慢速模式每章都印。
  - 每批最多 `--batch-size`（預設 50）章；有限速時改為約 20 秒的量（`rate × 20`，最少 5 章），慢速網站中斷時才不會損失太多。每批：抓第一頁 → 抓各章的後續分頁（一章多頁）→ 下載失敗或正文太短（< `MIN_CONTENT_LEN`，例如 JS 動態產生內容）的章節退回 `fetch_chapter`（Selenium 開啟頁面）→ 整批存進暫存。
  - `fetch_chapter` 的 `RuntimeError` 只略過該章，但 `BlockedError`（`RuntimeError` 子類別）必須往上拋。
- **慢速模式（`--slow`，`download_by_toc`）**：逐章 `fetch_chapter` → 每章存進暫存。
- **有目錄時（`download_toc_book`）**：
  1. 讀取暫存，略過 `--start`～`--end` 範圍內已下載的章節（`--refresh` 時不略過，新內容會蓋掉舊的）。
  2. 若沒有暫存、`<base>.txt` 已存在且 `--start > 1`（舊版下載的檔案），先把原檔改名為 `<base>.bak.txt`，避免被只含後半段的新檔覆寫。
  3. 交給 `run_and_export` 執行下載。
- **沒有目錄時（`download_next_book` → `download_by_next`）**：跟隨「下一章」連結，網址格式改變（例如回到目錄）時停止。也使用暫存：每章多存 `next`（下一章網址，最後一章為 `None`）。續傳時從起始網址沿著暫存的 `next` 一路走，不重新下載；遇到沒有 `next` 的章節（上次的最後一章）會重新抓，以便發現連載新增的章節。章節依走過的順序加進 `chain`，作為輸出順序。
- **共同收尾（`run_and_export`）**：`KeyboardInterrupt` 和一般 `RuntimeError` 在這裡攔下並提示重新執行即可接續；`BlockedError` 往上交給 `cli.main`。不論是否完成（`finally`），都用 `export_book` 把暫存中**整本**已下載的章節依順序輸出（不只這次的範圍），列出缺少的章節範圍與刪除的雜訊行，記錄網站速度，最後用 `check_quality` 列出可疑章節。回傳 `"done"` / `"interrupted"` / `"error"`，`download_toc_book` / `download_next_book` 原樣回傳。
- **多本書**（`cli.main` → `build_jobs`）：網址可以給多個、用 `-i` 從文字檔讀（`read_url_file`：一行一個、`#` 開頭為註解、容許 BOM），或用 `--update-all [DIR]` 讀 `DIR/.novel_cache/*.book.json`。每本書是一份複製的 `argparse.Namespace`，共用同一個 Chrome。某本書狀態為 `"interrupted"`（使用者按 Ctrl+C）時停止後面的書；`BlockedError` / `RuntimeError`（例如網域不存在）只算該本失敗，繼續下一本；最後列出各本結果。多本書時不能用 `-o`。
- **書籍資訊**（`save_book_info`，`<輸出檔名>.book.json`）：在開始下載前記錄 `BOOK_INFO_KEYS`（url、format、mode、reverse、slow）與輸出路徑（`output`，絕對路徑，讓 `-o` 指定的位置也能更新）。`--update-all` 沿用這些設定、`start=1`、`end=0`（下載所有還沒下載的章節，不沿用上次的範圍），其他參數（速度、`--convert` 等）用這次命令列的值。`--list` 不記錄。舊版下載的書沒有這個檔，要用網址重新執行一次才會被 `--update-all` 找到。
- **防止同時執行**（`acquire_lock`）：在 `chrome_profile/novel_downloader.lock` 上用作業系統的檔案鎖（Windows `msvcrt.locking`、其他 `fcntl.flock`），程式結束或當機時系統會自動解除，不會留下過期的鎖。拿不到鎖就提示並結束。沒有這個鎖時，第二個程式啟動 Chrome 失敗後，`kill_stale_chrome` 會把第一個程式的 Chrome 當成殘留程序關掉。`--profile ""` 時不鎖定。實測同時啟動兩個：第二個立刻被擋，第一個正常完成。
- **輸出（`export_book`）**：`collect_book` 依順序取出章節、刪除雜訊行並簡轉繁（只轉一次，txt 和 EPUB 共用），回傳 (items, 各章網址, 缺少的章節, 刪除的行)，再依 `FORMATS[--format]` 呼叫 `write_txt` / `write_epub`。`meta` 中的 `volumes` 依網址對應成每章的卷名，封面讀取暫存資料夾中的 `cover_path(base)`。`write_book` 是只輸出 txt 的簡便版本（測試用）。
- **EPUB（`write_epub`）**：EPUB 3，附 EPUB 2 的 `toc.ncx`（舊閱讀器也有目錄）。`mimetype` 必須是 zip 中第一個檔案且不壓縮。每章一個 `OEBPS/cNNNNN.xhtml`，每行正文一個 `<p>`。所有文字都要經過 `_xml_text`（跳脫 `& < > "` 並移除 XML 不允許的控制字元），否則閱讀器會打不開。識別碼由來源網址產生 `uuid5`，同一本書重新輸出時不變。
  - 分卷：`_group_volumes` 把連續同卷的章節分組，nav 與 NCX 都變成兩層（卷的連結指向該卷第一章）；不屬於任何卷的章節（卷名為空）放在第一層。NCX 規定同一個目標的 `playOrder` 要相同，所以卷和它的第一章共用 playOrder，navPoint id 用 `v<N>` 區分。
  - 封面：`cover-image` 屬性（EPUB 3）加上 `<meta name="cover">`（EPUB 2 閱讀器），`cover.xhtml` 放在 spine 第一個；圖片不再壓縮。
- **章節暫存（`ChapterCache`）**：JSON Lines，一行一章 `{"url", "title", "content", "page_title"?, "next"?, "retried"?}`，以章節網址為鍵（目錄增加新章節或 `--reverse` 都不影響），同網址以最後一筆為準。`get` 回傳 (標題, 正文)，`meta` 回傳整筆資料。每章寫入後 `flush`；讀取時略過壞掉的行（中斷時寫到一半），續寫前若檔尾不是換行會先補換行。正文為空的章節不存，下次會重抓。`cache_path(base)` 接收不含副檔名的路徑（書名可能含 `.`，不能用 `splitext`）。
- **品質檢查（`check_quality`）**：以字數中位數為基準，標出少於 30%（且至少 `MIN_CONTENT_LEN`）、多於 4 倍（且超過 2 萬字）的章節；去掉空白後前 200 字和前面某章相同的章節；以及目錄名稱和章節頁標題（暫存的 `page_title`）都有章號、但章號不同的章節（`chapter_number` 支援阿拉伯數字、全形數字與中文數字「第一百二十三章」，章/回/節/話）。實測抓到 hjwzw 第 173、520 章的內容其實是第 73 章（網站本身的錯誤）；hjwzw 前 600 章沒有章號誤報。回傳 `[(章節編號, 說明)]`。
- **自動重抓可疑章節**（`retry_suspects`，由 `run_and_export` 在下載**完成**後呼叫；`download_toc_book` 傳入 `retry=fetch`，next 模式沒有）：把 `check_quality` 標出、且暫存中沒有 `retried` 標記的章節，用同一個下載函式（快速或慢速）重抓一次，再重新輸出與檢查；分別列出「重抓後已修正」和「重抓後內容相同」的章節。重抓完成後把這些章節標上 `retried: True`（另寫一筆暫存），之後執行不再自動重抓 —— 否則網站本身的錯誤（hjwzw 第 173 章）每次執行都會被重抓；`--refresh` 重新下載會寫入沒有標記的新紀錄，標記就消失。重抓被中斷時不標記。可疑章節超過 `MAX_RETRY_SUSPECTS`（100）時不重抓。`--no-retry`（`args.no_retry`）關閉。實測 hjwzw 第 170～175 章：第 173 章重抓一次後判定內容相同，第二次執行不再重抓。

### Ctrl+C 與 Chrome 程序

- **chromedriver 放在獨立的程序群組**（`_start_chrome`：Windows 用 `Service(popen_kw={"creation_flags": CREATE_NEW_PROCESS_GROUP})`，其他系統用 `start_new_session`）。否則 Ctrl+C 會連 chromedriver 一起結束，程式就關不掉 Chrome，實測會殘留 11 個 Chrome 程序並佔住設定檔。
- **解析用的進程池（`start_parser_pool`）**：在暫時忽略 Ctrl+C 的狀態下（Windows：`SetConsoleCtrlHandler(None, True)`；其他系統：`SIG_IGN`，兩者都會被子進程繼承）建立，並送出與 worker 數量相同的 `_warmup` 工作，逼它一次啟動所有子進程，之後再恢復。否則子進程還在匯入模組時按 Ctrl+C，`_worker_init` 來不及執行，每個子進程都會印出一大串錯誤堆疊。
- **自動清除殘留的 Chrome**（`build_driver` → `kill_stale_chrome`）：啟動失敗（`SessionNotCreatedException`）且使用的是預設的 `CHROME_PROFILE` 時，關閉命令列含該設定檔路徑的 chrome.exe 後重試一次。子程序的參數是 `--user-data-dir="路徑"`（有引號），主程序沒有，所以比對路徑本身且不分大小寫。使用者用 `--profile` 指定的設定檔不會自動清除（可能是使用者正在用的 Chrome）。
- 實測（hjwzw，每秒 5 頁時按 Ctrl+C）：修正前約 16 秒才結束、殘留 11 個 Chrome；修正後約 3 秒結束、沒有殘留、沒有錯誤訊息，已完成的批次都有輸出。

### 自動調速、Cloudflare 與視窗

- JS 端把回應分成三類：403 或 Cloudflare 驗證頁特徵 → `__CHALLENGE__`；429/503 → `__RATELIMIT__`；其他 → HTML。
- `fetch_many_with_verify` 以「約 10 秒的量」（`rate × 10` 頁，最少 10）為一段逐段下載，並自動調速（參數 `RATE_*` 在 `novel/fetcher.py`）：
  - 限流：等 `RATELIMIT_WAIT` 秒，速度 × `RATE_DOWN`（不限速時改為 `RATE_START`），被擋頁面優先重抓。
  - 整段順利：速度 × `RATE_UP`（上限 `RATE_MAX`），逐步逼近網站上限。
  - 人機驗證，或連續 3 段整段都被限流：降速（不限速 → 每秒 2 頁 → 每次減半，最低 0.2）並降低同時下載數，再呼叫 `wait_for_human`。
  - 之前曾把 503 一律當成人機驗證並永久降到每秒 2 頁，導致 hjwzw 100 章要 21 秒；分開處理後約 2 秒。
- **記住網站速度**：`fetch_many_with_verify` 把每段的速度記到 `state["ok"]`（整段順利）或 `state["limited"]`（被限流/驗證；不限速時被擋記為 `inf`，代表網站有上限）。`learned_rate` 只在這次**曾被擋過**時才回傳值：低於所有被擋速度的最高順利速度。沒被擋過時回傳 `None`，因為順利的速度只是試過的速度，不代表上限（例如用 `--rate 5` 順利跑完，若記下來反而會讓下次變慢）。下載結束時乘上 `RATE_SAFETY`（0.85）留點餘裕，以網域為鍵存進 `site_rates.json`。`process_book` 在 `--rate` 未指定（預設 `None`）時讀取這個值當起始速度；`--rate 0` 表示明確不限速。目錄分頁與章節下載共用同一個 `state`（`args.state`）。
- **Chrome 設定檔**：預設為程式旁的 `chrome_profile/`。無法啟動時 `build_driver` 會先嘗試清除殘留的 Chrome（見上方），仍失敗才改拋 `RuntimeError` 並提示設定檔可能被佔用。
- `load_soup` 遇到 `ERR_NAME_NOT_RESOLVED`（網域不存在）時直接拋 `RuntimeError`，不重試。
- `wait_for_human` 會把平常最小化的視窗叫出來（`WINDOW["minimized"]`），輪詢到驗證頁消失後再縮回去。
- 直接封鎖頁由 `is_blocked_page` 偵測並拋出 `BlockedError`，`cli.main` 會提示原因（例如 headless 被擋）。
- `is_challenge_page`（以及 `_FETCH_START_JS` 中對應的判斷）只比對驗證頁的技術特徵：Cloudflare 的 `challenge-platform`、`cf-chl` 等，以及自訂驗證頁的 `<title>`（ttkan 的「正在验证浏览器」）。不比對內文中的「驗證」等字，避免小說內文誤判；新增其他網站的驗證頁時也只比對 `<title>` 或技術特徵。

## 已知網站行為

測試狀態總覽（2026-11，優先支援繁體網站）：

| 網站 | 語言 | 狀態 |
|---|---|---|
| tw.hjwzw.com（黃金屋） | 繁（機器轉換） | ✅ 正常，自動調速 |
| m.hjwzw.com / t.hjwzw.com（黃金屋手機版） | 繁（機器轉換） | ✅ 正常（書頁 → 目錄頁 → 14 頁範圍目錄） |
| uukanshu.cc（UU看書） | 繁 | ✅ 正常，但約每 30 請求需人機驗證，很慢 |
| czbooks.net（小說狂人） | 繁 | ✅ 正常 |
| ixdzs.tw（愛下電子書） | 繁 | ✅ 正常（需展開完整目錄） |
| 69shuba.com（69書吧） | 簡 | ✅ 正常（書頁自動點「完整目录」） |
| ttkan.co（天天看小說） | 簡 | ✅ 正常（自訂驗證頁會自動通過） |
| 81txt.net（八一中文，xbiquge.so 轉址） | 簡 | ❌ 目錄頁沒有章節連結（網站問題），無法使用 |
| piaotia.com（飄天文學）、hetushu.com（和圖書） | — | ⚠ 首頁即要求人機驗證，未測 |

### 69shuba.com（69書吧）

測試對象：`https://www.69shuba.com/book/90442.htm`（《霍格沃茨的學習面板》，569 章）。

- 書頁 `/book/<id>.htm` 只有推薦書和「完整目录」按鈕，`expand_toc` 會點它進入 `/book/<id>/`。章節網址為 `/txt/<id>/<章節id>`。
- 目錄頁的 h1 是「書名最新章节列表」，由 `_BOOK_NAME_SUFFIX` 去掉。
- 目錄頁在瀏覽器中顯示為繁體（目錄名稱是繁體），但 `fetch()` 抓到的章節原始 HTML 是簡體，所以標題比對要用 `title_key`。
- 正文開頭有兩行標題和一行「日期 作者： xxx」，由 `extract_content` 過濾。20 章約 1.6 秒，未遇到限流。

### ttkan.co（天天看小說）

測試對象：`https://www.ttkan.co/novel/chapters/wanxiangzhiwang-tiancantudou`（《萬相之王》，1838 章；章節網址為 `/novel/pagea/<代號>_<N>.html`）。

- 進站時會出現自訂的「正在验证浏览器」頁（不是 Cloudflare），是自動的瀏覽器檢查，幾秒後自己通過，不需要使用者操作。以 `<title>` 偵測後交給 `wait_for_human` 等待。
- 5 章約 1.4 秒。

### 81txt.net（八一中文）

書頁 `/book/<id>.html` 只列約 25 章，「章节目录」連到分頁目錄 `/book/<id>/1.html`（下拉選單「第1-100章」…，`find_toc_pages` 能正確找出全部分頁）。但目錄頁本身沒有任何章節連結（等待 JS 後也沒有，試了 3 本書都一樣），只有「开始阅读」「新书已出」兩個連結，因此無法使用。

### czbooks.net（小說狂人）

測試對象：`https://czbooks.net/n/sklkm61fg`（《折月亮》，236 章）。章節網址為 `/n/<書ID>/<英數章節ID>?chapterNumber=N`。og:title 是 SEO 字串，書名需從《》取出。每章約 1700 字，5 章 0.7 秒。

- 章名只有「第N頁」是網站本身的設計：網站把整本書按固定長度切頁，章節頁的 `<h1>` 也是「《書名》第N頁」，正文中沒有章名，無法取得真正的章名。

### ixdzs.tw（愛下電子書）

測試對象：`https://ixdzs.tw/read/586712/`（757 章）。書頁只列最新 100 章（新→舊），點「查看完整章節目錄>>>」後以 AJAX（POST `/novel/html`）載入完整目錄，由 `expand_toc` 處理。有 `og:novel:book_name` 與 `og:site_name`。

### tw.hjwzw.com（黃金屋）

測試對象：`https://tw.hjwzw.com/Book/Chapter/47403`（《術師手冊》，1381 章；章節網址為 `/Book/Read/47403,<id>`）。

- 限流方式：一次同時約 20 個請求後回傳 503（沒有 Retry-After，不需人機驗證），幾秒後即恢復。實測穩定每秒 15 頁不會被擋、每秒 25 頁會被擋。
- 自動調速下：100 章約 2 秒；整本 1381 章約 140 秒（中途被限流 3 次）。改為每批存暫存後，300 章約 32 秒；從記錄的每秒 16.6 頁開始，400 章約 34 秒。
- 第 173 章「第一卷總結兼請假」和第 520 章「第二卷總結兼請假」網站上的內容都是第 73 章的重複（網站錯誤，品質檢查會標出）。
- `<title>` 為「術師手冊/聽日/txt下載-黃金屋中文」，網站名稱取為「黃金屋」。
- 內容是從簡體機器轉換的繁體，會殘留簡體字（那么、什么、圣、愿、戰斗、腦海里、白發…）並使用 “” 引號，靠上述簡轉繁流程處理。
- 每章正文開頭會重複一行「書名第N章 標題」，已由重複標題過濾處理。

### m.hjwzw.com / t.hjwzw.com（黃金屋手機版）

測試對象：`https://m.hjwzw.com/Book/Chapter/47403`（轉址到 `t.hjwzw.com`；章節網址為 `/Read/47403_<id>`）。這是目前唯一在真實網站上驗證過的分頁目錄。

- 書頁只列最新 5 章，有「目錄」連結 → `/Chapter/47403`（由 `find_toc_link` 處理）。
- 目錄頁只有 14 個範圍連結「第0000--0100章」…「第1300--1381章」→ `/ChapterList/47403/0_100`，每頁 100 章（由 `collect_toc_index` 處理），合併後 1381 章，頭尾與電腦版一致。
- ASP.NET 網站，整頁包在 `<form id="form1">` 裡，正文在 `#Lab_Contents`（見「正文擷取」的 `WRAPPER_TAGS`）。修正前每章只抓到「挑選暢銷好書」6 個字。
- 3 章約 1.5 秒。

### uukanshu.cc

測試對象：`https://uukanshu.cc/book/17472/`（《術師手冊》，1370 章）。

- 不論是否限速（每秒 1 頁、0.5 頁都測過），大約每 30 個請求就出現 Cloudflare 驗證，每次約需 2 分鐘才放行；實際速度約每秒 0.25 頁（整本約 90 分鐘）。把 Accept 標頭改成瀏覽器開網頁用的標準值沒有幫助。
- headless Chrome 會被直接封鎖（「Sorry, you have been blocked」）；最小化的一般視窗可正常使用。
- 最後兩章番外夾雜其他小說的段落，是網站本身的問題，不是擷取錯誤。
