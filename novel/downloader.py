"""下載流程：快速/慢速/next 模式、中斷續傳、輸出與收尾。"""

import os
import signal
import time
from urllib.parse import urlparse

from selenium.common.exceptions import WebDriverException

from .browser import BlockedError, load_soup
from .chapter import extract_title, fetch_chapter, find_link, parse_chapter_html
from .config import MAX_RETRY_SUSPECTS, MIN_CONTENT_LEN, NEXT_CHAPTER_TEXT
from .exporter import _ranges, export_book
from .fetcher import RATE_SAFETY, fetch_binary, fetch_many_with_verify, learned_rate, save_site_rate
from .quality import check_quality
from .storage import ChapterCache, cache_path, cover_path, image_type
from .toc import url_pattern


def download_by_toc(driver, chapters, indices, cache: ChapterCache, delay) -> None:
    """慢速模式：用瀏覽器逐章開啟（適用於需要執行 JS 才有內容的網站）。"""
    total = len(chapters)
    t0 = time.time()
    for done, i in enumerate(indices, 1):
        title, url = chapters[i]
        title, content, soup = fetch_chapter(driver, url, title)
        if content:
            cache.put(url, title, content, page_title=extract_title(soup))
        else:
            print(f"  ⚠ 第 {i + 1} 章未擷取到正文：{url}")
        print(f"[{i + 1}/{total}] {title}（{len(content)} 字）{eta_text(done, len(indices), time.time() - t0)}")
        time.sleep(delay)


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小時 {seconds % 3600 // 60} 分"


def eta_text(done: int, total: int, elapsed: float) -> str:
    """「，預計還要 3 分 20 秒」；剛開始（樣本太少）或已完成時回傳空字串。"""
    if done < 3 or done >= total or elapsed <= 0:
        return ""
    return f"，預計還要 {_duration(elapsed / done * (total - done))}"


def _worker_init() -> None:
    # 解析用的子進程忽略 Ctrl+C，交給主程式處理，避免每個子進程都印出一大串錯誤訊息
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _warmup(seconds: float) -> None:
    time.sleep(seconds)


def start_parser_pool():
    """建立解析用的進程池，並在「暫時忽略 Ctrl+C」的狀態下把所有子進程都先啟動。
    子進程會繼承忽略 Ctrl+C 的設定；否則在子進程啟動中（匯入模組時）按 Ctrl+C，
    _worker_init 還來不及執行，每個子進程都會印出一大串錯誤訊息。"""
    from concurrent.futures import ProcessPoolExecutor

    workers = min(os.cpu_count() or 2, 61)  # Windows 上限 61
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)  # 會被子進程繼承
    else:
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)       # SIG_IGN 會被子進程繼承
    try:
        pool = ProcessPoolExecutor(max_workers=workers, initializer=_worker_init)
        list(pool.map(_warmup, [0.2] * workers))  # 同時送出多個工作，逼它一次啟動所有子進程
    finally:
        if os.name == "nt":
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
        else:
            signal.signal(signal.SIGINT, old)
    return pool


def download_fast(driver, chapters, indices, cache: ChapterCache, state: dict, batch_size: int) -> None:
    """快速模式：每批章節抓完（含一章多頁、退回瀏覽器重抓）就存進暫存，中斷時最多只損失一批。
    限速時每批約 20 秒的量（最少 5 章、最多 batch_size 章），慢速網站中斷時也不會損失太多。"""
    t0 = time.time()
    pos = 0
    with start_parser_pool() as pool:
        while pos < len(indices):
            rate = state["rate"]
            size = batch_size if rate <= 0 else max(5, min(batch_size, int(rate * 20)))
            batch = indices[pos:pos + size]
            pos += len(batch)
            titles = {i: chapters[i][0] for i in batch}
            page_titles: dict[int, str] = {}
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
                    title, content, cont, page_title = fut.result()
                    titles[i] = title
                    page_titles.setdefault(i, page_title)
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
                        titles[i], content, soup = fetch_chapter(driver, url, chapters[i][0])
                        page_titles[i] = extract_title(soup)
                    except BlockedError:
                        raise
                    except RuntimeError as e:
                        print(f"  ⚠ 第 {i + 1} 章：{e}")
                        continue
                if content:
                    cache.put(url, titles[i], content, page_title=page_titles.get(i, ""))
                else:
                    print(f"  ⚠ 第 {i + 1} 章未擷取到正文：{url}")
            elapsed = time.time() - t0
            print(f"  已下載 {pos}/{len(indices)} 章（{elapsed:.1f}s{eta_text(pos, len(indices), elapsed)}）")


