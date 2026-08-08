"""常模資料存取。

資料:data/benchmark_2025.json(2025 年報,全國 110 場)
說明:specs/reference/benchmark-2025.md
"""

import json
import pathlib
from typing import Dict, List

DATA_PATH = pathlib.Path(__file__).parent.parent / "data" / "benchmark_2025.json"


def _load() -> dict:
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


BENCHMARK = _load()

_BY_KEY: Dict[str, dict] = {m["key"]: m for m in BENCHMARK["metrics"]}


def get_metric(key: str) -> dict:
    """取得指標定義。未知的 key 直接拋錯,不回 None ——
    靜默的 None 會一路傳到畫面上變成空白,比炸掉難查。"""
    return _BY_KEY[key]


def all_metrics() -> List[dict]:
    return BENCHMARK["metrics"]


def gradable_metrics() -> List[dict]:
    """可評級的指標(有百分位者)。規模型指標不在此列。"""
    return [m for m in BENCHMARK["metrics"] if m["gradable"]]


def metrics_index() -> Dict[str, dict]:
    """供 grading.grade_all() 使用的 key -> 定義對照。"""
    return _BY_KEY


def upstream_of(key: str) -> List[str]:
    """該指標的上游驅動指標,取自 2025 年報的因果敘述。

    這是本專案唯一仍屬「文字解讀」的領域知識,放在資料檔中讓專家覆核時
    只需看一個地方。排序本身不依賴它 —— 它只用於呈現連鎖影響的脈絡。
    """
    return BENCHMARK["upstream"].get(key, [])
