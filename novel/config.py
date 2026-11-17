"""預設設定與共用常數（可用命令列參數覆蓋的預設值、啟發式規則的門檻）。"""

import os
import re


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 專案根目錄


# 專用的 Chrome 設定檔（第一次執行時自動建立），保存 Cookie 與 Cloudflare 驗證狀態
CHROME_PROFILE = os.path.join(BASE_DIR, "chrome_profile")

DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")  # 預設輸出資料夾

CACHE_DIRNAME = ".novel_cache"  # 已下載章節的暫存資料夾（建在輸出檔旁邊）

MAX_TOC_PAGES = 200      # 分頁目錄最多抓幾頁

BOOK_INFO_EXT = ".book.json"  # 暫存資料夾中記錄每本書下載設定的檔案（--update-all 用）

LOCK_NAME = "novel_downloader.lock"  # 放在 Chrome 設定檔中，避免同時執行兩次


# 每章都有的雜訊行（網站宣傳、「(本章完)」等）：出現在至少這個比例的章節、且至少這麼多章時自動刪除
REPEATED_LINE_RATIO = 0.5

REPEATED_LINE_MIN_CHAPTERS = 5

REPEATED_LINE_MIN_LEN = 4  # 太短的行（「嗯。」）可能是正常對話，不處理

WAIT_SECONDS = 10        # 每頁等待正文出現的最長秒數

MAX_RETRIES = 3          # 單頁失敗重試次數

MIN_CONTENT_LEN = 50     # 正文少於此字數視為尚未載入完成

MAX_RETRY_SUSPECTS = 100  # 下載完自動重抓的可疑章節最多幾章（太多通常是規則問題，重抓也沒用）
MIN_TOC_LINKS = 5        # 目錄至少要有幾個章節連結才算數


# 廣告/雜訊行：只有在「短行」（< 60 字）且符合時才刪除，避免誤刪正文段落
AD_PATTERNS = re.compile(
    r"https?://|www\.|\.(com|net|org|cc|cn|tw|la|info)\b"
    r"|本章未完|點擊下一頁|点击下一页|繼續閱讀|继续阅读"
    r"|請收藏|请收藏|加入書籤|加入书签|最新章節|最新章节"
    r"|手機閱讀|手机阅读|首發|首发|記住本站|记住本站|天才一秒",
    re.IGNORECASE,
)

NEXT_CHAPTER_TEXT = re.compile(r"下一章|下一頁|下一页|下页|下頁|next", re.IGNORECASE)

CONTINUE_PAGE_TEXT = re.compile(r"下一頁|下一页|下页|下頁")

REMOVE_TAGS = ["script", "style", "noscript", "iframe", "header", "footer", "nav",
               "aside", "form", "select", "button", "input", "svg"]


# 這些「外框」標籤包著大量文字時只拿掉外框、保留內容：ASP.NET 網站（如手機版黃金屋）整頁都包在 <form> 裡
WRAPPER_TAGS = {"form", "header", "footer", "nav", "aside"}

WRAPPER_KEEP_LEN = 500
