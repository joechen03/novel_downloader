"""通用小說下載器：輸入目錄頁網址，自動找出所有章節並下載成 txt。

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
    （中斷後重新執行同一指令即可接續：已下載的章節存在 .novel_cache/，會自動略過）
    python novel_downloader.py https://example.com/book/1/1.html --mode next   # 從某章一路抓「下一章」
"""

import argparse
import json
import os
import re
import statistics
import time
from collections import OrderedDict, defaultdict
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString
from selenium import webdriver
from selenium.common.exceptions import (SessionNotCreatedException, UnexpectedAlertPresentException,
                                        WebDriverException)

# ===== 預設設定（可用命令列參數覆蓋）=====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 專用的 Chrome 設定檔（第一次執行時自動建立），保存 Cookie 與 Cloudflare 驗證狀態
CHROME_PROFILE = os.path.join(BASE_DIR, "chrome_profile")
SITE_RATES_FILE = os.path.join(BASE_DIR, "site_rates.json")  # 各網站學到的速度上限
CACHE_DIRNAME = ".novel_cache"  # 已下載章節的暫存資料夾（建在輸出檔旁邊）
MAX_TOC_PAGES = 200      # 分頁目錄最多抓幾頁

WAIT_SECONDS = 10        # 每頁等待正文出現的最長秒數
MAX_RETRIES = 3          # 單頁失敗重試次數
MIN_CONTENT_LEN = 50     # 正文少於此字數視為尚未載入完成
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


# ---------------------------------------------------------------------------
# 瀏覽器
# ---------------------------------------------------------------------------
def build_driver(profile_dir: str, headless: bool) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    if profile_dir:
        options.add_argument(f"--user-data-dir={profile_dir}")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.page_load_strategy = "eager"  # DOM 就緒即可，不等圖片/廣告
    try:
        return webdriver.Chrome(options=options)
    except SessionNotCreatedException as e:
        hint = (f"\n  設定檔「{profile_dir}」可能正被另一個 Chrome 視窗使用，請關閉後再試，"
                "或用 --profile 指定其他資料夾。") if profile_dir else ""
        raise RuntimeError(f"無法啟動 Chrome：{e.msg}{hint}") from e


def load_soup(driver, url: str, ready=None) -> BeautifulSoup:
    """載入網址並回傳 soup；ready(soup) 為 True 才算載入完成（支援 JS 動態內容）。失敗自動重試。"""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            driver.get(url)
            if is_blocked_page(driver.page_source) or is_challenge_page(driver.page_source):
                wait_for_human(driver, url)
            deadline = time.time() + WAIT_SECONDS
            while True:
                soup = BeautifulSoup(driver.page_source, "lxml")
                if ready is None or ready(soup) or time.time() > deadline:
                    return soup
                time.sleep(0.5)
        except UnexpectedAlertPresentException:
            try:
                driver.switch_to.alert.dismiss()
            except WebDriverException:
                pass
            last_error = "網站跳出警告視窗"
        except WebDriverException as e:
            last_error = e.msg
        print(f"  第 {attempt} 次載入失敗（{last_error}），{attempt * 2} 秒後重試…")
        time.sleep(attempt * 2)
    raise RuntimeError(f"載入失敗：{url}（{last_error}）")


# ---------------------------------------------------------------------------
# 目錄頁解析
# ---------------------------------------------------------------------------
def url_pattern(url: str) -> str:
    """網址「格式」：目錄部分的數字換成 #，最後一段只保留副檔名。
    例：/book/17472/10322423.html → /book/#/*.html；/n/abc1/sk2e5 → /n/abc#/*
    （最後一段常是英數混合的章節 ID，如 czbooks 的 sk2e5、sknjc，不能逐字比對）"""
    path = urlparse(url).path
    head, _, last = path.rpartition("/")
    head = re.sub(r"\d+", "#", head)
    if not last:
        return head + "/"
    ext = last.rsplit(".", 1)[1] if "." in last else ""
    return f"{head}/*" + (f".{ext}" if ext else "")


def _clean_book_name(name: str) -> str:
    name = re.sub(r"【[^】]*】|\[[^\]]*\]", "", name)  # 【完結+番外】等標記
    return name.strip(" 　_-|｜")


def extract_book_name(soup: BeautifulSoup) -> str:
    """書名：og:novel:book_name → 標題中的《》→ 出現在 <title> 中的 h1/h2 → 標題第一段。"""
    def meta(prop):
        m = soup.find("meta", attrs={"property": prop})
        return m.get("content", "").strip() if m else ""

    if meta("og:novel:book_name"):
        return _clean_book_name(meta("og:novel:book_name"))
    title = soup.title.get_text(strip=True) if soup.title else ""
    for text in (meta("og:title"), title):
        m = re.search(r"《([^》]+)》", text)
        if m:  # 《折月亮_竹已【完結+番外】》→ 折月亮
            return _clean_book_name(re.split(r"[_|｜]", _clean_book_name(m.group(1)))[0])
    for h in soup.find_all(["h1", "h2"]):
        text = h.get_text(strip=True)
        if text and title and text in title:
            return _clean_book_name(text)
    first = re.split(r"[_\-|｜,，/]", meta("og:title") or title or "novel")[0]
    return _clean_book_name(first) or "novel"


_EXPAND_TOC_TEXT = re.compile(r"查看完整|完整目錄|完整目录|全部章節|全部章节|查看全部|展開目錄|展开目录|章節目錄>>|更多章節|更多章节")


