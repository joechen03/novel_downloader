"""簡轉繁（OpenCC）與引號轉換。"""

import re


# 簡轉繁（OpenCC，以詞為單位轉換；已是繁體的字不會被改動）
CONVERTER = {"enabled": False, "t2s": None, "s2t": None}

QUOTE_MAP = str.maketrans({"“": "「", "”": "」", "‘": "『", "’": "』"})

_NUM = "0-9０-９一二三四五六七八九十百千萬幾數"


# OpenCC 詞庫處理不到的常見錯誤（正規表示式, 取代）；遇到新的可以加在這裡
FIX_RULES = [
    (re.compile(r"(?<=[死戰打爭奮搏決械內])斗"), "鬥"),
    (re.compile(r"(?<=[眼神心夢這那哪屋房家口手])里(?![程長])"), "裡"),
    # 「XX里」表示位置（通道里的、工資里扣除）；排除公里、鄰里、歇斯底里與音譯（諾里斯）
    (re.compile(r"(?<![公英鄰故千萬鄉底華\d一二三四五六七八九十百])里"
                r"(?=[的，。、！？；：」』…—\s面頭邊居也都就還又才卻扣])"), "裡"),
    (re.compile(r"几乎"), "幾乎"),
    (re.compile(r"余裕"), "餘裕"),
    (re.compile(r"(?<![皇王太母])后(?=[，,便就再才])"), "後"),
    (re.compile(rf"(?<![{_NUM}])海里"), "海裡"),
    (re.compile(r"一齣(?![戲劇好])"), "一出"),
]


def setup_converter(config: str) -> None:
    """config 為 OpenCC 設定名稱（如 s2tw），'none' 表示不轉換。"""
    if config == "none":
        return
    CONVERTER["enabled"] = True  # 引號轉換不需要 OpenCC
    try:
        import opencc
    except ImportError:
        print("⚠ 未安裝 OpenCC，略過簡轉繁。請執行：pip install opencc")
        return
    # 先轉成純簡體再轉回繁體：網站的「機器繁體」常是繁簡混雜（如「戰斗」「腦海里」），
    # 直接 s2t 對不到詞庫；先統一成簡體，OpenCC 才能用完整詞彙判斷正確的繁體字。
    CONVERTER["t2s"] = opencc.OpenCC("t2s")
    CONVERTER["s2t"] = opencc.OpenCC(config)


def to_traditional(text: str) -> str:
    if not CONVERTER["enabled"]:
        return text
    if CONVERTER["s2t"] is not None:
        text = CONVERTER["s2t"].convert(CONVERTER["t2s"].convert(text))
        for pattern, repl in FIX_RULES:
            text = pattern.sub(repl, text)
    return text.translate(QUOTE_MAP)
