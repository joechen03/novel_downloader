"""目錄頁解析：章節連結、書名/網站名/作者、展開完整目錄、分頁目錄、分卷、封面網址。"""

import re
import time
from collections import OrderedDict, defaultdict
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString

from .config import MAX_TOC_PAGES, WAIT_SECONDS
from .fetcher import fetch_many_with_verify


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

_BOOK_NAME_SUFFIX = re.compile(r"(最新章[節节]列表|最新章[節节]|章[節节]列表|章[節节]目[錄录]|全文目[錄录]"
                               r"|全文[閱阅][讀读]|免[費费][閱阅][讀读]|在[線线][閱阅][讀读]|線上看|txt下[載载])$",
                               re.IGNORECASE)


def _clean_book_name(name: str) -> str:
    name = re.sub(r"【[^】]*】|\[[^\]]*\]", "", name)  # 【完結+番外】等標記
    name = name.strip(" 　_-|｜")
    while _BOOK_NAME_SUFFIX.search(name) and len(_BOOK_NAME_SUFFIX.sub("", name)) >= 2:
        name = _BOOK_NAME_SUFFIX.sub("", name).strip(" 　_-|｜")  # 霍格沃茨的學習面板最新章節列表 → 霍格沃茨的學習面板
    return name


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


def extract_author(soup: BeautifulSoup) -> str:
    """作者（EPUB 用）：og:novel:author / book:author / meta author，取不到回傳空字串。"""
    for attrs in ({"property": "og:novel:author"}, {"property": "book:author"}, {"name": "author"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content", "").strip():
            return m["content"].strip()
    return ""

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
_RANGE_TEXT = r"第?\s*\d+\s*[-~－～—至]+\s*\d+.{0,6}"  # 第1-100章、第0000--0100章、1~100

_TOC_PAGE_TEXT = re.compile(rf"^(上一?[頁页]|下一?[頁页]|尾[頁页]|末[頁页]|\d{{1,4}}|第\s*\d+\s*[頁页]|{_RANGE_TEXT})$")

_RANGE_ONLY = re.compile(rf"^{_RANGE_TEXT}$")


# 書籍介紹頁上連到目錄頁的連結
_TOC_LINK_TEXT = re.compile(r"^(目[錄录]|章[節节]目[錄录]|全部目[錄录]|查看目[錄录]|小[說说]目[錄录]|目[錄录]列表)$")

FEW_CHAPTERS = 30  # 找到的章節少於這個數時，若頁面有「目錄」連結就改開目錄頁（書籍介紹頁通常只列最新幾章）


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


def find_toc_link(soup: BeautifulSoup, page_url: str) -> str | None:
    """書籍介紹頁上「目錄」連結的網址（同網站、不是目前這頁；有書號時必須包含書號）。"""
    host = urlparse(page_url).netloc
    book_ids = re.findall(r"\d{3,}", urlparse(page_url).path)
    for a in soup.find_all("a", href=True):
        if not _TOC_LINK_TEXT.match(a.get_text(strip=True)):
            continue
        url = urljoin(page_url, a["href"].strip()).split("#")[0]
        parsed = urlparse(url)
        if (parsed.netloc == host and _norm_url(url) != _norm_url(page_url)
                and (not book_ids or any(b in parsed.path + "?" + parsed.query for b in book_ids))):
            return url
    return None


def is_toc_index(chapters) -> bool:
    """找到的「章節」其實是目錄分頁（名稱幾乎都是「第0000--0100章」這種範圍）。"""
    ranges = sum(1 for t, _ in chapters if _RANGE_ONLY.match(t.strip()))
    return len(chapters) >= 2 and ranges >= len(chapters) * 0.8


def collect_toc_index(driver, chapters, state) -> list[tuple[str, str]]:
    """目錄首頁只列出章節範圍（第0000--0100章 → 另一頁）時，抓回每一頁的章節，依首頁的順序合併。"""
    urls = [u for _, u in chapters]
    pattern, merged = None, []
    for url, html in zip(urls, fetch_many_with_verify(driver, urls, state)):
        if html is None:
            print(f"  ⚠ 目錄分頁下載失敗：{url}")
            continue
        found = extract_chapter_links(BeautifulSoup(html, "lxml"), url, pattern)
        if found and pattern is None:
            pattern = url_pattern(found[0][1])  # 之後的分頁只取同格式的章節連結
        merged += found
    merged = _dedup_keep_last(merged)
    print(f"目錄分成 {len(urls)} 頁（{chapters[0][0]} …），合併後 {len(merged)} 章")
    return merged


def collect_paginated_toc(driver, soup, toc_url, chapters, state) -> list[tuple[str, str]]:
    """目錄被拆成多頁時，抓回其他頁並合併。沒有分頁時原樣回傳。"""
    if is_toc_index(chapters):
        return collect_toc_index(driver, chapters, state)
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

_VOL_NUM = "0-9０-９零〇一二兩两三四五六七八九十百千"


# 目錄頁上的分卷標題（不是連結的文字）：第一卷、第2部、卷三、正文卷、作品相關
_VOLUME_HEADING = re.compile(rf"第\s*[{_VOL_NUM}]+\s*[卷部]|^\s*卷\s*[{_VOL_NUM}]+|正文卷|作品相[關关]")


# 章節名稱裡的分卷：「第一卷 啟程 第1章 xxx」
_TITLE_VOLUME = re.compile(rf"^(第\s*[{_VOL_NUM}]+\s*[卷部](?:\s*[^\s第]{{1,20}})?)\s+第\s*[{_VOL_NUM}]+\s*[章回節节話话]")


def extract_volumes(soup: BeautifulSoup, base_url: str, chapters) -> dict[str, str]:
    """找出每章所屬的分卷，回傳 {章節網址: 卷名}；分不到兩卷以上時回傳空 dict。
    優先用目錄頁上夾在章節連結之間的分卷標題，沒有時改從章節名稱開頭的「第X卷 …」取出。"""
    urls = {u for _, u in chapters}
    volumes: dict[str, str] = {}
    current = ""
    for node in soup.body.descendants if soup.body else []:
        if isinstance(node, NavigableString):
            if isinstance(node, Comment) or node.find_parent("a"):
                continue
            text = " ".join(node.split())
            if 2 <= len(text) <= 50 and _VOLUME_HEADING.search(text):
                current = text
        elif node.name == "a" and node.get("href"):
            url = urljoin(base_url, node["href"].strip()).split("#")[0]
            if url in urls:
                volumes[url] = current  # 同一章出現多次（「最新章節」區塊）時以最後的位置為準
    if len(set(volumes.values()) - {""}) < 2:
        volumes = {}
        for title, url in chapters:
            if m := _TITLE_VOLUME.match(title):
                volumes[url] = " ".join(m.group(1).split())
        if len(set(volumes.values())) < 2:
            return {}
    return {u: v for u, v in volumes.items() if v}


def find_cover_url(soup: BeautifulSoup, base_url: str, book: str = "") -> str | None:
    """找出封面圖片網址：og:image → alt/title 含書名的圖片 → 網址看起來像封面的圖片。"""
    meta = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "og:image"})
    candidates = [meta["content"]] if meta and meta.get("content") else []
    for img in soup.find_all("img"):
        src = img.get("data-original") or img.get("data-src") or img.get("src") or ""
        alt = (img.get("alt") or "") + (img.get("title") or "")
        if src and ((book and book in alt) or re.search(r"cover|fengmian|bookimg|/image/", src, re.IGNORECASE)):
            candidates.append(src)
    for src in candidates:
        src = src.strip()
        if src and not src.startswith("data:") and not re.search(r"nocover|noimg|no_cover|default", src, re.I):
            return urljoin(base_url, src)
    return None
