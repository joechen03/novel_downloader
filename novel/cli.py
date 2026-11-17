"""命令列：參數解析、一次下載多本、--update-all、單本書的處理流程。"""

import argparse
import os
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .browser import BlockedError, WINDOW, acquire_lock, build_driver, load_soup, release_lock
from .config import CACHE_DIRNAME, CHROME_PROFILE, DOWNLOAD_DIR, MIN_TOC_LINKS
from .converter import setup_converter, to_traditional
from .downloader import download_cover, download_next_book, download_toc_book
from .exporter import FORMATS
from .fetcher import load_site_rates
from .storage import BOOK_INFO_KEYS, load_book_infos, safe_filename, save_book_info
from .toc import (FEW_CHAPTERS, collect_paginated_toc, expand_toc, extract_author, extract_book_name,
                  extract_chapter_links, extract_site_name, extract_volumes, find_cover_url, find_toc_link)


def output_base(args, book: str, site: str) -> str:
    """輸出路徑（不含副檔名）。-o 有給 .txt / .epub 副檔名時去掉；沒給 -o 時放在 downloads/。"""
    if args.output:
        root, ext = os.path.splitext(args.output)
        base = root if ext.lower() in (".txt", ".epub") else args.output
    else:
        base = os.path.join(DOWNLOAD_DIR, safe_filename(f"{book}_{site}"))
    folder = os.path.dirname(os.path.abspath(base))
    os.makedirs(folder, exist_ok=True)
    return base


def process_book(driver, args) -> str:
    """下載一本書：解析目錄 → 下載 → 輸出。回傳 run_and_export 的狀態（--list 時為 "done"）。"""
    soup = load_soup(driver, args.url)
    first_soup = soup
    raw_book = extract_book_name(soup)
    book = to_traditional(raw_book)
    site = to_traditional(extract_site_name(soup, args.url))
    meta = {"book": book, "author": to_traditional(extract_author(soup)), "source": args.url,
            "clean": not args.keep_repeated}
    base = output_base(args, book, site)
    outputs = "、".join(f"{base}.{ext}" for ext in FORMATS[args.format])

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
    # 書籍介紹頁只列出最新幾章：有「目錄」連結時改開目錄頁，章節比較多才採用
    if args.mode != "next" and len(chapters) < FEW_CHAPTERS and (link := find_toc_link(soup, toc_url)):
        toc_soup = load_soup(driver, link)
        found = extract_chapter_links(toc_soup, driver.current_url)
        if len(found) > len(chapters):
            print(f"已開啟目錄頁：{driver.current_url}")
            soup, toc_url, chapters = toc_soup, driver.current_url, found
    if chapters:
        chapters = collect_paginated_toc(driver, soup, toc_url, chapters, args.state)
    if args.reverse:
        chapters.reverse()

    if args.mode == "toc" or len(chapters) >= MIN_TOC_LINKS:
        print(f"書名：{book}，共 {len(chapters)} 章 → {outputs}")
        meta["volumes"] = extract_volumes(soup, toc_url, chapters)
        if meta["volumes"]:
            print(f"分卷：{len(set(meta['volumes'].values()))} 卷（EPUB 目錄會依卷分層）")
        if args.list:
            for i, (title, url) in enumerate(chapters, 1):
                print(f"{i:>5}. {title}  {url}")
            return "done"
        save_book_info(base, args)
        if "epub" in FORMATS[args.format]:
            cover = find_cover_url(first_soup, args.url, raw_book) or find_cover_url(soup, toc_url, raw_book)
            if cover:
                download_cover(driver, cover, base)
        return download_toc_book(driver, args, chapters, base, meta)
    print(f"未偵測到目錄，改用「下一章」模式 → {outputs}")
    if args.list:
        return "done"
    save_book_info(base, args)
    return download_next_book(driver, args, base, meta)


def read_url_file(path: str) -> list[str]:
    """一行一個網址；空行與 # 開頭的行略過。"""
    with open(path, encoding="utf-8-sig") as f:
        return [s.strip() for s in f if s.strip() and not s.strip().startswith("#")]


