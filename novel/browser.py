"""瀏覽器：啟動 Chrome、清除殘留程序、載入頁面、人機驗證、防止同時執行兩次。"""

import os
import re
import subprocess
import time

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException, UnexpectedAlertPresentException, WebDriverException
from selenium.webdriver.chrome.service import Service

from .config import CHROME_PROFILE, LOCK_NAME, MAX_RETRIES, WAIT_SECONDS


def kill_stale_chrome(profile_dir: str) -> int:
    """關閉還在使用指定設定檔的 Chrome（上次程式被強制結束時殘留的）。回傳關閉的程序數。
    只用在本程式專用的設定檔，不會動到使用者平常開的 Chrome。"""
    # 子程序的參數是 --user-data-dir="路徑"（有引號），主程序沒有；所以只比對路徑本身，且不分大小寫
    needle = os.path.abspath(profile_dir)
    try:
        if os.name == "nt":
            ps = ("$p = '%s'.ToLower(); $n = 0; Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                  "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains('--user-data-dir') "
                  "-and $_.CommandLine.ToLower().Contains($p) } | "
                  "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $n++ } catch {} }; $n"
                  % needle.replace("'", "''"))
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=60).stdout.strip()
            return int(out.splitlines()[-1]) if out else 0
        out = subprocess.run(["pkill", "-f", "-e", "--", needle], capture_output=True, text=True, timeout=60).stdout
        return len(out.splitlines())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def build_driver(profile_dir: str, headless: bool) -> webdriver.Chrome:
    try:
        return _start_chrome(profile_dir, headless)
    except SessionNotCreatedException as e:
        error = e
    # 專用設定檔被上次殘留的 Chrome 佔用：關掉殘留程序後再試一次
    if profile_dir and os.path.abspath(profile_dir) == os.path.abspath(CHROME_PROFILE):
        killed = kill_stale_chrome(profile_dir)
        if killed:
            print(f"已關閉 {killed} 個上次殘留的 Chrome 程序，重新啟動…")
            time.sleep(2)
            try:
                return _start_chrome(profile_dir, headless)
            except SessionNotCreatedException as e:
                error = e
    hint = (f"\n  設定檔「{profile_dir}」可能正被另一個 Chrome 視窗使用，請關閉後再試，"
            "或用 --profile 指定其他資料夾。") if profile_dir else ""
    raise RuntimeError(f"無法啟動 Chrome：{error.msg}{hint}") from error


def _start_chrome(profile_dir: str, headless: bool) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    if profile_dir:
        options.add_argument(f"--user-data-dir={profile_dir}")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.page_load_strategy = "eager"  # DOM 就緒即可，不等圖片/廣告
    # chromedriver 放在獨立的程序群組：按 Ctrl+C 時才不會連它一起被結束，
    # 否則程式無法關閉 Chrome，會殘留一堆 Chrome 程序並佔住設定檔
    if os.name == "nt":
        service = Service(popen_kw={"creation_flags": subprocess.CREATE_NEW_PROCESS_GROUP})
    else:
        service = Service(popen_kw={"start_new_session": True})
    return webdriver.Chrome(options=options, service=service)


def load_soup(driver, url: str, ready=None) -> BeautifulSoup:
    """載入網址並回傳 soup；ready(soup) 為 True 才算載入完成（支援 JS 動態內容）。失敗自動重試。"""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            driver.get(url)
            if is_blocked_page(driver.page_source) or is_challenge_page(driver.page_source):
                wait_for_human(driver, url)
            deadline = time.time() + WAIT_SECONDS
            while True:
                soup = BeautifulSoup(driver.page_source, "lxml")
                if ready is None or ready(soup) or time.time() > deadline:
                    return soup
                time.sleep(0.5)
        except UnexpectedAlertPresentException:
            try:
                driver.switch_to.alert.dismiss()
            except WebDriverException:
                pass
            last_error = "網站跳出警告視窗"
        except WebDriverException as e:
            last_error = e.msg
            if "ERR_NAME_NOT_RESOLVED" in (e.msg or ""):  # 網域不存在，重試也沒用
                raise RuntimeError(f"找不到網站（網域不存在）：{url}") from e
        print(f"  第 {attempt} 次載入失敗（{last_error}），{attempt * 2} 秒後重試…")
        time.sleep(attempt * 2)
    raise RuntimeError(f"載入失敗：{url}（{last_error}）")


def is_challenge_page(html: str) -> bool:
    # 只看 Cloudflare 的技術特徵，避免小說內文剛好出現「驗證」等字眼而誤判
    # 也比對自訂驗證頁的 <title>（ttkan 的「正在验证浏览器」）：只看 <title>，不會被正文誤判
    return bool(re.search(r"challenge-platform|cf-chl|cf_chl|<title>Just a moment"
                          r"|<title>\s*(正在验证浏览器|正在驗證瀏覽器)", html))


class BlockedError(RuntimeError):
    """網站直接封鎖（不是可以手動通過的驗證）。"""


def is_blocked_page(html: str) -> bool:
    return bool(re.search(r"cf-error-details|Sorry, you have been blocked"
                          r"|<title>Attention Required! \| Cloudflare", html))


# 瀏覽器視窗平常是否縮到工作列（需要驗證時才跳出來）
WINDOW = {"minimized": False}


def wait_for_human(driver, url: str, timeout: int = 600) -> None:
    """遇到人機驗證時，在瀏覽器開啟頁面，等使用者手動完成驗證。"""
    driver.get(url)
    if is_blocked_page(driver.page_source):
        raise BlockedError("網站封鎖了這個瀏覽器（Sorry, you have been blocked）")
    if not is_challenge_page(driver.page_source):
        return
    if WINDOW["minimized"]:
        driver.maximize_window()  # 把視窗叫出來讓使用者驗證
    print("\n  ⚠ 網站要求人機驗證，請在瀏覽器視窗中完成驗證，完成後會自動繼續…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        if not is_challenge_page(driver.page_source):
            print("  ✓ 驗證完成，繼續下載。")
            if WINDOW["minimized"]:
                driver.minimize_window()
            return
    raise RuntimeError("等待人機驗證逾時")


# 防止同時執行兩次（兩個程式共用同一個 Chrome 設定檔會互相干擾，
# 而且後啟動的那個會把前一個的 Chrome 當成殘留程序關掉）
def acquire_lock(profile_dir: str):
    """鎖定設定檔，回傳鎖定用的檔案物件；已被另一個執行中的本程式鎖定時回傳 None。
    用作業系統的檔案鎖，程式結束（包含當機、被強制結束）時會自動解除，不會留下過期的鎖。"""
    os.makedirs(profile_dir, exist_ok=True)
    f = open(os.path.join(profile_dir, LOCK_NAME), "a+")
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def release_lock(f) -> None:
    if f is None:
        return
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()
