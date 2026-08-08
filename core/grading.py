"""生產指標分級 —— 對照全國常模,判定 A~F 級距。

演算法驗證基準:合億畜牧場 2025 年報 18 項實際評級(tests/test_grading.py)。
規則來源:specs/reference/benchmark-2025.md
"""

from typing import Dict, List, NamedTuple, Optional, Sequence

from core.benchmark import bands

# 官方年報定義:A級 <10%, B級 10~25%, C級 25~50%, D級 50~75%, E級 75~90%, F級 >90%
# 切點的唯一來源是 data/benchmark_2025.json,這裡只是取用,不再自訂一份。
BANDS = bands()

WORST_GRADE = "F"


class GradeResult(NamedTuple):
    """單一指標的評級結果。"""

    value: float
    grade: str
    percentile_band: tuple  # (下界, 上界),例如 (50, 75);最差級為 (90, 100)


def _validate(value, percentiles: Sequence[float]) -> None:
    if value is None:
        raise TypeError("value 不可為 None")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"value 必須為數值,收到 {type(value).__name__}")
    if percentiles is None or len(percentiles) != len(BANDS):
        raise ValueError(f"percentiles 必須有 {len(BANDS)} 個值,收到 {percentiles}")

    ascending = all(a < b for a, b in zip(percentiles, percentiles[1:]))
    descending = all(a > b for a, b in zip(percentiles, percentiles[1:]))
    if not (ascending or descending):
        raise ValueError(f"percentiles 必須單調遞增或遞減,收到 {list(percentiles)}")


def is_higher_better(percentiles: Sequence[float]) -> bool:
    """指標方向由資料自行推導,不另外維護對照表。

    百分位欄位固定由「最佳」排到「最差」。因此第一欄大於最後一欄時,
    代表數值越高越好(如 PSY);反之為越低越好(如離乳前死亡率)。
    """
    return percentiles[0] > percentiles[-1]


def grade(value: float, percentiles: Sequence[float]) -> str:
    """回傳 A~F 級距。

    邊界採嚴格不等式:值恰等於切點時歸入較差的一級。
    依據:合億畜牧場離乳前死亡率 20.21 恰等於 75% 切點,官方判 E 而非 D。
    """
    _validate(value, percentiles)
    higher_better = is_higher_better(percentiles)

    for i, (_, letter) in enumerate(BANDS):
        better = value > percentiles[i] if higher_better else value < percentiles[i]
        if better:
            return letter
    return WORST_GRADE


def percentile_band(grade_letter: str) -> tuple:
    """把級距換算回百分位區間,供畫面顯示「本場落在哪一段」。"""
    cuts = [cut for cut, _ in BANDS]
    letters = [letter for _, letter in BANDS]
    if grade_letter == WORST_GRADE:
        return (cuts[-1], 100)
    i = letters.index(grade_letter)
    lower = 0 if i == 0 else cuts[i - 1]
    return (lower, cuts[i])


def grade_all(
    values: Dict[str, float],
    metrics: Dict[str, dict],
) -> Dict[str, GradeResult]:
    """批次評級。

    只處理「有填值」且「可評級」的指標:
      - 未填的指標不出現在結果中(規格 US-2 驗收條件 1)
      - gradable=False 的規模型指標不評級(規格 US-3 驗收條件 3)
      - 不認識的 key 直接忽略
    """
    results = {}
    for key, value in values.items():
        meta = metrics.get(key)
        if meta is None or not meta.get("gradable"):
            continue
        if value is None:
            continue
        letter = grade(value, meta["percentiles"])
        results[key] = GradeResult(
            value=value,
            grade=letter,
            percentile_band=percentile_band(letter),
        )
    return results