def download_by_next(driver, start_url, cache: ChapterCache, chain: list, max_pages: int,
                     delay: float, refresh: bool = False) -> None:
    """next 模式：一路跟隨「下一章」。抓到的章節依序加進 chain（呼叫端用來輸出）。
    暫存中有記錄下一章網址的章節直接沿用，不重新下載；最後一章（沒有下一章）會重新抓，以便發現新章節。"""
    pattern = url_pattern(start_url)
    url, reused = start_url, 0
    while url and len(chain) < max_pages:
        rec = cache.meta(url) if not refresh else {}
        if rec.get("next"):
            chain.append((rec["title"], url))
            reused += 1
            url = rec["next"]
            continue
        if reused:
            print(f"已有 {reused} 章下載過，自動略過；要重新下載請加 --refresh")
            reused = 0
        title, content, soup = fetch_chapter(driver, url)
        nxt = find_link(soup, url, NEXT_CHAPTER_TEXT)
        # 下一章連回目錄或格式不同的頁面，代表已到最後一章
        if not nxt or nxt == url or url_pattern(nxt) != pattern:
            nxt = None
        if content:
            cache.put(url, title, content, page_title=extract_title(soup), next=nxt)
        else:
            print(f"  ⚠ 未擷取到正文：{url}")
        chain.append((title, url))
        print(f"[{len(chain)}] {title}（{len(content)} 字）")
        if not nxt:
            print("已到最後一章。")
            return
        url = nxt
        time.sleep(delay)


def _run(job, what: str = "") -> str:
    """執行 job()，回傳 "done" / "interrupted" / "error"；BlockedError 往上拋。"""
    try:
        job()
        return "done"
    except KeyboardInterrupt:
        print(f"\n使用者中斷{what}。已下載的章節都已保存，重新執行同一指令即可接續。")
        return "interrupted"
    except BlockedError:
        raise
    except RuntimeError as e:
        print(f"\n✗ {e}\n已下載的章節都已保存，重新執行同一指令即可接續。")
        return "error"


def retry_suspects(warnings, chapters, cache: ChapterCache, retry) -> tuple[str, list[int], list[int]]:
    """把品質檢查標出的章節重新下載一次（retry(索引清單)）。重抓過的章節在暫存中記上 retried，
    之後不再自動重抓（例如網站本身就重複的章節，每次重抓也一樣）；--refresh 重新下載後標記會消失。
    回傳 (狀態, 內容有變的章節編號, 內容沒變的章節編號)。"""
    targets = sorted({n for n, _ in warnings if not cache.meta(chapters[n - 1][1]).get("retried")})
    if not targets:
        return "done", [], []
    if len(targets) > MAX_RETRY_SUSPECTS:
        print(f"\n可疑章節有 {len(targets)} 章，太多了，不自動重抓（可能是擷取規則不適用這個網站）")
        return "done", [], []
    print(f"\n品質檢查標出 {len(targets)} 個可疑章節，自動重新下載一次：{_ranges(targets)}")
    before = {n: cache.get(chapters[n - 1][1]) for n in targets}
    status = _run(lambda: retry([n - 1 for n in targets]), "重抓")
    cache.close()
    changed, same = [], []
    for n in targets:
        url = chapters[n - 1][1]
        rec = cache.meta(url)
        if not rec:
            continue
        (changed if cache.get(url) != before[n] else same).append(n)
        if status == "done":  # 中斷時沒重抓完，下次還要再試
            extra = {k: v for k, v in rec.items() if k not in ("url", "title", "content")}
            cache.put(url, rec["title"], rec["content"], **{**extra, "retried": True})
    cache.close()
    return status, changed, same


