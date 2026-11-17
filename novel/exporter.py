"""輸出：txt / EPUB（都由暫存產生），以及刪除每章都有的雜訊行。"""

import html as html_lib
import re
import time
import uuid
import zipfile
from collections import defaultdict

from .config import REPEATED_LINE_MIN_CHAPTERS, REPEATED_LINE_MIN_LEN, REPEATED_LINE_RATIO
from .converter import to_traditional
from .storage import ChapterCache, cover_path, image_type


FORMATS = {"txt": ["txt"], "epub": ["epub"], "both": ["txt", "epub"]}


def find_repeated_lines(contents: list[str]) -> set[str]:
    """找出出現在大部分章節中的行（網站宣傳文字、「(本章完)」等），每章只算一次。
    小說正文中重複最多的行（例如「嗯？」）實測只出現在約 7% 的章節，門檻 50% 不會誤刪。"""
    if len(contents) < REPEATED_LINE_MIN_CHAPTERS:
        return set()
    counts: dict[str, int] = defaultdict(int)
    for content in contents:
        for line in {s.strip() for s in content.splitlines()}:
            if len(line) >= REPEATED_LINE_MIN_LEN:
                counts[line] += 1
    need = len(contents) * REPEATED_LINE_RATIO
    return {line for line, c in counts.items() if c >= need}


def remove_lines(content: str, lines: set[str]) -> str:
    return "\n".join(s for s in content.splitlines() if s.strip() not in lines)


def collect_book(chapters, cache: ChapterCache, clean: bool = True):
    """依目錄順序取出暫存中的章節，刪除每章都有的雜訊行（clean=True），再簡轉繁。
    回傳 (items [(標題, 正文)], 各章網址, 缺少的章節編號[從 1 起算], 刪除的雜訊行)。"""
    raw, urls, missing = [], [], []
    for n, (_, url) in enumerate(chapters, 1):
        if url in cache:
            raw.append(cache.get(url))
            urls.append(url)
        else:
            missing.append(n)
    removed = find_repeated_lines([c for _, c in raw]) if clean else set()
    items = [(to_traditional(t), to_traditional(remove_lines(c, removed) if removed else c)) for t, c in raw]
    return items, urls, missing, removed


def write_txt(path: str, items: list[tuple[str, str]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for title, content in items:
            f.write(f"{title}\n\n{content}\n\n")


def write_book(output: str, chapters, cache: ChapterCache) -> tuple[int, int, list[int]]:
    """把暫存中所有已下載的章節組合成 txt。回傳 (寫入章數, 字數, 缺少的章節編號)。"""
    items, _, missing, _ = collect_book(chapters, cache)
    write_txt(output, items)
    return len(items), sum(len(c) for _, c in items), missing

_EPUB_CSS = """body { font-family: serif; line-height: 1.8; }
h2 { font-size: 1.3em; margin: 1em 0; }
p { text-indent: 2em; margin: 0 0 0.6em 0; }
div.cover { text-align: center; margin: 0; padding: 0; }
div.cover img { max-width: 100%; max-height: 100%; }
"""


def _xhtml(title: str, body: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            'xml:lang="zh-Hant" lang="zh-Hant">\n'
            f'<head><meta charset="utf-8"/><title>{_xml_text(title)}</title>'
            f'<link rel="stylesheet" type="text/css" href="style.css"/></head>\n<body>\n{body}\n</body>\n</html>\n')


def _xml_text(text: str) -> str:
    # 移除 XML 不允許的控制字元，再跳脫 & < > "
    return html_lib.escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text))


def _group_volumes(volumes: list[str]) -> list[tuple[str, list[int]]]:
    """把連續同一卷的章節分在一起：[(卷名, [章節索引])]；卷名為空字串代表不屬於任何卷。"""
    groups: list[tuple[str, list[int]]] = []
    for i, vol in enumerate(volumes):
        if groups and groups[-1][0] == vol:
            groups[-1][1].append(i)
        else:
            groups.append((vol, [i]))
    return groups


