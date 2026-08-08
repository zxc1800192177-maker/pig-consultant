"""分級演算法測試。

驗收基準:範例牧場 2025 年報的實際評級結果(specs/reference/benchmark-2025.md)。
這 18 項是官方報表印出來的答案,我們的程式必須算出完全一樣的結果。
"""

import pytest

from core.grading import BANDS, grade, grade_all


# 範例牧場實際數據 —— (本場值, 官方級距, 百分位[最佳→最差])
# 取自實際牧場年報(已去識別化)
HOLDING_FARM_CASES = [
    ("母豬年產離乳仔豬數", 20.63, "D", [25.65, 23.22, 21.11, 19.11, 18.24]),
    ("母豬年產胎數", 2.25, "B", [2.29, 2.22, 2.15, 2.06, 1.97]),
    ("母豬非生產天數", 59.99, "C", [39.89, 47.93, 60.68, 73.78, 83.65]),
    ("分娩率", 71.56, "D", [87.47, 82.37, 75.19, 69.71, 63.42]),
    ("重發情配種佔比", 17.87, "E", [5.49, 8.63, 11.94, 16.91, 22.47]),
    ("離乳到第一次配種間隔", 7.05, "D", [4.99, 5.58, 6.60, 8.99, 10.74]),
    ("母豬年產活仔數", 27.00, "C", [30.78, 27.84, 25.92, 23.18, 21.33]),
    ("窩均總仔數", 13.29, "D", [15.77, 14.55, 13.50, 12.68, 11.98]),
    ("窩均活仔數", 11.92, "D", [13.75, 12.81, 12.04, 11.35, 10.67]),
    ("離乳前死亡率", 20.21, "E", [8.77, 12.75, 16.14, 20.21, 24.38]),
    ("母豬平均離乳仔豬數", 9.47, "D", [11.32, 10.80, 10.09, 9.38, 8.92]),
    ("平均仔豬離乳日齡", 21.97, "F", [29.12, 28.14, 27.23, 26.46, 25.79]),
    ("懷孕天數", 114.08, "B", [114.01, 114.52, 115.12, 115.65, 116.26]),
    ("分娩指數", 2.42, "B", [2.44, 2.39, 2.35, 2.30, 2.23]),
    ("公豬在養頭數", 11.00, "B", [27.00, 10.00, 5.00, 2.00, 1.00]),
    ("每窩平均離乳仔豬數", 9.79, "E", [11.48, 10.96, 10.40, 9.79, 9.28]),
    ("平均母豬哺乳天數", 21.82, "F", [29.11, 27.88, 26.74, 25.98, 25.05]),
    ("經產母豬年產離乳仔豬數", 20.69, "D", [26.14, 23.61, 21.41, 19.28, 18.70]),
]


@pytest.mark.parametrize("name,value,expected,percentiles", HOLDING_FARM_CASES)
def test_matches_official_report(name, value, expected, percentiles):
    """每一項都必須與官方報表的級距一致。"""
    assert grade(value, percentiles) == expected, f"{name} 評級與官方報表不符"


class TestDirection:
    """指標方向必須由資料推導,不靠外部設定。"""

    HIGHER_BETTER = [25.65, 23.22, 21.11, 19.11, 18.24]   # PSY,越高越好
    LOWER_BETTER = [8.77, 12.75, 16.14, 20.21, 24.38]     # 死亡率,越低越好

    def test_higher_better_top_value_gets_a(self):
        assert grade(30.0, self.HIGHER_BETTER) == "A"

    def test_higher_better_bottom_value_gets_f(self):
        assert grade(10.0, self.HIGHER_BETTER) == "F"

    def test_lower_better_top_value_gets_a(self):
        assert grade(5.0, self.LOWER_BETTER) == "A"

    def test_lower_better_bottom_value_gets_f(self):
        assert grade(30.0, self.LOWER_BETTER) == "F"


