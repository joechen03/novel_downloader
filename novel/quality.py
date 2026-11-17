"""品質檢查：字數異常、內容重複、章號不符。"""

import re
import statistics

from .config import MIN_CONTENT_LEN
from .storage import ChapterCache


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "万": 10000}

_CHAPTER_NUM = re.compile(r"第\s*([0-9０-９]+|[零〇一二兩两三四五六七八九十百千萬万]+)\s*[章回節节話话]")


def _cn_to_int(text: str) -> int:
    """中文數字轉整數：一百二十三 → 123、十五 → 15、一二三 → 123。"""
    if all(ch in _CN_DIGITS for ch in text):  # 一二三 這種逐位寫法
        return int("".join(str(_CN_DIGITS[ch]) for ch in text))
    total, section, num = 0, 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                total += (section + num) * unit
                section = 0
            else:
                section += (num or 1) * unit
            num = 0
    return total + section + num


def chapter_number(title: str) -> int | None:
    """從「第123章」「第一百二十三章」取出章節編號，沒有則回傳 None。"""
    m = _CHAPTER_NUM.search(title or "")
    if not m:
        return None
    s = m.group(1)
    return int(s) if s[0] in "0123456789０１２３４５６７８９" else _cn_to_int(s)


def check_quality(chapters, cache: ChapterCache) -> list[str]:
    """檢查已下載章節中可疑的地方：字數過少/過多、和其他章內容重複、章節頁標題的章號和目錄不同。"""
    items = [(n, chapters[n - 1][0], *cache.get(url), cache.meta(url).get("page_title", ""))
             for n, (_, url) in enumerate(chapters, 1) if url in cache]
    if len(items) < 3:
        return []
    median = statistics.median(len(c) for _, _, _, c, _ in items)
    warnings = []
    seen: dict[str, int] = {}
    for n, toc_title, title, content, page_title in items:
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
        a, b = chapter_number(toc_title), chapter_number(page_title)
        if a is not None and b is not None and a != b:
            warnings.append(f"第 {n} 章：目錄是「{toc_title}」，但頁面標題是「{page_title}」，連結可能指到別章")
    return warnings