def write_epub(path: str, items: list[tuple[str, str]], book: str, author: str = "", source: str = "",
               volumes: list[str] | None = None, cover: bytes | None = None) -> None:
    """輸出 EPUB 3（附 EPUB 2 的 toc.ncx，舊閱讀器也能看到目錄），每章一個 XHTML 檔。
    volumes：每章的卷名（與 items 對應），有分卷時目錄會分兩層；cover：封面圖片的原始位元組。"""
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, source or book)}"
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    names = [f"c{n:05d}.xhtml" for n in range(1, len(items) + 1)]
    titles = [_xml_text(t) for t, _ in items]
    groups = _group_volumes(volumes if volumes and len(volumes) == len(items) else [""] * len(items))
    cover_kind = image_type(cover) if cover else None

    cover_meta = cover_item = cover_spine = ""
    if cover_kind:
        ext, media = cover_kind
        cover_meta = '<meta name="cover" content="cover-img"/>\n'  # EPUB 2 閱讀器用
        cover_item = (f'<item id="cover-img" href="cover.{ext}" media-type="{media}" properties="cover-image"/>\n'
                      '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>\n')
        cover_spine = '<itemref idref="cover"/>\n'
    manifest = "".join(f'<item id="c{n}" href="{name}" media-type="application/xhtml+xml"/>\n'
                       for n, name in enumerate(names, 1))
    spine = "".join(f'<itemref idref="c{n}"/>\n' for n in range(1, len(items) + 1))
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh-Hant">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{book_id}</dc:identifier>
<dc:title>{_xml_text(book)}</dc:title>
<dc:creator>{_xml_text(author or "未知")}</dc:creator>
<dc:language>zh-Hant</dc:language>
<dc:source>{_xml_text(source)}</dc:source>
<meta property="dcterms:modified">{modified}</meta>
{cover_meta}</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="css" href="style.css" media-type="text/css"/>
{cover_item}{manifest}</manifest>
<spine toc="ncx">
{cover_spine}{spine}</spine>
</package>
"""

    # 目錄：有分卷時兩層（卷 → 章），卷的連結指向該卷第一章
    def nav_li(i: int) -> str:
        return f'<li><a href="{names[i]}">{titles[i]}</a></li>\n'

    def ncx_point(i: int, children: str = "", label: str | None = None, pid: str = "") -> str:
        # 同一個目標的 playOrder 必須相同，所以卷和它的第一章共用 playOrder
        return (f'<navPoint id="{pid or f"p{i + 1}"}" playOrder="{i + 1}"><navLabel><text>'
                f'{label if label is not None else titles[i]}</text></navLabel>'
                f'<content src="{names[i]}"/>{children}</navPoint>\n')

    nav_items, points = [], []
    for v, (vol, idx) in enumerate(groups, 1):
        if vol:
            nav_items.append(f'<li><a href="{names[idx[0]]}">{_xml_text(vol)}</a>\n<ol>\n'
                             + "".join(nav_li(i) for i in idx) + "</ol></li>\n")
            points.append(ncx_point(idx[0], "\n" + "".join(ncx_point(i) for i in idx),
                                    _xml_text(vol), f"v{v}"))
        else:
            nav_items += [nav_li(i) for i in idx]
            points += [ncx_point(i) for i in idx]
    nav = _xhtml(book, f'<nav epub:type="toc" id="toc"><h1>{_xml_text(book)}</h1>\n<ol>\n'
                       f'{"".join(nav_items)}</ol></nav>')
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{book_id}"/></head>
<docTitle><text>{_xml_text(book)}</text></docTitle>
<navMap>
{"".join(points)}</navMap>
</ncx>
"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
    with zipfile.ZipFile(path, "w") as z:
        # mimetype 必須是第一個檔案且不壓縮
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", _EPUB_CSS, compress_type=zipfile.ZIP_DEFLATED)
        if cover_kind:
            z.writestr(f"OEBPS/cover.{cover_kind[0]}", cover)  # 圖片已壓縮過，不再壓縮
            z.writestr("OEBPS/cover.xhtml", _xhtml(book, f'<div class="cover"><img src="cover.{cover_kind[0]}" '
                                                         f'alt="{_xml_text(book)}"/></div>'),
                       compress_type=zipfile.ZIP_DEFLATED)
        for name, (title, content) in zip(names, items):
            paras = "\n".join(f"<p>{_xml_text(line)}</p>" for line in content.splitlines() if line.strip())
            z.writestr(f"OEBPS/{name}", _xhtml(title, f"<h2>{_xml_text(title)}</h2>\n{paras}"),
                       compress_type=zipfile.ZIP_DEFLATED)


def export_book(base: str, fmt: str, chapters, cache: ChapterCache, meta: dict):
    """依 --format 輸出檔案。meta：book、author、source、volumes（{網址: 卷名}）、clean（是否刪除雜訊行）。
    封面使用暫存資料夾中已下載的圖片（cover_path）。
    回傳 (輸出的檔案路徑, 章數, 字數, 缺少的章節編號, 刪除的雜訊行)。"""
    items, urls, missing, removed = collect_book(chapters, cache, meta.get("clean", True))
    paths = []
    for ext in FORMATS[fmt]:
        path = f"{base}.{ext}"
        if ext == "txt":
            write_txt(path, items)
        else:
            vol_map = meta.get("volumes") or {}
            volumes = [to_traditional(vol_map.get(u, "")) for u in urls] if vol_map else None
            cover = None
            if (cp := cover_path(base)):
                with open(cp, "rb") as f:
                    cover = f.read()
            write_epub(path, items, meta.get("book", ""), meta.get("author", ""), meta.get("source", ""),
                       volumes, cover)
        paths.append(path)
    return paths, len(items), sum(len(c) for _, c in items), missing, removed


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
