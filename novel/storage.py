"""本機資料：章節暫存（JSON Lines）、書籍資訊（--update-all 用）、封面圖檔、檔名。"""

import json
import os
import re

from .config import BOOK_INFO_EXT, CACHE_DIRNAME


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip() or "novel"


# 章節暫存：每下載完一批章節就存起來，中斷後重新執行會自動略過已下載的章節
def cache_path(base: str) -> str:
    """base 為不含副檔名的輸出路徑（例如 downloads/術師手冊_黃金屋）。"""
    folder = os.path.dirname(os.path.abspath(base))
    return os.path.join(folder, CACHE_DIRNAME, os.path.basename(base) + ".jsonl")


def book_info_path(base: str) -> str:
    return cache_path(base)[:-len(".jsonl")] + BOOK_INFO_EXT


# 記錄在書籍資訊檔中、--update-all 時沿用的下載設定
BOOK_INFO_KEYS = ["url", "format", "mode", "reverse", "slow"]


def save_book_info(base: str, args) -> None:
    """記錄這本書的網址與下載設定，之後 --update-all 可以一次更新所有書。"""
    info = {k: getattr(args, k) for k in BOOK_INFO_KEYS}
    info["output"] = os.path.abspath(base)
    path = book_info_path(base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)


def load_book_infos(folder: str) -> list[dict]:
    """讀取 folder/.novel_cache/ 中所有書籍資訊，依檔名排序。"""
    cache_dir = os.path.join(folder, CACHE_DIRNAME)
    infos = []
    if os.path.isdir(cache_dir):
        for name in sorted(os.listdir(cache_dir)):
            if name.endswith(BOOK_INFO_EXT):
                try:
                    with open(os.path.join(cache_dir, name), encoding="utf-8") as f:
                        info = json.load(f)
                    if info.get("url") and info.get("output"):
                        infos.append(info)
                except (OSError, json.JSONDecodeError):
                    print(f"⚠ 無法讀取書籍資訊：{name}")
    return infos


def cover_path(base: str) -> str | None:
    """已下載的封面圖檔（副檔名依圖片格式），沒有則回傳 None。"""
    stem = cache_path(base)[:-len(".jsonl")] + ".cover"
    for ext in IMAGE_TYPES.values():
        if os.path.exists(f"{stem}.{ext[0]}"):
            return f"{stem}.{ext[0]}"
    return None


# 圖片格式：開頭的位元組 → (副檔名, media type)
IMAGE_TYPES = {b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"\x89PNG": ("png", "image/png"),
               b"GIF8": ("gif", "image/gif"), b"RIFF": ("webp", "image/webp")}


def image_type(data: bytes) -> tuple[str, str] | None:
    for magic, kind in IMAGE_TYPES.items():
        if data.startswith(magic) and (magic != b"RIFF" or data[8:12] == b"WEBP"):
            return kind
    return None


class ChapterCache:
    """JSON Lines 格式，一行一章 {"url", "title", "content", ...}，以章節網址為鍵（同網址以最後一筆為準）。
    其他欄位：page_title（章節頁上的標題，品質檢查用）、next（next 模式的下一章網址）。
    存的是未經簡轉繁的原始內容，輸出時才轉換。"""

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict] = {}
        self._f = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if isinstance(rec["title"], str) and isinstance(rec["content"], str):
                            self.data[rec["url"]] = rec
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue  # 中斷時寫到一半的最後一行

    def __contains__(self, url: str) -> bool:
        return url in self.data

    def __len__(self) -> int:
        return len(self.data)

    def get(self, url: str) -> tuple[str, str]:
        rec = self.data[url]
        return rec["title"], rec["content"]

    def meta(self, url: str) -> dict:
        return self.data.get(url, {})

    def put(self, url: str, title: str, content: str, **extra) -> None:
        rec = {"url": url, "title": title, "content": content, **extra}
        self.data[url] = rec
        if self._f is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            broken_tail = False
            if os.path.exists(self.path) and os.path.getsize(self.path):
                with open(self.path, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    broken_tail = f.read(1) != b"\n"
            self._f = open(self.path, "a", encoding="utf-8")
            if broken_tail:  # 上次中斷在一行中間，先換行，避免新資料黏在壞掉的那行後面
                self._f.write("\n")
        self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None
