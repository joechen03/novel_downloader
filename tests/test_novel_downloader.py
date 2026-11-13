"""自動化測試：不開瀏覽器、不連網，只測解析規則與資料處理。

執行（在專案根目錄）：
    python -m unittest discover -s tests -v
    （有裝 pytest 的話也可以直接 pytest）
"""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import novel_downloader as nd  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def load_fixture(name: str) -> tuple[BeautifulSoup, str]:
    """回傳 (soup, 實際網址)。樣本由 tests/save_fixture.py 產生。"""
    with open(os.path.join(FIXTURE_DIR, name + ".html"), encoding="utf-8") as f:
        html = f.read()
    final = re.search(r"<!-- final: (\S+) -->", html).group(1)
    return soup(html), final


# ---------------------------------------------------------------------------
# 真實網站的目錄頁樣本
# ---------------------------------------------------------------------------
# (樣本名稱, 書名, 網站名, 章數, 第一章, 最後一章)
SITE_CASES = [
    ("czbooks_toc", "折月亮", "小說狂人", 236, "第1頁", "第236頁"),
    ("hjwzw_toc", "術師手冊", "黃金屋", 1381, "第1章 我被逮捕了", "番外·職場的術師手冊(下)"),
    ("ixdzs_toc", "封總，太太想跟你離婚很久了", "愛下電子書", 757, "第1章  她該清醒了", "第757章  直接證據"),
]


class TestRealSites(unittest.TestCase):
    def test_sites(self):
        for name, book, site, count, first, last in SITE_CASES:
            with self.subTest(name):
                s, url = load_fixture(name)
                self.assertEqual(nd.extract_book_name(s), book)
                self.assertEqual(nd.extract_site_name(s, url), site)
                chapters = nd.extract_chapter_links(s, url)
                self.assertEqual(len(chapters), count)
                self.assertEqual(chapters[0][0], first)
                self.assertEqual(chapters[-1][0], last)
                self.assertEqual(len({u for _, u in chapters}), count, "章節網址不應重複")
                # 這些網站的目錄都沒有分頁，不能誤判
                self.assertEqual(nd.find_toc_pages(s, url, {u for _, u in chapters}), [])


# ---------------------------------------------------------------------------
# 目錄解析
# ---------------------------------------------------------------------------
class TestUrlPattern(unittest.TestCase):
    def test_patterns(self):
        cases = {
            "https://a.com/book/17472/10322423.html": "/book/#/*.html",
            "https://a.com/n/sklkm61fg/sk2e5?chapterNumber=0": "/n/sklkm#fg/*",
            "https://a.com/book/17472/": "/book/#/",
            "https://a.com/Book/Read/47403,22279063": "/Book/Read/*",
        }
        for url, expected in cases.items():
            self.assertEqual(nd.url_pattern(url), expected, url)

    def test_alphanumeric_ids_same_group(self):
        self.assertEqual(nd.url_pattern("https://a.com/n/x/sk2e5"), nd.url_pattern("https://a.com/n/x/sknjc"))


def toc_html(chapter_ids, latest_ids=(), extra="") -> str:
    latest = "".join(f'<li><a href="/book/1/{i}.html">第{i}章</a></li>' for i in latest_ids)
    body = "".join(f'<li><a href="/book/1/{i}.html">第{i}章</a></li>' for i in chapter_ids)
    return (f"<html><head><title>測試書_測試網</title></head><body>"
            f'<div class="nav"><a href="/">首頁</a><a href="/sort/1/">玄幻</a></div>'
            f'<ul class="latest">{latest}</ul><ul class="all">{body}</ul>{extra}</body></html>')


