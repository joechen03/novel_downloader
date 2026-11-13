"""把真實網站的目錄頁存成測試樣本（tests/fixtures/<名稱>.html）。

用法：
    python tests/save_fixture.py <目錄網址> <名稱>
    例：python tests/save_fixture.py https://czbooks.net/n/sklkm61fg czbooks_toc

會開瀏覽器載入頁面、展開完整目錄（expand_toc），移除 script/style 後存檔，
並在檔案開頭以註解記錄來源網址與實際網址（測試用它當 base URL）。
存好後到 tests/test_novel_downloader.py 的 SITE_CASES 加上預期結果。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import novel_downloader as nd  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        return
    url, name = sys.argv[1], sys.argv[2]
    driver = nd.build_driver(nd.CHROME_PROFILE, False)
    driver.minimize_window()
    nd.WINDOW["minimized"] = True
    try:
        nd.load_soup(driver, url)
        nd.expand_toc(driver)
        html = driver.page_source
        final_url = driver.current_url
    finally:
        driver.quit()
    html = re.sub(r"<(script|style|noscript|svg)\b[\s\S]*?</\1>", "", html, flags=re.I)
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    path = os.path.join(FIXTURE_DIR, name + ".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!-- source: {url} -->\n<!-- final: {final_url} -->\n{html}")
    print(f"已存檔：{path}（{len(html):,} 字元）")


if __name__ == "__main__":
    main()
