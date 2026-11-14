"""下載流程測試：用假的 driver 模擬網站，測「中斷 → 續傳 → 輸出」與 EPUB、品質檢查。
不開瀏覽器、不連網。執行：python -m unittest discover -s tests -v
"""

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import novel_downloader as nd  # noqa: E402

SITE = "https://a.com"
N = 12  # 假網站的章數


def chapter_url(i: int) -> str:
    return f"{SITE}/b/1/{i}.html"


def chapter_page(i: int, last: int = N) -> str:
    text = "<br>".join(f"第{i}章的第{k}段內容，這是測試用的文字。" * 2 for k in range(5))
    nxt = f'<a href="/b/1/{i + 1}.html">下一章</a>' if i < last else '<a href="/b/1/">回目錄</a>'
    return (f"<html><head><title>第{i}章 標題{i}_測試書</title></head><body>"
            f"<h1>第{i}章 標題{i}</h1><div id='content'>{text}</div><div>{nxt}</div></body></html>")


class FakeDriver:
    """模擬 Selenium driver：execute_script（快速模式的「啟動下載 + 輪詢」）與 get/page_source（慢速、next 模式）。
    interrupt_after：第幾次呼叫之後丟出 KeyboardInterrupt，模擬使用者按 Ctrl+C。"""

    def __init__(self, pages: dict[str, str], interrupt_after: int | None = None):
        self.pages = pages
        self.interrupt_after = interrupt_after
        self.calls = 0
        self.fetched: list[str] = []
        self.current_url = ""

    def _tick(self):
        self.calls += 1
        if self.interrupt_after is not None and self.calls > self.interrupt_after:
            raise KeyboardInterrupt

    def results(self, urls):
        return [self.pages.get(u) for u in urls]

    def execute_script(self, js, *args):
        if js is nd._FETCH_START_JS:
            self._tick()
            urls = args[0]
            self.fetched += urls
            self.jobs = getattr(self, "jobs", {})
            job_id = f"job{len(self.jobs)}"
            self.jobs[job_id] = self.results(urls)
            return job_id
        if js is nd._POLL_JS:
            return {"done": True, "out": self.jobs.pop(args[0])}
        raise AssertionError("未預期的 execute_script 呼叫")

    def get(self, url):
        self._tick()
        self.fetched.append(url)
        self.current_url = url

    @property
    def page_source(self):
        return self.pages.get(self.current_url, "<html><body></body></html>")


def make_args(tmp: str, **kw) -> argparse.Namespace:
    args = dict(url=f"{SITE}/b/1/", start=1, end=0, refresh=False, slow=False, delay=0, batch_size=5,
                format="txt", max_pages=1000, state={"c": 4, "rate": 0})
    args.update(kw)
    return argparse.Namespace(**args)


class FlowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "測試書_測試站")
        self.pages = {chapter_url(i): chapter_page(i) for i in range(1, N + 1)}
        self.chapters = [(f"第{i}章 標題{i}", chapter_url(i)) for i in range(1, N + 1)]
        self.meta = {"book": "測試書", "author": "某作者", "source": f"{SITE}/b/1/"}
        nd.CONVERTER.update(enabled=False)
        self.old_rates = nd.SITE_RATES_FILE
        self.old_poll, nd.POLL_INTERVAL = nd.POLL_INTERVAL, 0
        nd.SITE_RATES_FILE = os.path.join(self.tmp.name, "rates.json")
        self.quiet = contextlib.redirect_stdout(io.StringIO())  # 不顯示下載進度訊息
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        nd.SITE_RATES_FILE = self.old_rates
        nd.POLL_INTERVAL = self.old_poll
        self.tmp.cleanup()

    def titles_in_txt(self) -> list[str]:
        with open(self.base + ".txt", encoding="utf-8") as f:
            return [line for line in f.read().splitlines() if line.startswith("第") and "段" not in line]


