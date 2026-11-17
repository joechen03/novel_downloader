"""通用小說下載器：輸入目錄頁網址，自動找出所有章節並下載成 txt / EPUB。

程式本體在 novel/ 套件（各模組說明見 AGENTS.md），這個檔案只是執行入口。

不針對特定網站寫死 XPath，而是用啟發式規則判斷：
  * 目錄頁：找出「網址格式相同、數量最多」的一組連結，視為章節列表
            （會自動排除「最新章節」這類重複區塊）。
  * 章節頁：找出「直接包含最多文字」的區塊，視為正文；並過濾常見廣告行。
  * 章節被拆成多頁（123.html → 123_2.html）時會自動接續。
  * 若給的是章節網址而非目錄，會改用「下一章」連結一路往下抓。

用法範例：
    python novel_downloader.py https://uukanshu.cc/book/17472/
    python novel_downloader.py https://uukanshu.cc/book/17472/ --list          # 只列出章節，不下載
    python novel_downloader.py https://uukanshu.cc/book/17472/ --end 100       # 只下載前 100 章
    python novel_downloader.py https://uukanshu.cc/book/17472/ --format epub   # 輸出 EPUB
    （輸出到 downloads/；中斷後重新執行同一指令即可接續，已下載的章節會自動略過）
    python novel_downloader.py https://example.com/book/1/1.html --mode next   # 從某章一路抓「下一章」
"""

from novel.cli import main

# Windows 上解析用的子進程會重新匯入這個檔案，一定要保留這個判斷
if __name__ == "__main__":
    main()