class TestChapterLinks(unittest.TestCase):
    BASE = "https://a.com/book/1/"

    def test_basic_order(self):
        ch = nd.extract_chapter_links(soup(toc_html(range(1, 11))), self.BASE)
        self.assertEqual([t for t, _ in ch], [f"第{i}章" for i in range(1, 11)])
        self.assertEqual(ch[0][1], "https://a.com/book/1/1.html")

    def test_latest_block_removed(self):
        ch = nd.extract_chapter_links(soup(toc_html(range(1, 11), latest_ids=[10, 9, 8])), self.BASE)
        self.assertEqual([t for t, _ in ch], [f"第{i}章" for i in range(1, 11)])

    def test_dedup_keeps_last_in_same_container(self):
        html = ("<ul>" + "".join(f'<li><a href="/book/1/{i}.html">第{i}章</a></li>' for i in (9, 10))
                + "".join(f'<li><a href="/book/1/{i}.html">第{i}章</a></li>' for i in range(1, 11)) + "</ul>")
        ch = nd.extract_chapter_links(soup(html), self.BASE)
        self.assertEqual([t for t, _ in ch], [f"第{i}章" for i in range(1, 11)])

    def test_pattern_argument(self):
        html = toc_html(range(1, 4), extra="".join(f'<a href="/x/{i}/">書{i}</a>' for i in range(20)))
        ch = nd.extract_chapter_links(soup(html), self.BASE, pattern="/book/#/*.html")
        self.assertEqual(len(ch), 3)
        self.assertEqual(nd.extract_chapter_links(soup(html), self.BASE, pattern="/nope/*"), [])

    def test_book_and_site_name(self):
        s = soup('<html><head><title>《折月亮_竹已【完結+番外】》最新 | 小說狂人</title>'
                 '<meta property="og:title" content="【免費小說】《折月亮_竹已【完結+番外】》"></head></html>')
        self.assertEqual(nd.extract_book_name(s), "折月亮")
        self.assertEqual(nd.extract_site_name(s, "https://czbooks.net/n/1"), "小說狂人")
        s = soup("<html><head><title>術師手冊/聽日/txt下載-黃金屋中文</title></head><body><h1>術師手冊</h1></body></html>")
        self.assertEqual(nd.extract_book_name(s), "術師手冊")
        self.assertEqual(nd.extract_site_name(s, "https://tw.hjwzw.com/"), "黃金屋")
        self.assertEqual(nd.extract_site_name(soup("<html></html>"), "https://www.example.com/"), "example")


# ---------------------------------------------------------------------------
# 分頁目錄
# ---------------------------------------------------------------------------
class TestPaginatedToc(unittest.TestCase):
    TOC = "https://a.com/book/123/"
    KNOWN = {"https://a.com/book/123/5.html", "https://a.com/book/123/7.html"}

    def test_select_options(self):
        html = ('<select><option value="/book/123/">1-100章</option>'
                '<option value="/book/123/index_2.html">101-200章</option>'
                '<option value="/book/123/index_3.html">201-250章</option></select>')
        self.assertEqual(nd.find_toc_pages(soup(html), self.TOC, self.KNOWN),
                         ["https://a.com/book/123/index_2.html", "https://a.com/book/123/index_3.html"])

    def test_page_number_links(self):
        html = ('<a href="?page=2">2</a><a href="?page=3">3</a><a href="?page=2">下一頁</a>'
                '<a href="/book/123/5.html">第5章</a>')
        self.assertEqual(nd.find_toc_pages(soup(html), self.TOC, self.KNOWN),
                         ["https://a.com/book/123/?page=2", "https://a.com/book/123/?page=3"])

    def test_other_books_and_chapters_ignored(self):
        html = ('<a href="/book/999/">2</a>'          # 別本書（沒有書號 123）
                '<a href="/book/123/7.html">7</a>'    # 章節網址
                '<a href="https://b.com/book/123/?page=2">2</a>'  # 其他網域
                '<a href="/book/123/?page=2">熱門推薦</a>')       # 文字不像頁碼
        self.assertEqual(nd.find_toc_pages(soup(html), self.TOC, self.KNOWN), [])

    def test_merge_order_and_latest_block(self):
        def ch(*ids):
            return [(f"第{i}章", f"https://a.com/book/123/{i}.html") for i in ids]
        pages = {
            "https://a.com/book/123/?page=10": ch(19, 20),
            "https://a.com/book/123/": ch(20, 1, 2),        # 開頭是「最新章節」
            "https://a.com/book/123/?page=2": ch(3, 4),
        }
        merged = nd.merge_toc_pages(pages)
        self.assertEqual([t for t, _ in merged], ["第1章", "第2章", "第3章", "第4章", "第19章", "第20章"])


