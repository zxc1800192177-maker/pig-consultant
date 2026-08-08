"""驗收測試 —— 規格第 8 節成功條件 1。

「用合億畜牧場 2025 年報的實際數據輸入,系統產出的 18 項評級與官方報告完全一致。」

前面的 test_grading.py 用的是寫在測試裡的百分位;這裡走的是完整路徑:
真實資料檔 -> benchmark 載入 -> grade_all 批次評級 -> 比對官方報表。
資料檔抄錯、key 對錯、評級邏輯改壞,都會在這裡被抓到。

來源:豬豬顧問專案/豬豬牧場-2025.pdf
"""

import pytest

from core.benchmark import metrics_index
from core.grading import grade_all
from core.labels import source_label

# 合億畜牧場 2025 年實際數據 -> 官方報表印出的級距
HOLDING_FARM_VALUES = {
    "psy": 20.63,
    "litters_per_sow_year": 2.25,
    "npd": 59.99,
    "farrowing_rate": 71.56,
    "repeat_service_rate": 17.87,
    "wean_to_service": 7.05,
    "live_born_per_sow_year": 27.00,
    "total_born_per_litter": 13.29,
    "live_born_per_litter": 11.92,
    "preweaning_mortality": 20.21,
    "weaned_per_sow": 9.47,
    "weaned_per_litter": 9.79,
    "weaning_age": 21.97,
    "lactation_days": 21.82,
    "parity_sow_psy": 20.69,
    "farrowing_index": 2.42,
    "gestation_days": 114.08,
    "boars_inventory": 11.00,
}

OFFICIAL_GRADES = {
    "psy": "D",
    "litters_per_sow_year": "B",
    "npd": "C",
    "farrowing_rate": "D",
    "repeat_service_rate": "E",
    "wean_to_service": "D",
    "live_born_per_sow_year": "C",
    "total_born_per_litter": "D",
    "live_born_per_litter": "D",
    "preweaning_mortality": "E",
    "weaned_per_sow": "D",
    "weaned_per_litter": "E",
    "weaning_age": "F",
    "lactation_days": "F",
    "parity_sow_psy": "D",
    "farrowing_index": "B",
    "gestation_days": "B",
    "boars_inventory": "B",
}


@pytest.fixture(scope="module")
def graded():
    return grade_all(HOLDING_FARM_VALUES, metrics_index())


def test_all_eighteen_metrics_graded(graded):
    assert len(graded) == 18, f"應評 18 項,實際 {len(graded)} 項"


@pytest.mark.parametrize("key,expected", sorted(OFFICIAL_GRADES.items()))
def test_grade_matches_official_report(graded, key, expected):
    actual = graded[key].grade
    assert actual == expected, (
        f"{key}: 本場值 {HOLDING_FARM_VALUES[key]} "
        f"官方判 {expected},程式判 {actual}"
    )


def test_no_metric_missing_from_official_comparison():
    """兩份對照表必須涵蓋同一組指標,避免漏測。"""
    assert set(HOLDING_FARM_VALUES) == set(OFFICIAL_GRADES)


def test_source_is_disclosed():
    """憲法第三條:顯示常模數字時必須一併標註來源。"""
    label = source_label()
    assert "2025" in label and "110" in label


def test_scale_metrics_are_not_graded():
    """規格 US-3 驗收條件 3:規模型數值不評級,即使有填也一樣。"""
    with_scale = dict(HOLDING_FARM_VALUES)
    with_scale.update({
        "total_services": 1181.00,
        "total_born": 11617.00,
        "sows_inventory": 386.00,
    })
    result = grade_all(with_scale, metrics_index())
    assert len(result) == 18
    for key in ("total_services", "total_born", "sows_inventory"):
        assert key not in result
