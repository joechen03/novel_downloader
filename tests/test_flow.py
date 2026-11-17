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

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from novel import browser, cli, config, converter, downloader, exporter, fetcher, quality, storage, toc  # noqa: E402

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
        if js is fetcher._FETCH_START_JS:
            self._tick()
            urls = args[0]
            self.fetched += urls
            self.jobs = getattr(self, "jobs", {})
            job_id = f"job{len(self.jobs)}"
            self.jobs[job_id] = self.results(urls)
            return job_id
        if js is fetcher._POLL_JS:
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
        converter.CONVERTER.update(enabled=False)
        self.old_rates = fetcher.SITE_RATES_FILE
        self.old_poll, fetcher.POLL_INTERVAL = fetcher.POLL_INTERVAL, 0
        fetcher.SITE_RATES_FILE = os.path.join(self.tmp.name, "rates.json")
        self.quiet = contextlib.redirect_stdout(io.StringIO())  # 不顯示下載進度訊息
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        fetcher.SITE_RATES_FILE = self.old_rates
        fetcher.POLL_INTERVAL = self.old_poll
        self.tmp.cleanup()

    def titles_in_txt(self) -> list[str]:
        with open(self.base + ".txt", encoding="utf-8") as f:
            return [line for line in f.read().splitlines() if line.startswith("第") and "段" not in line]


