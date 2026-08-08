"""輸入轉換 —— 把各種來源的原始值轉成可計算的數字。

從 metrics.py 拆出來的理由(SRP):
  coercion.py 因「輸入來源改變」而修改(表單字串、PDF 解析結果、API JSON)
  metrics.py  因「領域規則改變」而修改(什麼數值算合理)

規格已規劃 v0.2 要支援上傳年報 PDF 自動解析。屆時新增的是轉換規則
(例如處理「1,537.45」的千分位逗號、全形數字),合理範圍的定義不該跟著動。
"""

import unicodedata
from typing import Any, Optional


def to_number(value: Any) -> Optional[float]:
    """回傳 float,無法轉換則回 None。

    bool 必須擋掉 —— True 在 Python 裡等於 1,放行會靜默算出錯誤結果,
    而且錯得很安靜:畫面上會出現一個看似合理的級距。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _parse_string(value)
    return None


def _parse_string(text: str) -> Optional[float]:
    """處理人類與各種來源實際會送進來的字串格式。

    表單會送純字串;報表複製貼上會帶千分位逗號與全形數字;
    有些欄位會帶百分比符號。
    """
    cleaned = unicodedata.normalize("NFKC", text).strip()
    cleaned = cleaned.replace(",", "").replace("%", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None