def expand_toc(driver) -> bool:
    """若頁面有「查看完整章節目錄」之類的按鈕（點了才用 AJAX 載入完整目錄），點它並等目錄載入。"""
    count_js = "return document.querySelectorAll('a[href]').length"
    before = driver.execute_script(count_js)
    url_before = driver.current_url
    clicked = driver.execute_script("""
        const re = new RegExp(arguments[0]);
        for (const el of document.querySelectorAll('a, button, span, div')) {
            const t = (el.innerText || '').trim();
            if (t.length > 0 && t.length < 30 && re.test(t) && el.children.length <= 3) { el.click(); return t; }
        }
        return null;""", _EXPAND_TOC_TEXT.pattern)
    if not clicked:
        return False
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(0.5)
        # 按鈕是連到另一個完整目錄頁（只有 #錨點 改變不算）
        if driver.current_url.split("#")[0] != url_before.split("#")[0]:
            time.sleep(1)
            print(f"已開啟完整目錄頁（點擊「{clicked}」）：{driver.current_url}")
            return True
        now = driver.execute_script(count_js)
        if now > before:
            time.sleep(1)  # 等最後一批渲染完
            if driver.execute_script(count_js) == now:
                print(f"已展開完整目錄（點擊「{clicked}」，連結 {before} → {now}）")
                return True
    return False


_SITE_SUFFIX = re.compile(r"(繁體|簡體|简体|免費|免费|小說|小说|閱讀|阅读|線上|在线|全文|中文|官方|首頁|網|网|站)$")


def extract_site_name(soup: BeautifulSoup, url: str) -> str:
    """取得網站名稱：og:site_name → <title> 最後一段（去掉「中文網」等字尾）→ 網域名稱。"""
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    name = meta.get("content", "").strip() if meta else ""
    if not name and soup.title:
        parts = [p.strip() for p in re.split(r"[-_|｜–—]", soup.title.get_text(strip=True)) if p.strip()]
        name = parts[-1] if len(parts) > 1 else ""
        while _SITE_SUFFIX.search(name) and len(name) > 2:
            name = _SITE_SUFFIX.sub("", name)
    if not name or len(name) > 12:
        host = urlparse(url).netloc.split(":")[0]
        labels = [x for x in host.split(".") if x not in ("www", "m", "tw", "cn")]
        name = labels[0] if labels else host
    return name


def extract_chapter_links(soup: BeautifulSoup, base_url: str, pattern: str | None = None) -> list[tuple[str, str]]:
    """回傳 [(章節名稱, 網址), ...]，依目錄順序排列。
    pattern 指定章節網址格式（url_pattern）時只取該格式，否則取數量最多的一組。"""
    host = urlparse(base_url).netloc
    toc_path = urlparse(base_url).path
    by_pattern = defaultdict(list)  # pattern -> [(container, url, text)]

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        text = a.get_text(strip=True)
        if not text or urlparse(url).netloc != host or urlparse(url).path == toc_path:
            continue
        container = a.find_parent(["ul", "ol", "dl", "table", "div", "section"])
        by_pattern[url_pattern(url)].append((id(container), url, text))

    if pattern is not None:
        links = by_pattern.get(pattern, [])
    elif by_pattern:
        links = max(by_pattern.values(), key=len)
    else:
        links = []
    if not links:
        return []

    # 依容器分組；若某容器的連結全部在其他容器出現過（例如「最新章節」區塊），整塊丟掉
    containers = OrderedDict()
    for cid, url, text in links:
        containers.setdefault(cid, []).append((url, text))
    if len(containers) > 1:
        for cid in list(containers):
            others = {u for k, v in containers.items() if k != cid for u, _ in v}
            if all(u in others for u, _ in containers[cid]):
                del containers[cid]

    # 同一容器內前面若有重複（最新章節與正文在同一個 <dl>），保留最後出現的位置
    flat = [item for group in containers.values() for item in group]
    return _dedup_keep_last([(text, url) for url, text in flat])