class TestTocFlow(FlowTestCase):
    def test_interrupt_and_resume_fast(self):
        # 第一次：批次大小 5，第 2 批時中斷 → 只有第 1 批（5 章）存進暫存
        d1 = FakeDriver(self.pages, interrupt_after=1)
        nd.download_toc_book(d1, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(len(self.titles_in_txt()), 5)

        # 第二次：只抓剩下的 7 章，輸出完整 12 章
        d2 = FakeDriver(self.pages)
        nd.download_toc_book(d2, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(sorted(d2.fetched), sorted(chapter_url(i) for i in range(6, N + 1)))
        self.assertEqual(self.titles_in_txt(), [t for t, _ in self.chapters])

        # 第三次：全部都下載過，不應再抓任何頁面
        d3 = FakeDriver(self.pages)
        nd.download_toc_book(d3, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(d3.fetched, [])

    def test_refresh_range(self):
        nd.download_toc_book(FakeDriver(self.pages), make_args(self.tmp.name), self.chapters, self.base, self.meta)
        d = FakeDriver(self.pages)
        nd.download_toc_book(d, make_args(self.tmp.name, start=3, end=4, refresh=True),
                             self.chapters, self.base, self.meta)
        self.assertEqual(sorted(d.fetched), [chapter_url(3), chapter_url(4)])
        self.assertEqual(len(self.titles_in_txt()), N)

    def test_slow_mode_resume(self):
        d1 = FakeDriver(self.pages, interrupt_after=3)
        nd.download_toc_book(d1, make_args(self.tmp.name, slow=True), self.chapters, self.base, self.meta)
        self.assertEqual(len(self.titles_in_txt()), 3)
        d2 = FakeDriver(self.pages)
        nd.download_toc_book(d2, make_args(self.tmp.name, slow=True), self.chapters, self.base, self.meta)
        self.assertEqual(d2.fetched, [chapter_url(i) for i in range(4, N + 1)])
        self.assertEqual(len(self.titles_in_txt()), N)

    def test_failed_pages_fall_back_to_browser(self):
        # 快速模式抓不到（例如 JS 動態內容）的頁面，改用瀏覽器開啟
        class PartialDriver(FakeDriver):
            def results(self, urls):
                return [None if u == chapter_url(2) else self.pages.get(u) for u in urls]
        d = PartialDriver(self.pages)
        nd.download_toc_book(d, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(len(self.titles_in_txt()), N)

    def test_backup_old_txt_without_cache(self):
        with open(self.base + ".txt", "w", encoding="utf-8") as f:
            f.write("舊版下載的內容")
        nd.download_toc_book(FakeDriver(self.pages), make_args(self.tmp.name, start=10),
                             self.chapters, self.base, self.meta)
        with open(self.base + ".bak.txt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "舊版下載的內容")
        self.assertEqual(len(self.titles_in_txt()), 3)

    def test_both_formats(self):
        nd.download_toc_book(FakeDriver(self.pages), make_args(self.tmp.name, format="both"),
                             self.chapters, self.base, self.meta)
        self.assertTrue(os.path.exists(self.base + ".txt"))
        self.assertTrue(os.path.exists(self.base + ".epub"))


class TestNextFlow(FlowTestCase):
    def test_interrupt_and_resume_next(self):
        start = chapter_url(1)
        d1 = FakeDriver(self.pages, interrupt_after=4)
        nd.download_next_book(d1, make_args(self.tmp.name, url=start), self.base, self.meta)
        self.assertEqual(len(self.titles_in_txt()), 4)

        # 續傳：前 4 章沿用暫存（不重新下載），從第 5 章接著抓到最後
        d2 = FakeDriver(self.pages)
        nd.download_next_book(d2, make_args(self.tmp.name, url=start), self.base, self.meta)
        self.assertEqual(d2.fetched, [chapter_url(i) for i in range(5, N + 1)])
        self.assertEqual(len(self.titles_in_txt()), N)

        # 連載更新：網站多了第 13 章。只會重抓最後一章（找新的下一章連結）和新章節
        self.pages[chapter_url(N)] = chapter_page(N, last=N + 1)
        self.pages[chapter_url(N + 1)] = chapter_page(N + 1, last=N + 1)
        d3 = FakeDriver(self.pages)
        nd.download_next_book(d3, make_args(self.tmp.name, url=start), self.base, self.meta)
        self.assertEqual(d3.fetched, [chapter_url(N), chapter_url(N + 1)])
        self.assertEqual(len(self.titles_in_txt()), N + 1)


class TestEpub(unittest.TestCase):
    def test_structure(self):
        items = [("第1章 開始", "第一段 <tag> & 符號\n第二段"), ("第2章 結束", "最後一段")]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "書.epub")
            nd.write_epub(path, items, "書名 & <測試>", "作者", "https://a.com/b/1/")
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                self.assertEqual(infos[0].filename, "mimetype")
                self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
                self.assertEqual(z.read("mimetype"), b"application/epub+zip")
                names = z.namelist()
                for name in ["META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml",
                             "OEBPS/toc.ncx", "OEBPS/c00001.xhtml", "OEBPS/c00002.xhtml"]:
                    self.assertIn(name, names)
                for name in names:  # 所有 XML 都要合法
                    if name.endswith((".xml", ".opf", ".xhtml", ".ncx")):
                        ET.fromstring(z.read(name))
                ch1 = z.read("OEBPS/c00001.xhtml").decode("utf-8")
                self.assertIn("<p>第一段 &lt;tag&gt; &amp; 符號</p>", ch1)
                self.assertIn("<p>第二段</p>", ch1)
                opf = z.read("OEBPS/content.opf").decode("utf-8")
                self.assertIn("書名 &amp; &lt;測試&gt;", opf)
                self.assertIn("<dc:creator>作者</dc:creator>", opf)


class TestChapterNumber(unittest.TestCase):
    def test_numbers(self):
        cases = {"第123章 標題": 123, "第一百二十三章": 123, "第十五章": 15, "第十章": 10,
                 "第二十回": 20, "第一千零一章": 1001, "第１２章": 12, "第一二三章": 123,
                 "番外 一": None, "第3頁": None, "第一卷總結": None, "第兩萬零五章": 20005}
        for text, expected in cases.items():
            self.assertEqual(nd.chapter_number(text), expected, text)

    def test_title_mismatch_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = nd.ChapterCache(os.path.join(tmp, "x.jsonl"))
            chapters = [(f"第{i}章", f"u{i}") for i in range(1, 5)]
            for i in range(1, 5):
                page_title = "第9章 錯的" if i == 3 else f"第{i}章 標題"
                c.put(f"u{i}", f"第{i}章", f"第{i}章獨特的開頭，" + "內容" * 300, page_title=page_title)
            c.close()
            warnings = nd.check_quality(chapters, c)
            self.assertEqual(len(warnings), 1)
            self.assertIn("第 3 章", warnings[0])
            self.assertIn("第9章 錯的", warnings[0])


class TestOutputBase(unittest.TestCase):
    def test_paths(self):
        ns = argparse.Namespace
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(nd.output_base(ns(output=os.path.join(tmp, "a.txt")), "書", "站"), os.path.join(tmp, "a"))
            self.assertEqual(nd.output_base(ns(output=os.path.join(tmp, "a.EPUB")), "書", "站"), os.path.join(tmp, "a"))
            self.assertEqual(nd.output_base(ns(output=os.path.join(tmp, "Mr.Wang")), "書", "站"),
                             os.path.join(tmp, "Mr.Wang"))
        self.assertEqual(nd.output_base(ns(output=None), "書", "站"), os.path.join(nd.DOWNLOAD_DIR, "書_站"))


if __name__ == "__main__":
    unittest.main()