class TestBoundary:
    """邊界採嚴格不等式:值等於切點時歸入較差的一級。

    依據:範例牧場離乳前死亡率 20.21 恰等於 75% 切點,官方判 E 而非 D。
    """

    LOWER_BETTER = [8.77, 12.75, 16.14, 20.21, 24.38]
    HIGHER_BETTER = [25.65, 23.22, 21.11, 19.11, 18.24]

    def test_lower_better_exactly_on_cut_goes_worse(self):
        assert grade(20.21, self.LOWER_BETTER) == "E"

    def test_lower_better_just_below_cut_goes_better(self):
        assert grade(20.20, self.LOWER_BETTER) == "D"

    def test_higher_better_exactly_on_cut_goes_worse(self):
        # 21.11 是 50% 切點,等於切點應歸 D 而非 C
        assert grade(21.11, self.HIGHER_BETTER) == "D"

    def test_higher_better_just_above_cut_goes_better(self):
        assert grade(21.12, self.HIGHER_BETTER) == "C"

    def test_exactly_on_worst_cut_gets_f(self):
        # 等於 90% 切點,不優於它,應為 F
        assert grade(18.24, self.HIGHER_BETTER) == "F"


class TestDeterminism:
    """規格 US-3 驗收條件 2:同樣輸入必須永遠得到同樣結果。"""

    def test_repeated_calls_identical(self):
        pcts = [25.65, 23.22, 21.11, 19.11, 18.24]
        results = {grade(20.63, pcts) for _ in range(100)}
        assert results == {"D"}


class TestInvalidInput:
    """壞輸入要明確報錯,不能默默回一個看似合理的級距。"""

    PCTS = [25.65, 23.22, 21.11, 19.11, 18.24]

    def test_rejects_none_value(self):
        with pytest.raises((TypeError, ValueError)):
            grade(None, self.PCTS)

    def test_rejects_wrong_percentile_count(self):
        with pytest.raises(ValueError):
            grade(20.0, [25.65, 23.22, 21.11])

    def test_rejects_non_monotonic_percentiles(self):
        # 百分位必須單調,否則資料檔有錯,應該炸掉而不是猜
        with pytest.raises(ValueError):
            grade(20.0, [25.65, 19.11, 21.11, 23.22, 18.24])


class TestBandsDefinition:
    """分級定義必須與官方年報一致:A<10%, B 10~25%, C 25~50%, D 50~75%, E 75~90%, F>90%"""

    def test_five_cuts_produce_six_grades(self):
        assert [letter for _, letter in BANDS] == ["A", "B", "C", "D", "E"]
        assert len(BANDS) == 5  # 第六級 F 是 fallback

    def test_cut_percentages(self):
        assert [cut for cut, _ in BANDS] == [10, 25, 50, 75, 90]


class TestGradeAll:
    """批次評級:規格 US-2 驗收條件 1 —— 只填部分欄位時,其餘標示未評。"""

    METRICS = {
        "psy": {"percentiles": [25.65, 23.22, 21.11, 19.11, 18.24], "gradable": True},
        "farrowing_rate": {"percentiles": [87.47, 82.37, 75.19, 69.71, 63.42], "gradable": True},
        "total_services": {"percentiles": None, "gradable": False},
    }

    def test_grades_only_provided_values(self):
        result = grade_all({"psy": 20.63}, self.METRICS)
        assert result["psy"].grade == "D"
        assert "farrowing_rate" not in result

    def test_skips_non_gradable_metrics(self):
        result = grade_all({"psy": 20.63, "total_services": 1181}, self.METRICS)
        assert "total_services" not in result

    def test_ignores_unknown_keys(self):
        result = grade_all({"psy": 20.63, "not_a_metric": 5}, self.METRICS)
        assert set(result) == {"psy"}

    def test_result_carries_value_and_percentile_band(self):
        result = grade_all({"psy": 20.63}, self.METRICS)
        assert result["psy"].value == 20.63
        assert result["psy"].percentile_band == (50, 75)