def _dedup_keep_last(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(名稱, 網址) 依網址去重，保留最後一次出現的位置。"""
    last_index = {url: i for i, (_, url) in enumerate(items)}
    return [item for i, item in enumerate(items) if last_index[item[1]] == i]


# 分頁目錄的換頁連結文字：「下一頁」「尾頁」「2」「第2頁」「1-100章」…
_TOC_PAGE_TEXT = re.compile(r"^(上一?[頁页]|下一?[頁页]|尾[頁页]|末[頁页]|\d{1,4}|第\s*\d+\s*[頁页]"
                            r"|第?\s*\d+\s*[-~－～—至]\s*\d+.{0,6})$")


def _norm_url(url: str) -> str:
    return url.split("#")[0].rstrip("/")


def find_toc_pages(soup: BeautifulSoup, toc_url: str, chapter_urls: set[str]) -> list[str]:
    """找出分頁目錄的其他頁網址：<select> 的選項，或文字像頁碼／「下一頁」的連結。
    網址必須包含書號（目錄網址中的長數字）或在目錄資料夾底下，且不能是已知的章節網址
    （分頁網址常和章節網址同格式，例如 index_2.html 與 123.html，所以不能用 url_pattern 排除）。"""
    host = urlparse(toc_url).netloc
    toc_path = urlparse(toc_url).path
    toc_dir = toc_path if toc_path.endswith("/") else toc_path.rpartition("/")[0] + "/"
    book_ids = re.findall(r"\d{3,}", toc_path)

    candidates = [o.get("value", "") for o in soup.find_all("option")]
    candidates += [a["href"] for a in soup.find_all("a", href=True)
                   if _TOC_PAGE_TEXT.match(a.get_text(strip=True))]
    pages = []
    for href in candidates:
        href = href.strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        url = urljoin(toc_url, href).split("#")[0]
        parsed = urlparse(url)
        if parsed.netloc != host or _norm_url(url) == _norm_url(toc_url) or url in chapter_urls:
            continue
        where = parsed.path + "?" + parsed.query
        # 有書號時一定要包含書號（避免抓到別本書的目錄）；沒有書號時要在目錄資料夾底下
        same_book = (any(b in where for b in book_ids) if book_ids
                     else toc_dir != "/" and parsed.path.startswith(toc_dir))
        if same_book:
            if url not in pages:
                pages.append(url)
    return pages


def _natural_key(url: str) -> tuple:
    p = urlparse(url)
    return tuple(int(n) for n in re.findall(r"\d+", p.path + "?" + p.query))


def merge_toc_pages(pages: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    """把各分頁目錄的章節依頁碼順序合併；重複的章節（例如每頁都有的「最新章節」）保留最後的位置。"""
    merged = []
    for url in sorted(pages, key=_natural_key):
        merged += pages[url]
    return _dedup_keep_last(merged)


def collect_paginated_toc(driver, soup, toc_url, chapters, state) -> list[tuple[str, str]]:
    """目錄被拆成多頁時，抓回其他頁並合併。沒有分頁時原樣回傳。"""
    pattern = url_pattern(chapters[0][1])
    pages = {toc_url: chapters}
    seen = {_norm_url(toc_url)}
    known = {u for _, u in chapters}
    queue = find_toc_pages(soup, toc_url, known)
    while queue and len(seen) < MAX_TOC_PAGES:
        batch = [u for u in queue if _norm_url(u) not in seen][:MAX_TOC_PAGES - len(seen)]
        queue = []
        seen |= {_norm_url(u) for u in batch}
        for url, html in zip(batch, fetch_many_with_verify(driver, batch, state)):
            if html is None:
                print(f"  ⚠ 目錄分頁下載失敗：{url}")
                continue
            page = BeautifulSoup(html, "lxml")
            found = extract_chapter_links(page, url, pattern)
            if any(u not in known for _, u in found):  # 沒有新章節的頁面（例如誤抓到章節頁）不算
                pages[url] = found
                known |= {u for _, u in found}
                queue += [u for u in find_toc_pages(page, url, known) if _norm_url(u) not in seen]
    if len(pages) == 1:
        return chapters
    merged = merge_toc_pages(pages)
    print(f"目錄共 {len(pages)} 頁，合併後 {len(merged)} 章")
    return merged


# ---------------------------------------------------------------------------
# 章節頁解析
# ---------------------------------------------------------------------------
def _direct_text_score(el) -> int:
    """計算元素「直接」包含的文字量（不含深層巢狀區塊與連結文字）。"""
    score = 0
    for child in el.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            score += len(child.strip())
        elif child.name in ("p", "span", "font", "strong", "em", "b", "i"):
            score += len(child.get_text(strip=True))
        elif child.name == "a":
            score -= len(child.get_text(strip=True))
    return score


def extract_content(soup: BeautifulSoup, title: str = "", inplace: bool = False) -> str:
    if not inplace:
        soup = BeautifulSoup(str(soup), "lxml")  # 複製一份，避免動到原本的 soup
    for tag in soup(REMOVE_TAGS):
        tag.decompose()

    candidates = soup.find_all(["div", "article", "section", "td", "main", "pre"])
    if not candidates:
        return ""
    best = max(candidates, key=_direct_text_score)
    if _direct_text_score(best) <= 0:
        return ""

    for br in best.find_all("br"):
        br.replace_with("\n")
    for p in best.find_all(["p", "div"]):
        p.insert_after("\n")

    lines = []
    for line in best.get_text().splitlines():
        line = line.strip().replace("\xa0", "").replace("\u3000", "").strip()
        if not line or line == title or re.fullmatch(r"[←→↑↓\-—=*·.。…\s]+", line):
            continue
        if len(line) < 60 and AD_PATTERNS.search(line):
            continue
        # 正文開頭重複的標題（例如「書名第1章 xxx」）
        if title and len(lines) < 3 and title in line and len(line) <= len(title) + 30:
            continue
        lines.append(line)
    return "\n".join(lines)


def extract_title(soup: BeautifulSoup) -> str:
    for tag in ("h1", "h2", "h3"):
        el = soup.find(tag)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return soup.title.get_text(strip=True) if soup.title else ""


def find_link(soup: BeautifulSoup, base_url: str, text_re: re.Pattern) -> str | None:
    for a in soup.find_all("a", href=True):
        if text_re.search(a.get_text(strip=True)):
            href = a["href"].strip()
            if href and not href.startswith(("javascript:", "#")):
                return urljoin(base_url, href).split("#")[0]
    return None


def find_continuation(soup: BeautifulSoup, url: str) -> str | None:
    """同一章被拆成多頁時（123.html → 123_2.html），回傳下一分頁網址。"""
    nxt = find_link(soup, url, CONTINUE_PAGE_TEXT)
    if not nxt:
        return None
    stem = re.sub(r"[_-]\d+$", "", urlparse(url).path.rsplit(".", 1)[0])
    nxt_path = urlparse(nxt).path
    return nxt if re.match(re.escape(stem) + r"[_-]\d+", nxt_path) else None


def fetch_chapter(driver, url: str, title_hint: str = "") -> tuple[str, str, BeautifulSoup]:
    """抓取一章（含多分頁），回傳 (標題, 正文, 最後一頁的 soup)。"""
    def ready(s):
        return len(extract_content(s)) >= MIN_CONTENT_LEN

    soup = load_soup(driver, url, ready)
    page_title = extract_title(soup)
    title = title_hint or page_title
    # 目錄名稱不完整時（如「第3章」），若頁面標題以它開頭且更長，改用頁面標題
    if title_hint and page_title.startswith(title_hint) and len(page_title) > len(title_hint):
        title = page_title
    parts = [extract_content(soup, title)]

    seen = {url}
    page_url = find_continuation(soup, url)
    while page_url and page_url not in seen:
        seen.add(page_url)
        soup = load_soup(driver, page_url, ready)
        parts.append(extract_content(soup, title))
        page_url = find_continuation(soup, page_url)

    return title, "\n".join(p for p in parts if p), soup


# ---------------------------------------------------------------------------
# 下載流程
# ---------------------------------------------------------------------------
def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip() or "novel"


# ---------------------------------------------------------------------------
# 簡轉繁（OpenCC，以詞為單位轉換；已是繁體的字不會被改動）
# ---------------------------------------------------------------------------
CONVERTER = {"enabled": False, "t2s": None, "s2t": None}
QUOTE_MAP = str.maketrans({"“": "「", "”": "」", "‘": "『", "’": "』"})
_NUM = "0-9０-９一二三四五六七八九十百千萬幾數"
# OpenCC 詞庫處理不到的常見錯誤（正規表示式, 取代）；遇到新的可以加在這裡
FIX_RULES = [
    (re.compile(r"(?<=[死戰打爭奮搏決械內])斗"), "鬥"),
    (re.compile(r"(?<=[眼神心夢這那哪屋房家口手])里(?![程長])"), "裡"),
    # 「XX里」表示位置（通道里的、工資里扣除）；排除公里、鄰里、歇斯底里與音譯（諾里斯）
    (re.compile(r"(?<![公英鄰故千萬鄉底華\d一二三四五六七八九十百])里"
                r"(?=[的，。、！？；：」』…—\s面頭邊居也都就還又才卻扣])"), "裡"),
    (re.compile(r"几乎"), "幾乎"),
    (re.compile(r"余裕"), "餘裕"),
    (re.compile(r"(?<![皇王太母])后(?=[，,便就再才])"), "後"),
    (re.compile(rf"(?<![{_NUM}])海里"), "海裡"),
    (re.compile(r"一齣(?![戲劇好])"), "一出"),
]


def setup_converter(config: str) -> None:
    """config 為 OpenCC 設定名稱（如 s2tw），'none' 表示不轉換。"""
    if config == "none":
        return
    CONVERTER["enabled"] = True  # 引號轉換不需要 OpenCC
    try:
        import opencc
    except ImportError:
        print("⚠ 未安裝 OpenCC，略過簡轉繁。請執行：pip install opencc")
        return
    # 先轉成純簡體再轉回繁體：網站的「機器繁體」常是繁簡混雜（如「戰斗」「腦海里」），
    # 直接 s2t 對不到詞庫；先統一成簡體，OpenCC 才能用完整詞彙判斷正確的繁體字。
    CONVERTER["t2s"] = opencc.OpenCC("t2s")
    CONVERTER["s2t"] = opencc.OpenCC(config)


def to_traditional(text: str) -> str:
    if not CONVERTER["enabled"]:
        return text
    if CONVERTER["s2t"] is not None:
        text = CONVERTER["s2t"].convert(CONVERTER["t2s"].convert(text))
        for pattern, repl in FIX_RULES:
            text = pattern.sub(repl, text)
    return text.translate(QUOTE_MAP)


def write_chapter(f, title: str, content: str) -> None:
    title, content = to_traditional(title), to_traditional(content)
    f.write(f"{title}\n\n{content}\n\n")
    f.flush()  # 每章即時寫入，中斷也不會遺失已下載內容


# ---------------------------------------------------------------------------
# 章節暫存：每下載完一章就存起來，中斷後重新執行會自動略過已下載的章節
# ---------------------------------------------------------------------------
def cache_path(output: str) -> str:
    folder = os.path.dirname(os.path.abspath(output))
    stem = os.path.splitext(os.path.basename(output))[0]
    return os.path.join(folder, CACHE_DIRNAME, stem + ".jsonl")


class ChapterCache:
    """JSON Lines 格式，一行一章 {"url", "title", "content"}，以章節網址為鍵（同網址以最後一筆為準）。
    存的是未經簡轉繁的原始內容，組合成 txt 時才轉換。"""

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, tuple[str, str]] = {}
        self._f = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        self.data[rec["url"]] = (rec["title"], rec["content"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue  # 中斷時寫到一半的最後一行

    def __contains__(self, url: str) -> bool:
        return url in self.data

    def __len__(self) -> int:
        return len(self.data)

    def get(self, url: str) -> tuple[str, str]:
        return self.data[url]

    def put(self, url: str, title: str, content: str) -> None:
        self.data[url] = (title, content)
        if self._f is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            broken_tail = False
            if os.path.exists(self.path) and os.path.getsize(self.path):
                with open(self.path, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    broken_tail = f.read(1) != b"\n"
            self._f = open(self.path, "a", encoding="utf-8")
            if broken_tail:  # 上次中斷在一行中間，先換行，避免新資料黏在壞掉的那行後面
                self._f.write("\n")
        self._f.write(json.dumps({"url": url, "title": title, "content": content}, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None


def write_book(output: str, chapters, cache: ChapterCache) -> tuple[int, int, list[int]]:
    """依目錄順序，把暫存中所有已下載的章節組合成 txt（整本，不只這次下載的範圍）。
    回傳 (寫入章數, 字數, 缺少的章節編號[從 1 起算])。"""
    written, chars, missing = 0, 0, []
    with open(output, "w", encoding="utf-8") as f:
        for n, (_, url) in enumerate(chapters, 1):
            if url not in cache:
                missing.append(n)
                continue
            title, content = cache.get(url)
            write_chapter(f, title, content)
            written += 1
            chars += len(content)
    return written, chars, missing


def _ranges(nums: list[int]) -> str:
    """[1,2,3,7,9,10] → '1-3, 7, 9-10'"""
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def check_quality(chapters, cache: ChapterCache) -> list[str]:
    """檢查已下載章節中可疑的地方：字數過少/過多、和其他章內容重複。回傳警告訊息。"""
    items = [(n, *cache.get(url)) for n, (_, url) in enumerate(chapters, 1) if url in cache]
    if len(items) < 3:
        return []
    median = statistics.median(len(c) for _, _, c in items)
    warnings = []
    seen: dict[str, int] = {}
    for n, title, content in items:
        size = len(content)
        if size < max(MIN_CONTENT_LEN, median * 0.3):
            warnings.append(f"第 {n} 章「{title}」只有 {size} 字（一般約 {median:.0f} 字）")
        elif size > max(median * 4, 20000):
            warnings.append(f"第 {n} 章「{title}」有 {size} 字，遠多於一般的 {median:.0f} 字，可能混入其他內容")
        key = re.sub(r"\s+", "", content)[:200]
        if len(key) >= 50:
            if key in seen:
                warnings.append(f"第 {n} 章「{title}」開頭與第 {seen[key]} 章相同，可能重複或抓錯頁面")
            else:
                seen[key] = n
    return warnings


def download_by_toc(driver, chapters, indices, cache: ChapterCache, delay) -> None:
    """慢速模式：用瀏覽器逐章開啟（適用於需要執行 JS 才有內容的網站）。"""
    total = len(chapters)
    for i in indices:
        title, url = chapters[i]
        title, content, _ = fetch_chapter(driver, url, title)
        if content:
            cache.put(url, title, content)
        else:
            print(f"  ⚠ 第 {i + 1} 章未擷取到正文：{url}")
        print(f"[{i + 1}/{total}] {title}（{len(content)} 字）")
        time.sleep(delay)


# ---------------------------------------------------------------------------
# 快速模式：在瀏覽器內用 fetch() 平行下載原始 HTML，再用多進程解析
# （沿用瀏覽器的連線、憑證與 Cookie，所以 Cloudflare / 憑證問題都跟瀏覽器一樣能過）
# ---------------------------------------------------------------------------
_FETCH_JS = r"""
const urls = arguments[0], limit = arguments[1], rate = arguments[2], done = arguments[arguments.length - 1];
const out = new Array(urls.length).fill(null);
let next = 0;
// 限速：rate > 0 時，每個請求的「開始時間」至少間隔 1000/rate 毫秒
const gap = rate > 0 ? 1000 / rate : 0;
let nextStart = performance.now();
async function throttle() {
  if (!gap) return;
  const now = performance.now();
  const wait = nextStart - now;
  nextStart = Math.max(now, nextStart) + gap;
  if (wait > 0) await new Promise(res => setTimeout(res, wait));
}
function decode(buf, ctype) {
  let m = (ctype || '').match(/charset=["']?([\w-]+)/i);
  if (!m) m = new TextDecoder('latin1').decode(buf.slice(0, 4096)).match(/charset=["']?([\w-]+)/i);
  try { return new TextDecoder(m ? m[1] : 'utf-8').decode(buf); }
  catch (e) { return new TextDecoder('utf-8').decode(buf); }
}
async function getOne(u) {
  for (let t = 0; t < 3; t++) {
    try {
      await throttle();
      const r = await fetch(u, {credentials: 'include', headers: {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}});
      const html = decode(await r.arrayBuffer(), r.headers.get('content-type'));
      // 人機驗證 / 封鎖 → 交給 Python 請使用者驗證；限流（429/503）→ 交給 Python 降速重試
      if (r.status === 403 || /challenge-platform|cf-chl|cf_chl/.test(html)) return '__CHALLENGE__';
      if (r.status === 429 || r.status === 503) return '__RATELIMIT__';
      if (!r.ok) throw new Error(r.status);
      // 先砍掉 script/style，減少回傳給 Python 的資料量
      return html.replace(/<(script|style|noscript)\b[\s\S]*?<\/\1>/gi, '');
    } catch (e) { await new Promise(res => setTimeout(res, 500 * (t + 1))); }
  }
  return null;
}
async function worker() { while (next < urls.length) { const i = next++; out[i] = await getOne(urls[i]); } }
Promise.all(Array.from({length: limit}, worker)).then(() => done(out));
"""


CHALLENGE = "__CHALLENGE__"
RATELIMIT = "__RATELIMIT__"

# 自動調速參數（被限流時）
RATE_START = 10.0      # 第一次被限流時的起始速度（頁/秒）
RATE_MIN = 0.5
RATE_MAX = 50.0
RATE_DOWN = 0.6        # 被限流時乘上此值
RATE_UP = 1.25         # 一段時間沒被擋時乘上此值
RATELIMIT_WAIT = 3     # 被限流後等待秒數


def is_challenge_page(html: str) -> bool:
    # 只看 Cloudflare 的技術特徵，避免小說內文剛好出現「驗證」等字眼而誤判
    return bool(re.search(r"challenge-platform|cf-chl|cf_chl|<title>Just a moment", html))


class BlockedError(RuntimeError):
    """網站直接封鎖（不是可以手動通過的驗證）。"""


def is_blocked_page(html: str) -> bool:
    return bool(re.search(r"cf-error-details|Sorry, you have been blocked"
                          r"|<title>Attention Required! \| Cloudflare", html))


# 瀏覽器視窗平常是否縮到工作列（需要驗證時才跳出來）
WINDOW = {"minimized": False}


def wait_for_human(driver, url: str, timeout: int = 600) -> None:
    """遇到人機驗證時，在瀏覽器開啟頁面，等使用者手動完成驗證。"""
    driver.get(url)
    if is_blocked_page(driver.page_source):
        raise BlockedError("網站封鎖了這個瀏覽器（Sorry, you have been blocked）")
    if not is_challenge_page(driver.page_source):
        return
    if WINDOW["minimized"]:
        driver.maximize_window()  # 把視窗叫出來讓使用者驗證
    print("\n  ⚠ 網站要求人機驗證，請在瀏覽器視窗中完成驗證，完成後會自動繼續…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        if not is_challenge_page(driver.page_source):
            print("  ✓ 驗證完成，繼續下載。")
            if WINDOW["minimized"]:
                driver.minimize_window()
            return
    raise RuntimeError("等待人機驗證逾時")


def fetch_many(driver, urls: list[str], concurrency: int, rate: float = 0) -> list[str | None]:
    if not urls:
        return []
    # Selenium 單一指令有 120 秒逾時：限速時把網址切成每段約 60 秒能抓完的量
    chunk = max(1, int(rate * 60)) if rate > 0 else len(urls)
    results: list[str | None] = []
    for k in range(0, len(urls), chunk):
        results += driver.execute_async_script(_FETCH_JS, urls[k:k + chunk], concurrency, rate)
    return results


def fetch_many_with_verify(driver, urls: list[str], state: dict) -> list[str | None]:
    """平行下載並自動調速。state['c'] 為同時下載數，state['rate'] 為每秒請求數（0 = 不限）。

    * 被限流（429/503）：等幾秒、速度 × RATE_DOWN，重抓被擋的頁面。
    * 一段（約 10 秒的量）都沒被擋：速度 × RATE_UP，逐步逼近網站上限。
    * 人機驗證（403 / Cloudflare）：大幅降速並請使用者手動驗證。
    """
    results: list[str | None] = [None] * len(urls)
    todo = list(range(len(urls)))
    strikes = 0  # 連續整段都被限流的次數
    while todo:
        rate = state["rate"]
        size = len(todo) if rate <= 0 else max(10, int(rate * 10))
        chunk, todo = todo[:size], todo[size:]
        res = fetch_many(driver, [urls[k] for k in chunk], state["c"], rate)

        challenged = [k for k, h in zip(chunk, res) if h == CHALLENGE]
        limited = [k for k, h in zip(chunk, res) if h == RATELIMIT]
        for k, h in zip(chunk, res):
            if h not in (CHALLENGE, RATELIMIT):
                results[k] = h
        todo = challenged + limited + todo  # 被擋的頁面優先重抓

        if (challenged or limited) and rate > 0:
            state.setdefault("limited", []).append(rate)
        elif not challenged and not limited and rate > 0:
            state.setdefault("ok", []).append(rate)

        if challenged or strikes >= 3:
            # 不限速時先改成每秒 2 個，之後每次被擋再減半
            state["rate"] = 2.0 if rate <= 0 else max(0.2, rate / 2)
            state["c"] = max(2, min(state["c"], int(state["rate"] * 2) + 1))
            print(f"  {len(challenged) or len(limited)} 頁被網站擋下，降速為每秒 {state['rate']:.1f} 頁")
            wait_for_human(driver, urls[(challenged or limited)[0]])
            strikes = 0
        elif limited:
            state["rate"] = RATE_START if rate <= 0 else max(RATE_MIN, rate * RATE_DOWN)
            strikes = strikes + 1 if len(limited) == len(chunk) else 0
            print(f"  {len(limited)} 頁被限流，{RATELIMIT_WAIT} 秒後以每秒 {state['rate']:.1f} 頁重試")
            time.sleep(RATELIMIT_WAIT)
        elif rate > 0:
            state["rate"] = min(RATE_MAX, rate * RATE_UP)
            strikes = 0
    return results


def learned_rate(state: dict) -> float | None:
    """這次下載中「沒被擋過的最快速度」：低於任何曾被擋的速度、且整段順利的最高速度。
    從頭到尾都不限速也沒被擋 → None（不需要記錄）。"""
    limit = min(state.get("limited", []), default=float("inf"))
    ok = [r for r in state.get("ok", []) if r < limit]
    return max(ok) if ok else None


def load_site_rates() -> dict[str, float]:
    try:
        with open(SITE_RATES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_site_rate(host: str, rate: float) -> None:
    data = load_site_rates()
    data[host] = round(rate, 2)
    try:
        with open(SITE_RATES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError as e:
        print(f"  ⚠ 無法儲存網站速度記錄：{e}")


def parse_chapter_html(html: str, url: str, title_hint: str) -> tuple[str, str, str | None]:
    """（給子進程用）解析一頁 HTML，回傳 (標題, 正文, 下一分頁網址)。"""
    soup = BeautifulSoup(html, "lxml")
    page_title = extract_title(soup)
    title = title_hint or page_title
    if title_hint and page_title.startswith(title_hint) and len(page_title) > len(title_hint):
        title = page_title
    continuation = find_continuation(soup, url)
    return title, extract_content(soup, title, inplace=True), continuation


def download_fast(driver, chapters, indices, cache: ChapterCache, state: dict, batch_size: int) -> None:
    """快速模式：每批 batch_size 章抓完（含一章多頁、退回瀏覽器重抓）就存進暫存，中斷時最多只損失一批。"""
    from concurrent.futures import ProcessPoolExecutor

    t0 = time.time()
    done = 0
    with ProcessPoolExecutor() as pool:
        for b in range(0, len(indices), batch_size):
            batch = indices[b:b + batch_size]
            titles = {i: chapters[i][0] for i in batch}
            parts: dict[int, list[str]] = {i: [] for i in batch}
            failed: set[int] = set()
            pending = [(i, chapters[i][1]) for i in batch]  # (章節索引, 要抓的網址)
            seen = {u for _, u in pending}
            while pending:  # 第二輪以後處理「同一章的第 2、3… 分頁」
                htmls = fetch_many_with_verify(driver, [u for _, u in pending], state)
                futures = []
                for (i, u), html in zip(pending, htmls):
                    if html is None:
                        failed.add(i)
                    else:
                        futures.append((i, pool.submit(parse_chapter_html, html, u, titles[i])))
                pending = []
                for i, fut in futures:
                    title, content, cont = fut.result()
                    titles[i] = title
                    parts[i].append(content)
                    if cont and cont not in seen:
                        seen.add(cont)
                        pending.append((i, cont))

            for i in batch:
                url = chapters[i][1]
                content = "\n".join(p for p in parts[i] if p)
                # 原始 HTML 抓不到正文（可能是 JS 動態產生）→ 退回用瀏覽器開啟
                if i in failed or len(content) < MIN_CONTENT_LEN:
                    try:
                        titles[i], content, _ = fetch_chapter(driver, url, chapters[i][0])
                    except BlockedError:
                        raise
                    except RuntimeError as e:
                        print(f"  ⚠ 第 {i + 1} 章：{e}")
                        continue
                if content:
                    cache.put(url, titles[i], content)
                else:
                    print(f"  ⚠ 第 {i + 1} 章未擷取到正文：{url}")
            done += len(batch)
            print(f"  已下載 {done}/{len(indices)} 章（{time.time() - t0:.1f}s）")


def download_by_next(driver, url, output, max_pages, append, delay) -> None:
    pattern = url_pattern(url)
    with open(output, "a" if append else "w", encoding="utf-8") as f:
        for i in range(max_pages):
            try:
                title, content, soup = fetch_chapter(driver, url)
            except RuntimeError as e:
                print(f"{e}\n可用 --mode next --append {url} 從此處接續。")
                return
            write_chapter(f, title, content)
            print(f"[{i + 1}] {title}（{len(content)} 字）")

            nxt = find_link(soup, url, NEXT_CHAPTER_TEXT)
            # 下一章連回目錄或格式不同的頁面，代表已到最後一章
            if not nxt or nxt == url or url_pattern(nxt) != pattern:
                print("已到最後一章。")
                return
            url = nxt
            time.sleep(delay)


def download_toc_book(driver, args, chapters, output) -> None:
    """有目錄時的下載流程：略過暫存中已有的章節 → 下載 → 組合成 txt → 檢查品質 → 記錄網站速度。"""
    total = len(chapters)
    lo, hi = max(args.start, 1) - 1, min(args.end or total, total)
    cache = ChapterCache(cache_path(output))
    if lo > 0 and not len(cache) and os.path.exists(output):
        # 舊版（沒有暫存）下載的檔案：--start 只會下載後半段，直接覆寫會遺失前面的章節
        stem, ext = os.path.splitext(output)
        backup = f"{stem}.bak{ext}"
        os.replace(output, backup)
        print(f"⚠ 沒有找到「{output}」的下載暫存，原檔已備份為「{backup}」，新檔只會包含這次下載的章節。")
    indices =[i for i in range(lo, hi) if args.refresh or chapters[i][1] not in cache]
    skipped = (hi - lo) - len(indices)
    if skipped:
        print(f"已有 {skipped} 章下載過（暫存：{cache.path}），自動略過；要重新下載請加 --refresh")

    host = urlparse(args.url).netloc
    t0 = time.time()
    completed = False
    try:
        if not indices:
            print("指定範圍的章節都已下載過。")
        elif args.slow:
            download_by_toc(driver, chapters, indices, cache, args.delay)
        else:
            download_fast(driver, chapters, indices, cache, args.state, args.batch_size)
        completed = True
    except KeyboardInterrupt:
        print("\n使用者中斷。已下載的章節都已保存，重新執行同一指令即可接續。")
    except BlockedError:
        raise
    except RuntimeError as e:
        print(f"\n✗ {e}\n已下載的章節都已保存，重新執行同一指令即可接續。")
    finally:
        cache.close()
        written, chars, missing = write_book(output, chapters, cache)
        print(f"{'完成' if completed else '已輸出目前進度'}：共 {written} 章、{chars:,} 字，"
              f"耗時 {time.time() - t0:.1f} 秒 → {output}")
        if missing:
            print(f"  尚未下載的章節（{len(missing)} 章）：{_ranges(missing)}")
        rate = learned_rate(args.state)
        if rate:
            save_site_rate(host, rate)
            print(f"  已記錄 {host} 的穩定速度：每秒 {rate:.1f} 頁（下次直接從這個速度開始）")

    warnings = check_quality(chapters, cache)
    if warnings:
        print(f"\n⚠ 有 {len(warnings)} 個章節可能有問題，請檢查：")
        for w in warnings[:20]:
            print(f"  {w}")
        if len(warnings) > 20:
            print(f"  …另外還有 {len(warnings) - 20} 個")
        print("  要重新下載某幾章：--start N --end M --refresh")


def main() -> None:
    parser = argparse.ArgumentParser(description="通用小說下載器")
    parser.add_argument("url", help="目錄頁網址（或 --mode next 時的起始章節網址）")
    parser.add_argument("-o", "--output", help="輸出檔名（預設為「書名_網站名.txt」）")
    parser.add_argument("--mode", choices=["auto", "toc", "next"], default="auto",
                        help="auto：自動判斷；toc：解析目錄；next：一路點下一章")
    parser.add_argument("--start", type=int, default=1, help="從第幾章開始下載")
    parser.add_argument("--end", type=int, default=0, help="下載到第幾章（預設全部）")
    parser.add_argument("--refresh", action="store_true",
                        help="重新下載指定範圍內的章節（預設會略過暫存中已下載的章節）")
    parser.add_argument("--reverse", action="store_true", help="目錄為新→舊排列時使用")
    parser.add_argument("--list", action="store_true", help="只列出章節清單，不下載")
    parser.add_argument("--max-pages", type=int, default=100000, help="next 模式最多抓幾章")
    parser.add_argument("--append", action="store_true", help="next 模式附加到既有檔案")
    parser.add_argument("--delay", type=float, default=0.5, help="慢速/next 模式每章間隔秒數")
    parser.add_argument("--slow", action="store_true", help="改用瀏覽器逐章開啟（JS 動態網站用）")
    parser.add_argument("-c", "--concurrency", type=int, default=8, help="快速模式同時下載數（太高會觸發網站人機驗證）")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="快速模式每批幾章（每批完成就存進暫存，中斷時最多損失一批）")
    parser.add_argument("-r", "--rate", type=float, default=None,
                        help="快速模式起始速度，每秒幾頁（0 = 不限；預設沿用上次記錄的速度，沒有記錄則不限）")
    parser.add_argument("--profile", default=CHROME_PROFILE,
                        help="Chrome 使用者資料夾（預設為程式旁的 chrome_profile；空字串表示不使用）")
    parser.add_argument("--show", action="store_true", help="下載時顯示瀏覽器視窗（預設縮到工作列）")
    parser.add_argument("--headless", action="store_true",
                        help="完全不開視窗（注意：有 Cloudflare 的網站通常會直接封鎖）")
    parser.add_argument("--convert", default="s2tw",
                        help="OpenCC 簡轉繁設定（預設 s2tw 台灣正體；s2twp 含台灣用語；none 不轉換）")
    args = parser.parse_args()
    setup_converter(args.convert)

    try:
        driver = build_driver(args.profile, args.headless)
    except RuntimeError as e:
        print(f"✗ {e}")
        return
    if not args.headless and not args.show:
        driver.minimize_window()
        WINDOW["minimized"] = True
    driver.set_script_timeout(600)
    try:
        soup = load_soup(driver, args.url)
        book = to_traditional(extract_book_name(soup))
        site = to_traditional(extract_site_name(soup, args.url))
        output = args.output or safe_filename(f"{book}_{site}") + ".txt"

        # 起始速度：命令列指定 > 上次記錄的速度 > 不限
        host = urlparse(args.url).netloc
        rate = args.rate
        if rate is None:
            rate = load_site_rates().get(host, 0.0)
            if rate:
                print(f"沿用上次記錄的速度：每秒 {rate:.1f} 頁（可用 --rate 指定）")
        args.state = {"c": args.concurrency, "rate": rate}

        toc_url = args.url
        if args.mode != "next" and expand_toc(driver):
            soup = BeautifulSoup(driver.page_source, "lxml")
            toc_url = driver.current_url
        chapters = [] if args.mode == "next" else extract_chapter_links(soup, toc_url)
        if chapters:
            chapters = collect_paginated_toc(driver, soup, toc_url, chapters, args.state)
        if args.reverse:
            chapters.reverse()

        if args.mode == "toc" or len(chapters) >= MIN_TOC_LINKS:
            print(f"書名：{book}，共 {len(chapters)} 章 → {output}")
            if args.list:
                for i, (title, url) in enumerate(chapters, 1):
                    print(f"{i:>5}. {title}  {url}")
                return
            download_toc_book(driver, args, chapters, output)
        else:
            print(f"未偵測到目錄，改用「下一章」模式 → {output}")
            download_by_next(driver, args.url, output, args.max_pages, args.append, args.delay)
    except BlockedError as e:
        print(f"\n✗ {e}")
        if args.headless:
            print("  這個網站不接受無頭（headless）瀏覽器，請拿掉 --headless 再試一次。")
    except KeyboardInterrupt:
        print("\n使用者中斷。")
    finally:
        driver.quit()
    print("Program End!")


if __name__ == "__main__":
    main()