def build_jobs(args) -> list[argparse.Namespace]:
    """把命令列（網址、--input 清單、--update-all）展開成每本書一組參數。"""
    jobs = []
    for url in args.urls:
        jobs.append(argparse.Namespace(**{**vars(args), "url": url}))
    if args.update_all:
        infos = load_book_infos(args.update_all)
        if not infos:
            print(f"⚠ 「{os.path.join(args.update_all, CACHE_DIRNAME)}」中沒有書籍資訊。"
                  "這個功能只認得之後用新版下載過的書，請先用網址下載一次。")
        for info in infos:
            # 更新時下載所有還沒下載的章節，不沿用上次的 --start / --end
            jobs.append(argparse.Namespace(**{**vars(args), **{k: info[k] for k in BOOK_INFO_KEYS if k in info},
                                              "output": info["output"], "start": 1, "end": 0}))
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(description="通用小說下載器")
    parser.add_argument("urls", nargs="*", metavar="url",
                        help="目錄頁網址，可以一次給多個（--mode next 時為起始章節網址）")
    parser.add_argument("-i", "--input", help="從文字檔讀入網址清單（一行一個，# 開頭為註解），依序下載")
    parser.add_argument("--update-all", nargs="?", const=DOWNLOAD_DIR, metavar="DIR",
                        help="更新資料夾（預設 downloads/）中所有下載過的書，只下載新章節")
    parser.add_argument("-o", "--output",
                        help="輸出檔名（預設為 downloads/書名_網站名；副檔名依 --format 自動加上）；只能用在單本書")
    parser.add_argument("-f", "--format", choices=list(FORMATS), default="txt",
                        help="輸出格式：txt（預設）、epub、both（兩種都輸出）")
    parser.add_argument("--mode", choices=["auto", "toc", "next"], default="auto",
                        help="auto：自動判斷；toc：解析目錄；next：一路點下一章")
    parser.add_argument("--start", type=int, default=1, help="從第幾章開始下載")
    parser.add_argument("--end", type=int, default=0, help="下載到第幾章（預設全部）")
    parser.add_argument("--refresh", action="store_true",
                        help="重新下載指定範圍內的章節（預設會略過暫存中已下載的章節）")
    parser.add_argument("--reverse", action="store_true", help="目錄為新→舊排列時使用")
    parser.add_argument("--list", action="store_true", help="只列出章節清單，不下載")
    parser.add_argument("--no-retry", action="store_true",
                        help="下載完不要自動重抓品質檢查標出的可疑章節")
    parser.add_argument("--keep-repeated", action="store_true",
                        help="不要自動刪除每章都出現的雜訊行（網站宣傳文字、「(本章完)」等）")
    parser.add_argument("--max-pages", type=int, default=100000, help="next 模式最多抓幾章")
    parser.add_argument("--delay", type=float, default=0.5, help="慢速/next 模式每章間隔秒數")
    parser.add_argument("--slow", action="store_true", help="改用瀏覽器逐章開啟（JS 動態網站用）")
    parser.add_argument("-c", "--concurrency", type=int, default=8, help="快速模式同時下載數（太高會觸發網站人機驗證）")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="快速模式每批最多幾章（每批完成就存進暫存，中斷時最多損失一批）")
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
    if args.input:
        try:
            args.urls += read_url_file(args.input)
        except OSError as e:
            parser.error(f"無法讀取網址清單：{e}")
    if not args.urls and not args.update_all:
        parser.error("請給目錄頁網址，或使用 --input / --update-all")
    jobs = build_jobs(args)
    if not jobs:
        return
    if args.output and len(jobs) > 1:
        parser.error("-o 只能用在單本書；一次下載多本時檔名會自動取為「書名_網站名」")
    setup_converter(args.convert)

    lock = None
    if args.profile:
        lock = acquire_lock(args.profile)
        if lock is None:
            print(f"✗ 另一個下載器正在使用 Chrome 設定檔「{args.profile}」，請等它結束後再執行；"
                  "一定要同時執行的話，請用 --profile 指定另一個資料夾。")
            return
    try:
        try:
            driver = build_driver(args.profile, args.headless)
        except RuntimeError as e:
            print(f"✗ {e}")
            return
        if not args.headless and not args.show:
            driver.minimize_window()
            WINDOW["minimized"] = True
        results = []
        try:
            for n, job in enumerate(jobs, 1):
                if len(jobs) > 1:
                    print(f"\n===== [{n}/{len(jobs)}] {job.url} =====")
                try:
                    status = process_book(driver, job)
                except BlockedError as e:
                    print(f"\n✗ {e}")
                    if job.headless:
                        print("  這個網站不接受無頭（headless）瀏覽器，請拿掉 --headless 再試一次。")
                    status = "error"
                except RuntimeError as e:
                    print(f"\n✗ {e}")
                    status = "error"
                results.append((job.url, status))
                if status == "interrupted":
                    break
        except KeyboardInterrupt:
            print("\n使用者中斷。")
        finally:
            driver.quit()
        if len(jobs) > 1:
            names = {"done": "完成", "interrupted": "中斷", "error": "失敗"}
            print(f"\n===== 共 {len(jobs)} 本：" + "、".join(
                f"{names[s]} {sum(1 for _, x in results if x == s)} 本" for s in names) + " =====")
            for url, status in results:
                if status != "done":
                    print(f"  {names[status]}：{url}")
            if len(results) < len(jobs):
                print(f"  未執行：{len(jobs) - len(results)} 本")
    finally:
        release_lock(lock)
    print("Program End!")


if __name__ == "__main__":
    main()
