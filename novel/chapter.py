"""章節頁解析：正文擷取、標題比對、一章多頁、下一章連結。"""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString

from .browser import load_soup
from .config import (AD_PATTERNS, CONTINUE_PAGE_TEXT, MIN_CONTENT_LEN, REMOVE_TAGS, WRAPPER_KEEP_LEN,
                     WRAPPER_TAGS)


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

_KEY_CONVERTER = {}


def title_key(text: str) -> str:
    """比對標題用的正規化：去掉空白，有 OpenCC 時再轉成簡體。
    同一網站的目錄和章節頁可能一個繁體、一個簡體（69shuba 的目錄頁會被轉成繁體，正文仍是簡體）。"""
    text = re.sub(r"\s+", "", text or "")
    if "t2s" not in _KEY_CONVERTER:  # 子進程不會執行 setup_converter，所以這裡自己建立
        try:
            import opencc
            _KEY_CONVERTER["t2s"] = opencc.OpenCC("t2s")
        except ImportError:
            _KEY_CONVERTER["t2s"] = None
    conv = _KEY_CONVERTER["t2s"]
    return conv.convert(text) if conv and text else text


def better_title(title_hint: str, page_title: str) -> str:
    """優先用目錄上的名稱；目錄名稱不完整時（如「第3章」），若頁面標題以它開頭且更長，改用頁面標題。"""
    if not title_hint:
        return page_title
    hint, page = title_key(title_hint), title_key(page_title)
    return page_title if page.startswith(hint) and len(page) > len(hint) else title_hint

_HEADER_META = re.compile(r"作者[:：]|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}|更新時間|更新时间|字數[:：]|字数[:：]")


def extract_content(soup: BeautifulSoup, title: str = "", inplace: bool = False) -> str:
    if not inplace:
        soup = BeautifulSoup(str(soup), "lxml")  # 複製一份，避免動到原本的 soup
    for tag in soup(REMOVE_TAGS):
        if tag.decomposed:  # 外層已經被刪掉
            continue
        if tag.name in WRAPPER_TAGS and len(tag.get_text(strip=True)) > WRAPPER_KEEP_LEN:
            tag.unwrap()
        else:
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
    tkey = title_key(title)
    for line in best.get_text().splitlines():
        line = line.strip().replace("\xa0", "").replace("\u3000", "").strip()
        if not line or re.fullmatch(r"[←→↑↓\-—=*·.。…\s]+", line):
            continue
        if len(line) < 60 and AD_PATTERNS.search(line):
            continue
        if len(lines) < 3:
            key = title_key(line)
            # 正文開頭重複的標題（例如「書名第1章 xxx」）；比對時忽略空白（網站常用 &nbsp; 或全形空白）
            if tkey and tkey in key and len(key) <= len(tkey) + 30:
                continue
            # 正文開頭的發布日期／作者資訊（例如 69shuba 的「2025-12-10 作者： xxx」）
            if len(line) < 40 and _HEADER_META.search(line):
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
    title = better_title(title_hint, extract_title(soup))
    parts = [extract_content(soup, title)]

    seen = {url}
    page_url = find_continuation(soup, url)
    while page_url and page_url not in seen:
        seen.add(page_url)
        soup = load_soup(driver, page_url, ready)
        parts.append(extract_content(soup, title))
        page_url = find_continuation(soup, page_url)

    return title, "\n".join(p for p in parts if p), soup


def parse_chapter_html(html: str, url: str, title_hint: str) -> tuple[str, str, str | None, str]:
    """（給子進程用）解析一頁 HTML，回傳 (標題, 正文, 下一分頁網址, 章節頁上的標題)。"""
    soup = BeautifulSoup(html, "lxml")
    page_title = extract_title(soup)
    title = better_title(title_hint, page_title)
    continuation = find_continuation(soup, url)
    return title, extract_content(soup, title, inplace=True), continuation, page_title
