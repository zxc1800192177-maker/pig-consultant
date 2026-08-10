"""藥品標示辨識結果的解析。

跟 ai/transport.py 的 parse_stream_line() 是同一種角色:把 AI 產生的
輸出整理成程式能用的形狀。因為存在的理由就是「模型輸出長什麼樣」,
所以放在 ai/ 而不是 core/ —— core/ 是純計算層,不得依賴 AI 層。

**這裡的輸出永遠是「草稿」,不是事實。** 呼叫端必須讓牧場主核對過
才寫進藥品庫(憲法第三條)。藥品庫的內容會被 build_my_drugs_context()
當成可引用的劑量依據送進疾病諮詢,若 AI 讀出來的數字能自動入庫,
等於 AI 的輸出繞一圈變成了「使用者提供的事實」。
"""

import json
import re
from typing import Optional

import config

FIELDS = ("name", "activeIngredient", "dosageNote", "withdrawalDays")

# 模型常常無視「不要 markdown 圍籬」這條指令,把 JSON 包在 ```json ... ``` 裡。
# 與其反覆調提示詞,不如在解析這端容忍 —— 提示詞管不動的事,程式碼管得動。
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

_EMPTY = {field: None for field in FIELDS}


def _strip_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group(1) if match else text


def _first_object(text: str) -> Optional[str]:
    """取出第一個成對的大括號區塊。

    模型偶爾會在 JSON 前面加一句「以下是辨識結果:」。直接 json.loads
    會失敗,但內容其實是好的,為了這種小事整張照片重拍很浪費。

    用括號配對而不是正則,因為 JSON 字串裡本來就可能出現大括號。
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _clean_text(value, limit: int) -> Optional[str]:
    """字串欄位收斂。空字串與「null」這類佔位字視同沒讀到。

    模型有時不填 null 而填 "null"、"無"、"看不清楚" —— 那些是它在
    描述「沒有」,不是標示上真的印著這幾個字,直接當成 None。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-"):
        return None
    if text in ("無", "未知", "看不清楚", "不明", "無法辨識"):
        return None
    return text[:limit]


def _clean_days(value) -> Optional[int]:
    """休藥期。這是整份辨識結果裡最不能出錯的數字。

    只接受非負整數。bool 要單獨擋掉 —— Python 的 True 是 int 的子類別,
    isinstance(True, int) 為真,不擋的話 True 會變成休藥期 1 天。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and value != int(value):
        return None
    days = int(value)
    return days if days >= 0 else None


def parse_label(text: str) -> dict:
    """把模型回應解析成藥品草稿。

    任何解析不出來的情況都回傳「四個欄位都是 None」,而不是拋例外 ——
    照片糊掉、模型講廢話都是預期中會發生的事,不是程式錯誤。
    呼叫端據此告訴使用者重拍,而不是看到一個 500。
    """
    if not isinstance(text, str) or not text.strip():
        return dict(_EMPTY)

    raw = _first_object(_strip_fence(text))
    if raw is None:
        return dict(_EMPTY)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return dict(_EMPTY)

    if not isinstance(parsed, dict):
        return dict(_EMPTY)

    return {
        "name": _clean_text(parsed.get("name"), config.MAX_DRUG_NAME_CHARS),
        "activeIngredient": _clean_text(
            parsed.get("activeIngredient"), config.MAX_DRUG_INGREDIENT_CHARS
        ),
        "dosageNote": _clean_text(parsed.get("dosageNote"), config.MAX_DRUG_NOTE_CHARS),
        "withdrawalDays": _clean_days(parsed.get("withdrawalDays")),
    }
