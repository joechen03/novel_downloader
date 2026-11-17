"""快速模式：在瀏覽器內用 fetch() 平行下載（啟動 + 輪詢），自動調速與記住網站速度。"""

import base64
import json
import os
import time

from selenium.common.exceptions import WebDriverException

from .browser import wait_for_human
from .config import BASE_DIR


SITE_RATES_FILE = os.path.join(BASE_DIR, "site_rates.json")  # 各網站學到的速度上限


# 快速模式：在瀏覽器內用 fetch() 平行下載原始 HTML，再用多進程解析
# （沿用瀏覽器的連線、憑證與 Cookie，所以 Cloudflare / 憑證問題都跟瀏覽器一樣能過）
# 啟動一批下載後立刻回傳 job id，不等下載完成；Python 再用 _POLL_JS 每 0.3 秒查一次。
# 不用 execute_async_script 等整批完成，是因為 chromedriver 一次只處理一個指令：
# 長時間的指令會讓 Ctrl+C 後的 driver.quit() 卡住，也受 Selenium 120 秒逾時限制。
_FETCH_START_JS = r"""
const urls = arguments[0], limit = arguments[1], rate = arguments[2];
const job = {out: new Array(urls.length).fill(null), done: false, finished: 0};
const id = 'nd' + Date.now() + '_' + Math.random().toString(36).slice(2);
(window.__ndJobs = window.__ndJobs || {})[id] = job;
let next = 0;
// 限速：rate > 0 時，每個請求的「開始時間」至少間隔 1000/rate 毫秒
const gap = rate > 0 ? 1000 / rate : 0;
let nextStart = performance.now();
async function throttle() {
  if (!gap) return;
  const now = performance.now();
  const wait = nextStart - now;
  nextStart = Math.max(now, nextStart) + gap;
  if (wait > 0) await new Promise(res => setTimeout(res, wait));
}
function decode(buf, ctype) {
  let m = (ctype || '').match(/charset=["']?([\w-]+)/i);
  if (!m) m = new TextDecoder('latin1').decode(buf.slice(0, 4096)).match(/charset=["']?([\w-]+)/i);
  try { return new TextDecoder(m ? m[1] : 'utf-8').decode(buf); }
  catch (e) { return new TextDecoder('utf-8').decode(buf); }
}
async function getOne(u) {
  for (let t = 0; t < 3; t++) {
    try {
      await throttle();
      const r = await fetch(u, {credentials: 'include', signal: AbortSignal.timeout(30000), headers: {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}});
      const html = decode(await r.arrayBuffer(), r.headers.get('content-type'));
      // 人機驗證 / 封鎖 → 交給 Python 請使用者驗證；限流（429/503）→ 交給 Python 降速重試
      if (r.status === 403 || /challenge-platform|cf-chl|cf_chl|<title>\s*(正在验证浏览器|正在驗證瀏覽器)/.test(html)) return '__CHALLENGE__';
      if (r.status === 429 || r.status === 503) return '__RATELIMIT__';
      if (!r.ok) throw new Error(r.status);
      // 先砍掉 script/style，減少回傳給 Python 的資料量
      return html.replace(/<(script|style|noscript)\b[\s\S]*?<\/\1>/gi, '');
    } catch (e) { await new Promise(res => setTimeout(res, 500 * (t + 1))); }
  }
  return null;
}
async function worker() {
  while (next < urls.length) { const i = next++; job.out[i] = await getOne(urls[i]); job.finished++; }
}
Promise.all(Array.from({length: limit}, worker)).then(() => { job.done = true; });
return id;
"""

_POLL_JS = r"""
const jobs = window.__ndJobs || {}, job = jobs[arguments[0]];
if (!job) return {lost: true};
if (!job.done) return {done: false};
delete jobs[arguments[0]];
return {done: true, out: job.out};
"""

CHALLENGE = "__CHALLENGE__"

RATELIMIT = "__RATELIMIT__"


# 自動調速參數（被限流時）
RATE_START = 10.0      # 第一次被限流時的起始速度（頁/秒）

RATE_MIN = 0.5

RATE_MAX = 50.0

RATE_DOWN = 0.6        # 被限流時乘上此值

RATE_UP = 1.25         # 一段時間沒被擋時乘上此值

RATE_SAFETY = 0.85     # 記錄網站速度時打的折扣（學到的是「剛好沒被擋」的速度，留一點餘裕）

RATELIMIT_WAIT = 3     # 被限流後等待秒數

POLL_INTERVAL = 0.3    # 快速模式每隔幾秒檢查一次瀏覽器是否抓完