# ---------------------------------------------------------------------------
# 正文擷取
# ---------------------------------------------------------------------------
def chapter_html(lines, title="第1章 開始", extra="") -> str:
    body = "<br>".join(lines)
    return (f"<html><body><h1>{title}</h1><div class='nav'><a href='/'>首頁</a></div>"
            f"<div id='content'>{body}</div>{extra}</body></html>")


class TestContent(unittest.TestCase):
    TEXT = ["這是正文第一段，內容足夠長所以會被選為正文區塊。" * 2, "第二段。" * 5, "第三段結束。"]

    def test_basic(self):
        c = nd.extract_content(soup(chapter_html(self.TEXT)), "第1章 開始")
        self.assertEqual(c.splitlines(), self.TEXT)

    def test_ads_and_symbols_removed(self):
        lines = ["←→", self.TEXT[0], "請收藏本站：www.abc.com", self.TEXT[1], "天才一秒記住本站"]
        c = nd.extract_content(soup(chapter_html(lines)), "")
        self.assertEqual(c.splitlines(), self.TEXT[:2])

    def test_long_line_with_ad_word_kept(self):
        long_line = "他說最新章節這種東西根本不重要，" * 5
        c = nd.extract_content(soup(chapter_html([self.TEXT[0], long_line])), "")
        self.assertIn(long_line, c)

    def test_repeated_title_skipped(self):
        lines = ["術師手冊第1章 開始", self.TEXT[0], self.TEXT[1]]
        c = nd.extract_content(soup(chapter_html(lines)), "第1章 開始")
        self.assertEqual(c.splitlines(), self.TEXT[:2])

    def test_does_not_modify_soup(self):
        s = soup(chapter_html(self.TEXT, extra="<script>x</script>"))
        nd.extract_content(s)
        self.assertIsNotNone(s.find("script"))

    def test_parse_chapter_html_title(self):
        title, content, cont = nd.parse_chapter_html(chapter_html(self.TEXT, title="第3章 真正的標題"),
                                                     "https://a.com/b/1/3.html", "第3章")
        self.assertEqual(title, "第3章 真正的標題")
        self.assertIsNone(cont)

    def test_continuation(self):
        url = "https://a.com/book/1/123.html"
        s = soup('<a href="123_2.html">下一頁</a>')
        self.assertEqual(nd.find_continuation(s, url), "https://a.com/book/1/123_2.html")
        s = soup('<a href="124.html">下一頁</a>')  # 其實是下一章
        self.assertIsNone(nd.find_continuation(s, url))
        s = soup('<a href="123_3.html">下一頁</a>')
        self.assertEqual(nd.find_continuation(s, "https://a.com/book/1/123_2.html"),
                         "https://a.com/book/1/123_3.html")