def run_and_export(args, base: str, meta: dict, cache: ChapterCache, chapters: list, job, retry=None) -> str:
    """執行下載工作 job()，不論成功或中斷都把暫存輸出成檔案，再記錄網站速度、檢查品質。
    chapters 為輸出順序 [(標題, 網址)]（next 模式由 job 邊下載邊加入）。
    retry(索引清單)：下載完成後用來重抓可疑章節（有目錄時才有；args.no_retry 時不使用）。
    回傳 "done"（完成）、"interrupted"（使用者按 Ctrl+C）或 "error"（下載失敗）。"""
    t0 = time.time()
    status = "error"
    try:
        status = _run(job)
    finally:
        cache.close()
        paths, count, chars, missing, removed = export_book(base, args.format, chapters, cache, meta)
        print(f"{'完成' if status == 'done' else '已輸出目前進度'}：共 {count} 章、{chars:,} 字，"
              f"耗時 {time.time() - t0:.1f} 秒 → {'、'.join(paths)}")
        if missing:
            print(f"  尚未下載的章節（{len(missing)} 章）：{_ranges(missing)}")
        if removed:
            sample = "、".join(f"「{line[:30]}」" for line in sorted(removed)[:3])
            print(f"  已刪除每章都有的 {len(removed)} 種雜訊行，例如 {sample}（不想刪除請加 --keep-repeated）")
        rate = learned_rate(args.state)
        if rate:
            rate *= RATE_SAFETY
            host = urlparse(args.url).netloc
            save_site_rate(host, rate)
            print(f"  已記錄 {host} 的穩定速度：每秒 {rate:.1f} 頁（下次直接從這個速度開始）")

    warnings = check_quality(chapters, cache)
    if warnings and retry and status == "done" and not getattr(args, "no_retry", False):
        status, changed, same = retry_suspects(warnings, chapters, cache, retry)
        if changed or same:
            export_book(base, args.format, chapters, cache, meta)
            warnings = check_quality(chapters, cache)
            still = {n for n, _ in warnings}
            fixed = [n for n in changed if n not in still]
            if fixed:
                print(f"  ✓ 重抓後已修正：第 {_ranges(fixed)} 章（輸出檔已更新）")
            if same:
                print(f"  重抓後內容相同：第 {_ranges(same)} 章，多半是網站本身的內容就是這樣，之後不會再自動重抓")
    if warnings:
        print(f"\n⚠ 有 {len(warnings)} 個章節可能有問題，請檢查：")
        for _, w in warnings[:20]:
            print(f"  {w}")
        if len(warnings) > 20:
            print(f"  …另外還有 {len(warnings) - 20} 個")
        print("  要重新下載某幾章：--start N --end M --refresh")
    return status


def download_toc_book(driver, args, chapters, base: str, meta: dict) -> str:
    """有目錄時：略過暫存中已有的章節 → 下載 → 輸出 → 檢查品質。"""
    total = len(chapters)
    lo, hi = max(args.start, 1) - 1, min(args.end or total, total)
    cache = ChapterCache(cache_path(base))
    txt = base + ".txt"
    if lo > 0 and not len(cache) and os.path.exists(txt):
        # 舊版（沒有暫存）下載的檔案：--start 只會下載後半段，直接覆寫會遺失前面的章節
        backup = base + ".bak.txt"
        os.replace(txt, backup)
        print(f"⚠ 沒有找到「{txt}」的下載暫存，原檔已備份為「{backup}」，新檔只會包含這次下載的章節。")
    indices = [i for i in range(lo, hi) if args.refresh or chapters[i][1] not in cache]
    skipped = (hi - lo) - len(indices)
    if skipped:
        print(f"已有 {skipped} 章下載過（暫存：{cache.path}），自動略過；要重新下載請加 --refresh")

    def fetch(todo):
        if args.slow:
            download_by_toc(driver, chapters, todo, cache, args.delay)
        else:
            download_fast(driver, chapters, todo, cache, args.state, args.batch_size)

    def job():
        if not indices:
            print("指定範圍的章節都已下載過。")
        else:
            fetch(indices)

    return run_and_export(args, base, meta, cache, chapters, job, retry=fetch)


def download_next_book(driver, args, base: str, meta: dict) -> str:
    """沒有目錄時：從起始章節一路抓「下一章」，同樣使用暫存，可中斷續傳。"""
    cache = ChapterCache(cache_path(base))
    chain: list[tuple[str, str]] = []
    return run_and_export(args, base, meta, cache, chain,
                          lambda: download_by_next(driver, args.url, cache, chain, args.max_pages,
                                                   args.delay, args.refresh))


def download_cover(driver, url: str, base: str) -> None:
    """下載封面存進暫存資料夾（已下載過就略過），輸出 EPUB 時使用。
    圖片放在其他網域時，從目錄頁 fetch() 會被瀏覽器的跨網域限制擋下，改成先開啟圖片網址再抓，抓完回到原頁面。"""
    if cover_path(base):
        return
    data = fetch_binary(driver, url)
    if not (data and image_type(data)) and urlparse(url).netloc != urlparse(driver.current_url).netloc:
        back = driver.current_url
        try:
            driver.get(url)
            data = fetch_binary(driver, url)
        except WebDriverException:
            data = None
        finally:
            load_soup(driver, back)  # 快速模式要在小說網站的頁面上 fetch()，一定要回到原頁面
    kind = image_type(data) if data else None
    if not kind:
        print("  ⚠ 無法下載封面，EPUB 將不含封面")
        return
    path = f"{cache_path(base)[:-len('.jsonl')]}.cover.{kind[0]}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print(f"已下載封面（{len(data) // 1024} KB）")