def fetch_many(driver, urls: list[str], concurrency: int, rate: float = 0) -> list[str | None]:
    """在瀏覽器內平行下載 urls，回傳 HTML（失敗為 None；被擋為 CHALLENGE / RATELIMIT）。"""
    if not urls:
        return []
    job = driver.execute_script(_FETCH_START_JS, urls, concurrency, rate)
    while True:
        time.sleep(POLL_INTERVAL)  # 等待期間按 Ctrl+C 會立刻生效
        r = driver.execute_script(_POLL_JS, job)
        if r.get("lost"):  # 頁面被切換（例如使用者在視窗中點了連結），這批全部當成失敗
            return [None] * len(urls)
        if r.get("done"):
            return r["out"]


def fetch_many_with_verify(driver, urls: list[str], state: dict) -> list[str | None]:
    """平行下載並自動調速。state['c'] 為同時下載數，state['rate'] 為每秒請求數（0 = 不限）。

    * 被限流（429/503）：等幾秒、速度 × RATE_DOWN，重抓被擋的頁面。
    * 一段（約 10 秒的量）都沒被擋：速度 × RATE_UP，逐步逼近網站上限。
    * 人機驗證（403 / Cloudflare）：大幅降速並請使用者手動驗證。
    """
    results: list[str | None] = [None] * len(urls)
    todo = list(range(len(urls)))
    strikes = 0  # 連續整段都被限流的次數
    while todo:
        rate = state["rate"]
        size = len(todo) if rate <= 0 else max(10, int(rate * 10))
        chunk, todo = todo[:size], todo[size:]
        res = fetch_many(driver, [urls[k] for k in chunk], state["c"], rate)

        challenged = [k for k, h in zip(chunk, res) if h == CHALLENGE]
        limited = [k for k, h in zip(chunk, res) if h == RATELIMIT]
        for k, h in zip(chunk, res):
            if h not in (CHALLENGE, RATELIMIT):
                results[k] = h
        todo = challenged + limited + todo  # 被擋的頁面優先重抓

        if challenged or limited:  # 不限速時被擋記為無限大，代表「網站有上限」
            state.setdefault("limited", []).append(rate if rate > 0 else float("inf"))
        elif not challenged and not limited and rate > 0:
            state.setdefault("ok", []).append(rate)

        if challenged or strikes >= 3:
            # 不限速時先改成每秒 2 個，之後每次被擋再減半
            state["rate"] = 2.0 if rate <= 0 else max(0.2, rate / 2)
            state["c"] = max(2, min(state["c"], int(state["rate"] * 2) + 1))
            print(f"  {len(challenged) or len(limited)} 頁被網站擋下，降速為每秒 {state['rate']:.1f} 頁")
            wait_for_human(driver, urls[(challenged or limited)[0]])
            strikes = 0
        elif limited:
            state["rate"] = RATE_START if rate <= 0 else max(RATE_MIN, rate * RATE_DOWN)
            strikes = strikes + 1 if len(limited) == len(chunk) else 0
            print(f"  {len(limited)} 頁被限流，{RATELIMIT_WAIT} 秒後以每秒 {state['rate']:.1f} 頁重試")
            time.sleep(RATELIMIT_WAIT)
        elif rate > 0:
            state["rate"] = min(RATE_MAX, rate * RATE_UP)
            strikes = 0
    return results


def learned_rate(state: dict) -> float | None:
    """這次下載中「沒被擋過的最快速度」：低於任何曾被擋的速度、且整段順利的最高速度。
    這次完全沒被擋過 → None：順利的速度只是我們試過的速度，不代表網站上限，不能拿來覆蓋記錄。"""
    if not state.get("limited"):
        return None
    limit = min(state["limited"])
    ok = [r for r in state.get("ok", []) if r < limit]
    return max(ok) if ok else None


def load_site_rates() -> dict[str, float]:
    try:
        with open(SITE_RATES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_site_rate(host: str, rate: float) -> None:
    data = load_site_rates()
    data[host] = round(rate, 2)
    try:
        with open(SITE_RATES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError as e:
        print(f"  ⚠ 無法儲存網站速度記錄：{e}")


# 封面
_FETCH_BINARY_JS = r"""
const url = arguments[0], done = arguments[arguments.length - 1];
fetch(url, {signal: AbortSignal.timeout(15000)})
  .then(r => r.ok ? r.arrayBuffer() : null)
  .then(buf => {
    if (!buf || buf.byteLength > 5000000) return done(null);
    const b = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    done(btoa(s));
  })
  .catch(() => done(null));
"""


def fetch_binary(driver, url: str) -> bytes | None:
    """在瀏覽器內下載一個小檔案（封面圖片），失敗回傳 None。"""
    try:
        data = driver.execute_async_script(_FETCH_BINARY_JS, url)
    except WebDriverException:
        return None
    return base64.b64decode(data) if data else None