# ---------------------------------------------------------------------------
# 暫存、組合、品質檢查、調速
# ---------------------------------------------------------------------------
class TestCacheAndBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = os.path.join(self.tmp.name, "書_站.txt")
        self.path = nd.cache_path(self.output)
        nd.CONVERTER.update(enabled=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cache_path(self):
        self.assertEqual(self.path, os.path.join(self.tmp.name, nd.CACHE_DIRNAME, "書_站.jsonl"))

    def test_roundtrip_and_last_wins(self):
        c = nd.ChapterCache(self.path)
        c.put("u1", "第1章", "舊內容")
        c.put("u2", "第2章", "內容二\n第二行")
        c.put("u1", "第1章", "新內容")
        c.close()
        c2 = nd.ChapterCache(self.path)
        self.assertEqual(len(c2), 2)
        self.assertEqual(c2.get("u1"), ("第1章", "新內容"))
        self.assertEqual(c2.get("u2"), ("第2章", "內容二\n第二行"))

    def test_broken_last_line(self):
        c = nd.ChapterCache(self.path)
        c.put("u1", "第1章", "內容一")
        c.close()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"url": "u2", "title": "第2')  # 模擬寫到一半中斷
        c = nd.ChapterCache(self.path)
        self.assertEqual(len(c), 1)
        c.put("u3", "第3章", "內容三")
        c.close()
        c = nd.ChapterCache(self.path)
        self.assertEqual(sorted(c.data), ["u1", "u3"])

    def test_write_book(self):
        chapters = [(f"第{i}章", f"u{i}") for i in range(1, 6)]
        c = nd.ChapterCache(self.path)
        for i in (1, 2, 4):
            c.put(f"u{i}", f"第{i}章", f"內容{i}")
        c.close()
        written, chars, missing = nd.write_book(self.output, chapters, c)
        self.assertEqual((written, missing), (3, [3, 5]))
        with open(self.output, encoding="utf-8") as f:
            self.assertEqual(f.read(), "第1章\n\n內容1\n\n第2章\n\n內容2\n\n第4章\n\n內容4\n\n")

    def test_ranges(self):
        self.assertEqual(nd._ranges([1, 2, 3, 7, 9, 10]), "1-3, 7, 9-10")
        self.assertEqual(nd._ranges([]), "")

    def test_quality(self):
        normal = "正常的內容，" * 300
        chapters = [(f"第{i}章", f"u{i}") for i in range(1, 7)]
        c = nd.ChapterCache(self.path)
        c.put("u1", "第1章", normal + "一")
        c.put("u2", "第2章", "太短了")
        c.put("u3", "第3章", normal + "三")   # 開頭和第 1 章相同
        c.put("u4", "第4章", "不一樣的開頭。" * 260)
        c.put("u5", "第5章", "又一個開頭，" * 300)
        c.put("u6", "第6章", "超長，" * 30000)
        c.close()
        warnings = nd.check_quality(chapters, c)
        text = "\n".join(warnings)
        self.assertIn("第 2 章", text)
        self.assertIn("與第 1 章相同", text)
        self.assertIn("第 6 章", text)
        self.assertNotIn("第 4 章", text)
        self.assertEqual(len(warnings), 3)


class TestRate(unittest.TestCase):
    def test_learned_rate(self):
        self.assertIsNone(nd.learned_rate({"c": 8, "rate": 0}))
        self.assertEqual(nd.learned_rate({"ok": [10, 12.5, 15.6, 12], "limited": [19.5, 15.6]}), 12.5)
        self.assertIsNone(nd.learned_rate({"ok": [], "limited": [2.0]}))

    def test_site_rate_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = nd.SITE_RATES_FILE
            nd.SITE_RATES_FILE = os.path.join(tmp, "rates.json")
            try:
                self.assertEqual(nd.load_site_rates(), {})
                nd.save_site_rate("a.com", 12.345)
                nd.save_site_rate("b.com", 3)
                self.assertEqual(nd.load_site_rates(), {"a.com": 12.35, "b.com": 3})
            finally:
                nd.SITE_RATES_FILE = old


# ---------------------------------------------------------------------------
# 簡轉繁
# ---------------------------------------------------------------------------
try:
    import opencc  # noqa: F401
    HAS_OPENCC = True
except ImportError:
    HAS_OPENCC = False


class TestConvert(unittest.TestCase):
    def tearDown(self):
        nd.CONVERTER.update(enabled=False, t2s=None, s2t=None)

    def test_quotes_without_opencc(self):
        nd.CONVERTER.update(enabled=True, t2s=None, s2t=None)
        self.assertEqual(nd.to_traditional("“你好”‘嗯’"), "「你好」『嗯』")

    def test_disabled(self):
        nd.setup_converter("none")
        self.assertEqual(nd.to_traditional("“这里”"), "“这里”")

    @unittest.skipUnless(HAS_OPENCC, "未安裝 opencc")
    def test_fix_rules(self):
        nd.setup_converter("s2tw")
        cases = {
            "他們在戰斗。": "他們在戰鬥。",
            "腦海里一片空白": "腦海裡一片空白",
            "白發蒼蒼": "白髮蒼蒼",
            "通道里的人": "通道裡的人",
            "几乎": "幾乎",
            "聊天后，他走了": "聊天後，他走了",
            "那么什么": "那麼什麼",
        }
        for src, expected in cases.items():
            self.assertEqual(nd.to_traditional(src), expected, src)

    @unittest.skipUnless(HAS_OPENCC, "未安裝 opencc")
    def test_fix_rules_keep_correct_words(self):
        nd.setup_converter("s2tw")
        for text in ["歇斯底里", "諾里斯", "三公里", "皇后，請", "一齣好戲", "鄰里", "十海里"]:
            self.assertEqual(nd.to_traditional(text), text)


if __name__ == "__main__":
    unittest.main()