class TestTocFlow(FlowTestCase):
    def test_interrupt_and_resume_fast(self):
        # 第一次：批次大小 5，第 2 批時中斷 → 只有第 1 批（5 章）存進暫存
        d1 = FakeDriver(self.pages, interrupt_after=1)
        downloader.download_toc_book(d1, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(len(self.titles_in_txt()), 5)

        # 第二次：只抓剩下的 7 章，輸出完整 12 章
        d2 = FakeDriver(self.pages)
        downloader.download_toc_book(d2, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(sorted(d2.fetched), sorted(chapter_url(i) for i in range(6, N + 1)))
        self.assertEqual(self.titles_in_txt(), [t for t, _ in self.chapters])

        # 第三次：全部都下載過，不應再抓任何頁面
        d3 = FakeDriver(self.pages)
        downloader.download_toc_book(d3, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(d3.fetched, [])

    def test_refresh_range(self):
        downloader.download_toc_book(FakeDriver(self.pages), make_args(self.tmp.name), self.chapters, self.base, self.meta)
        d = FakeDriver(self.pages)
        downloader.download_toc_book(d, make_args(self.tmp.name, start=3, end=4, refresh=True),
                             self.chapters, self.base, self.meta)
        self.assertEqual(sorted(d.fetched), [chapter_url(3), chapter_url(4)])
        self.assertEqual(len(self.titles_in_txt()), N)

    def test_slow_mode_resume(self):
        d1 = FakeDriver(self.pages, interrupt_after=3)
        downloader.download_toc_book(d1, make_args(self.tmp.name, slow=True), self.chapters, self.base, self.meta)
        self.assertEqual(len(self.titles_in_txt()), 3)
        d2 = FakeDriver(self.pages)
        downloader.download_toc_book(d2, make_args(self.tmp.name, slow=True), self.chapters, self.base, self.meta)
        self.assertEqual(d2.fetched, [chapter_url(i) for i in range(4, N + 1)])
        self.assertEqual(len(self.titles_in_txt()), N)

    def test_failed_pages_fall_back_to_browser(self):
        # 快速模式抓不到（例如 JS 動態內容）的頁面，改用瀏覽器開啟
        class PartialDriver(FakeDriver):
            def results(self, urls):
                return [None if u == chapter_url(2) else self.pages.get(u) for u in urls]
        d = PartialDriver(self.pages)
        downloader.download_toc_book(d, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(len(self.titles_in_txt()), N)

    def test_backup_old_txt_without_cache(self):
        with open(self.base + ".txt", "w", encoding="utf-8") as f:
            f.write("舊版下載的內容")
        downloader.download_toc_book(FakeDriver(self.pages), make_args(self.tmp.name, start=10),
                             self.chapters, self.base, self.meta)
        with open(self.base + ".bak.txt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "舊版下載的內容")
        self.assertEqual(len(self.titles_in_txt()), 3)

    def test_both_formats(self):
        downloader.download_toc_book(FakeDriver(self.pages), make_args(self.tmp.name, format="both"),
                             self.chapters, self.base, self.meta)
        self.assertTrue(os.path.exists(self.base + ".txt"))
        self.assertTrue(os.path.exists(self.base + ".epub"))


class TestNextFlow(FlowTestCase):
    def test_interrupt_and_resume_next(self):
        start = chapter_url(1)
        d1 = FakeDriver(self.pages, interrupt_after=4)
        downloader.download_next_book(d1, make_args(self.tmp.name, url=start), self.base, self.meta)
        self.assertEqual(len(self.titles_in_txt()), 4)

        # 續傳：前 4 章沿用暫存（不重新下載），從第 5 章接著抓到最後
        d2 = FakeDriver(self.pages)
        downloader.download_next_book(d2, make_args(self.tmp.name, url=start), self.base, self.meta)
        self.assertEqual(d2.fetched, [chapter_url(i) for i in range(5, N + 1)])
        self.assertEqual(len(self.titles_in_txt()), N)

        # 連載更新：網站多了第 13 章。只會重抓最後一章（找新的下一章連結）和新章節
        self.pages[chapter_url(N)] = chapter_page(N, last=N + 1)
        self.pages[chapter_url(N + 1)] = chapter_page(N + 1, last=N + 1)
        d3 = FakeDriver(self.pages)
        downloader.download_next_book(d3, make_args(self.tmp.name, url=start), self.base, self.meta)
        self.assertEqual(d3.fetched, [chapter_url(N), chapter_url(N + 1)])
        self.assertEqual(len(self.titles_in_txt()), N + 1)


class TestEpub(unittest.TestCase):
    def test_structure(self):
        items = [("第1章 開始", "第一段 <tag> & 符號\n第二段"), ("第2章 結束", "最後一段")]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "書.epub")
            exporter.write_epub(path, items, "書名 & <測試>", "作者", "https://a.com/b/1/")
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


def check_epub_consistency(test: unittest.TestCase, z: zipfile.ZipFile) -> None:
    """EPUB 內部參照的一致性（epubcheck 最常報的錯誤）：manifest 與實際檔案相符、spine/目錄連結都存在、
    id 不重複、同一目標的 playOrder 相同。"""
    opf_ns = {"o": "http://www.idpf.org/2007/opf"}
    names = set(z.namelist())
    opf = ET.fromstring(z.read("OEBPS/content.opf"))
    items = opf.findall("o:manifest/o:item", opf_ns)
    ids = [i.get("id") for i in items]
    test.assertEqual(len(ids), len(set(ids)), "manifest id 重複")
    hrefs = {f"OEBPS/{i.get('href')}" for i in items}
    test.assertEqual(hrefs, names - {"mimetype", "META-INF/container.xml", "OEBPS/content.opf"},
                     "manifest 與 zip 中的檔案不一致")
    for ref in opf.findall("o:spine/o:itemref", opf_ns):
        test.assertIn(ref.get("idref"), ids)
    test.assertEqual(sum(1 for i in items if "nav" in (i.get("properties") or "")), 1)

    nav = ET.fromstring(z.read("OEBPS/nav.xhtml"))
    for a in nav.iter("{http://www.w3.org/1999/xhtml}a"):
        test.assertIn(f"OEBPS/{a.get('href')}", names)
    ncx_ns = "{http://www.daisy.org/z3986/2005/ncx/}"
    order: dict[str, str] = {}
    point_ids = []
    for p in ET.fromstring(z.read("OEBPS/toc.ncx")).iter(f"{ncx_ns}navPoint"):
        src = p.find(f"{ncx_ns}content").get("src")
        test.assertIn(f"OEBPS/{src}", names)
        test.assertEqual(order.setdefault(src, p.get("playOrder")), p.get("playOrder"), "同一目標的 playOrder 不同")
        point_ids.append(p.get("id"))
    test.assertEqual(len(point_ids), len(set(point_ids)), "navPoint id 重複")


PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082")


class TestEpubExtras(unittest.TestCase):
    def build(self, **kw):
        items = [(f"第{i}章", f"第{i}章內容") for i in range(1, 5)]
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        path = os.path.join(tmp, "書.epub")
        exporter.write_epub(path, items, "書", "作者", "https://a.com/b/1/", **kw)
        z = zipfile.ZipFile(path)
        self.addCleanup(z.close)
        for name in z.namelist():
            if name.endswith((".xml", ".opf", ".xhtml", ".ncx")):
                ET.fromstring(z.read(name))
        check_epub_consistency(self, z)
        return z

    def test_plain(self):
        self.build()

    def test_volumes_and_cover(self):
        self.build(volumes=["第一卷", "第一卷", "第二卷", ""], cover=PNG_1PX)

    def test_volumes(self):
        z = self.build(volumes=["", "第一卷 啟程", "第一卷 啟程", "第二卷 歸來"])
        nav = z.read("OEBPS/nav.xhtml").decode("utf-8")
        # 序章不屬於任何卷，直接放在第一層；兩卷各自一層
        self.assertIn('<li><a href="c00001.xhtml">第1章</a></li>', nav)
        self.assertIn('<li><a href="c00002.xhtml">第一卷 啟程</a>\n<ol>', nav)
        self.assertEqual(nav.count("<ol>"), 3)
        ncx = ET.fromstring(z.read("OEBPS/toc.ncx"))
        ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
        top = ncx.findall("n:navMap/n:navPoint", ns)
        self.assertEqual(len(top), 3)
        self.assertEqual(len(top[1].findall("n:navPoint", ns)), 2)
        # 卷與它的第一章指向同一個檔案，playOrder 必須相同
        self.assertEqual(top[1].get("playOrder"), top[1].find("n:navPoint", ns).get("playOrder"))

    def test_cover(self):
        z = self.build(cover=PNG_1PX)
        self.assertIn("OEBPS/cover.png", z.namelist())
        self.assertIn("OEBPS/cover.xhtml", z.namelist())
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        self.assertIn('properties="cover-image"', opf)
        self.assertIn('<meta name="cover" content="cover-img"/>', opf)
        self.assertLess(opf.index('idref="cover"'), opf.index('idref="c1"'))  # 封面是第一頁

    def test_invalid_cover_ignored(self):
        z = self.build(cover=b"<html>not an image</html>")
        self.assertFalse(any("cover" in n for n in z.namelist()))

    def test_image_type(self):
        self.assertEqual(storage.image_type(PNG_1PX), ("png", "image/png"))
        self.assertEqual(storage.image_type(b"\xff\xd8\xff\xe0xxxx"), ("jpg", "image/jpeg"))
        self.assertEqual(storage.image_type(b"RIFF\0\0\0\0WEBPVP8 "), ("webp", "image/webp"))
        self.assertIsNone(storage.image_type(b"RIFF\0\0\0\0WAVEfmt "))


class TestRepeatedLines(FlowTestCase):
    def test_removed_from_output(self):
        pages = {u: html.replace("</div><div>", "<br>(本章完)<br>本站網址：測試站點</div><div>")
                 for u, html in self.pages.items()}
        downloader.download_toc_book(FakeDriver(pages), make_args(self.tmp.name), self.chapters, self.base, self.meta)
        with open(self.base + ".txt", encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("(本章完)", text)
        self.assertNotIn("本站網址", text)
        self.assertIn("第3章的第2段內容", text)

        # --keep-repeated：保留
        downloader.download_toc_book(FakeDriver(pages), make_args(self.tmp.name), self.chapters, self.base,
                             {**self.meta, "clean": False})
        with open(self.base + ".txt", encoding="utf-8") as f:
            self.assertEqual(f.read().count("(本章完)"), N)

    def test_threshold(self):
        common = "這一行在一半以上的章節出現"
        rare = "這一行只在少數章節出現"
        contents = [f"正文{i}\n{common if i < 6 else ''}\n{rare if i < 2 else ''}\n嗯。" for i in range(10)]
        self.assertEqual(exporter.find_repeated_lines(contents), {common})  # 「嗯。」太短，不處理
        self.assertEqual(exporter.find_repeated_lines(contents[:4]), set())  # 章數太少不處理


class TestVolumes(unittest.TestCase):
    def test_toc_headings(self):
        html = ("<html><body><div class='latest'><a href='/b/1/5.html'>第5章</a></div><dl>"
                "<dt>《書》作品相關</dt><dd><a href='/b/1/0.html'>序</a></dd>"
                "<dt>第一卷 少年</dt>" + "".join(f"<dd><a href='/b/1/{i}.html'>第{i}章</a></dd>" for i in (1, 2, 3))
                + "<dt>第二卷 青年</dt>" + "".join(f"<dd><a href='/b/1/{i}.html'>第{i}章</a></dd>" for i in (4, 5))
                + "</dl></body></html>")
        base = "https://a.com/b/1/"
        chapters = toc.extract_chapter_links(BeautifulSoup(html, "lxml"), base)
        vols = toc.extract_volumes(BeautifulSoup(html, "lxml"), base, chapters)
        self.assertEqual(vols[f"{base}0.html"], "《書》作品相關")
        self.assertEqual(vols[f"{base}3.html"], "第一卷 少年")
        self.assertEqual(vols[f"{base}5.html"], "第二卷 青年")  # 「最新章節」區塊不影響

    def test_titles(self):
        chapters = [("第一卷 啟程 第1章 出發", "u1"), ("第一卷 啟程 第2章 路上", "u2"),
                    ("第二卷 第3章 回家", "u3"), ("番外", "u4")]
        vols = toc.extract_volumes(BeautifulSoup("<html><body></body></html>", "lxml"), "https://a.com/", chapters)
        self.assertEqual(vols, {"u1": "第一卷 啟程", "u2": "第一卷 啟程", "u3": "第二卷"})

    def test_no_volumes(self):
        chapters = [("第一卷總結兼請假", "u1"), ("第1章", "u2")]
        html = "<html><body><p>本書共分三卷</p><a href='u2'>第1章</a></body></html>"
        self.assertEqual(toc.extract_volumes(BeautifulSoup(html, "lxml"), "https://a.com/", chapters), {})


class TestCover(unittest.TestCase):
    def test_find_cover_url(self):
        base = "https://a.com/b/1/"
        s = BeautifulSoup('<html><head><meta property="og:image" content="/img/1.jpg"></head></html>', "lxml")
        self.assertEqual(toc.find_cover_url(s, base), "https://a.com/img/1.jpg")
        s = BeautifulSoup('<img src="/logo.png"><img src="/x/123.jpg" alt="測試書">', "lxml")
        self.assertEqual(toc.find_cover_url(s, base, "測試書"), "https://a.com/x/123.jpg")
        s = BeautifulSoup('<img src="/logo.png"><img src="/files/nocover.jpg" alt="測試書">', "lxml")
        self.assertIsNone(toc.find_cover_url(s, base, "測試書"))


class TestJobs(unittest.TestCase):
    def test_book_info_and_update_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "書_站")
            args = make_args(tmp, url="https://a.com/b/1/", format="epub", mode="auto", reverse=True, slow=False)
            storage.save_book_info(base, args)
            infos = storage.load_book_infos(tmp)
            self.assertEqual(len(infos), 1)
            self.assertEqual(infos[0]["output"], os.path.abspath(base))

            cli_args = argparse.Namespace(urls=["https://b.com/x/"], update_all=tmp, format="txt", mode="auto",
                                     reverse=False, slow=False, output=None, start=5, end=10)
            jobs = cli.build_jobs(cli_args)
            self.assertEqual([j.url for j in jobs], ["https://b.com/x/", "https://a.com/b/1/"])
            self.assertEqual((jobs[0].format, jobs[0].start), ("txt", 5))
            # 更新時沿用該書的設定，並下載全部章節
            self.assertEqual((jobs[1].format, jobs[1].reverse, jobs[1].start, jobs[1].end), ("epub", True, 1, 0))
            self.assertEqual(jobs[1].output, os.path.abspath(base))

    def test_url_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "list.txt")
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write("# 我的書單\nhttps://a.com/1/\n\n  https://b.com/2/  \n")
            self.assertEqual(cli.read_url_file(path), ["https://a.com/1/", "https://b.com/2/"])

    def test_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = browser.acquire_lock(tmp)
            self.assertIsNotNone(first)
            self.assertIsNone(browser.acquire_lock(tmp))  # 第二個程式拿不到鎖
            browser.release_lock(first)
            again = browser.acquire_lock(tmp)
            self.assertIsNotNone(again)
            browser.release_lock(again)

    def test_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages = {chapter_url(i): chapter_page(i) for i in range(1, N + 1)}
            chapters = [(f"第{i}章", chapter_url(i)) for i in range(1, N + 1)]
            base = os.path.join(tmp, "b")
            old_rates, fetcher.SITE_RATES_FILE = fetcher.SITE_RATES_FILE, os.path.join(tmp, "r.json")
            old_poll, fetcher.POLL_INTERVAL = fetcher.POLL_INTERVAL, 0
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    s1 = downloader.download_toc_book(FakeDriver(pages, interrupt_after=1), make_args(tmp), chapters, base, {})
                    s2 = downloader.download_toc_book(FakeDriver(pages), make_args(tmp), chapters, base, {})
            finally:
                fetcher.SITE_RATES_FILE, fetcher.POLL_INTERVAL = old_rates, old_poll
            self.assertEqual((s1, s2), ("interrupted", "done"))


class TestRetrySuspects(FlowTestCase):
    def flaky_driver(self, times: int):
        """第 3 章前 times 次都回傳第 1 章的內容（模擬網站暫時出錯或本身就錯）。"""
        pages, wrong = self.pages, self.pages[chapter_url(1)]

        class Flaky(FakeDriver):
            left = times

            def results(self, urls):
                out = []
                for u in urls:
                    if u == chapter_url(3) and Flaky.left > 0:
                        Flaky.left -= 1
                        out.append(wrong)
                    else:
                        out.append(pages.get(u))
                return out
        return Flaky(pages)

    def text(self) -> str:
        with open(self.base + ".txt", encoding="utf-8") as f:
            return f.read()

    def test_transient_error_fixed(self):
        d = self.flaky_driver(1)
        status = downloader.download_toc_book(d, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(status, "done")
        self.assertEqual(d.fetched.count(chapter_url(3)), 2)  # 第一次抓錯，自動重抓一次
        self.assertIn("第3章的第0段", self.text())
        self.assertEqual(self.text().count("第1章的第0段"), 2)  # 每段重複兩次 → 只有第 1 章本身

    def test_site_error_retried_only_once(self):
        d1 = self.flaky_driver(100)  # 網站本身就錯：重抓也一樣
        downloader.download_toc_book(d1, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(d1.fetched.count(chapter_url(3)), 2)
        cache = storage.ChapterCache(storage.cache_path(self.base))
        self.assertTrue(cache.meta(chapter_url(3)).get("retried"))
        # 再執行一次：已經重抓過，不再自動重抓
        d2 = self.flaky_driver(100)
        downloader.download_toc_book(d2, make_args(self.tmp.name), self.chapters, self.base, self.meta)
        self.assertEqual(d2.fetched, [])
        # --refresh 重新下載後標記消失（網站修好了 → 內容正確、不再可疑）
        d3 = self.flaky_driver(0)
        downloader.download_toc_book(d3, make_args(self.tmp.name, start=3, end=3, refresh=True),
                                     self.chapters, self.base, self.meta)
        cache = storage.ChapterCache(storage.cache_path(self.base))
        self.assertFalse(cache.meta(chapter_url(3)).get("retried"))
        self.assertIn("第3章的第0段", self.text())

    def test_no_retry(self):
        d = self.flaky_driver(1)
        downloader.download_toc_book(d, make_args(self.tmp.name, no_retry=True), self.chapters, self.base, self.meta)
        self.assertEqual(d.fetched.count(chapter_url(3)), 1)
        self.assertNotIn("第3章的第0段", self.text())


class TestTocIndex(FlowTestCase):
    def test_collect_toc_index(self):
        # 目錄首頁只有「第1-5章」「第6-10章」「第11-12章」三個範圍連結，每頁開頭都有「最新章節」
        index = [("第1-5章", f"{SITE}/list/1/a"), ("第6-10章", f"{SITE}/list/1/b"), ("第11-12章", f"{SITE}/list/1/c")]
        latest = f"<div><a href='/b/1/{N}.html'>第{N}章 標題{N}</a></div>"
        ranges = {"a": range(1, 6), "b": range(6, 11), "c": range(11, N + 1)}
        pages = {f"{SITE}/list/1/{k}": "<html><body>" + latest + "<ul>" + "".join(
            f"<li><a href='/b/1/{i}.html'>第{i}章 標題{i}</a></li>" for i in r) + "</ul></body></html>"
            for k, r in ranges.items()}
        self.assertTrue(toc.is_toc_index(index))
        merged = toc.collect_paginated_toc(FakeDriver(pages), None, f"{SITE}/list/1/", index,
                                          {"c": 4, "rate": 0})
        self.assertEqual(merged, self.chapters)


class TestEta(unittest.TestCase):
    def test_eta(self):
        self.assertEqual(downloader.eta_text(1, 100, 1.0), "")  # 樣本太少
        self.assertEqual(downloader.eta_text(10, 100, 10.0), "，預計還要 1 分 30 秒")
        self.assertEqual(downloader.eta_text(100, 100, 10.0), "")
        self.assertEqual(downloader._duration(7300), "2 小時 1 分")


class TestChapterNumber(unittest.TestCase):
    def test_numbers(self):
        cases = {"第123章 標題": 123, "第一百二十三章": 123, "第十五章": 15, "第十章": 10,
                 "第二十回": 20, "第一千零一章": 1001, "第１２章": 12, "第一二三章": 123,
                 "番外 一": None, "第3頁": None, "第一卷總結": None, "第兩萬零五章": 20005}
        for text, expected in cases.items():
            self.assertEqual(quality.chapter_number(text), expected, text)

    def test_title_mismatch_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = storage.ChapterCache(os.path.join(tmp, "x.jsonl"))
            chapters = [(f"第{i}章", f"u{i}") for i in range(1, 5)]
            for i in range(1, 5):
                page_title = "第9章 錯的" if i == 3 else f"第{i}章 標題"
                c.put(f"u{i}", f"第{i}章", f"第{i}章獨特的開頭，" + "內容" * 300, page_title=page_title)
            c.close()
            warnings = quality.check_quality(chapters, c)
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0][0], 3)
            self.assertIn("第9章 錯的", warnings[0][1])


class TestOutputBase(unittest.TestCase):
    def test_paths(self):
        ns = argparse.Namespace
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.output_base(ns(output=os.path.join(tmp, "a.txt")), "書", "站"), os.path.join(tmp, "a"))
            self.assertEqual(cli.output_base(ns(output=os.path.join(tmp, "a.EPUB")), "書", "站"), os.path.join(tmp, "a"))
            self.assertEqual(cli.output_base(ns(output=os.path.join(tmp, "Mr.Wang")), "書", "站"),
                             os.path.join(tmp, "Mr.Wang"))
        self.assertEqual(cli.output_base(ns(output=None), "書", "站"), os.path.join(config.DOWNLOAD_DIR, "書_站"))


if __name__ == "__main__":
    unittest.main()
