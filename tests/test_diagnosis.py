"""弱項排序測試 —— 規格 US-4。

排序依據改為「距離全國平均幾個標準差」,取代初版人為指定的 3/2/1 權重。
理由:這份排序會影響牧場主的資源配置,依據必須可追溯到常模表,不能是猜測。
"""

import pytest

from core.benchmark import get_metric, metrics_index
from core.diagnosis import GRADE_RANK, rank_weaknesses, shortfall_sd
from core.grading import GradeResult, grade_all


def _graded(**pairs):
    """把 {key: (值, 級距)} 轉成 grade_all() 的輸出格式。"""
    return {
        key: GradeResult(value=value, grade=letter, percentile_band=(0, 10))
        for key, (value, letter) in pairs.items()
    }


class TestGradeRank:
    def test_a_is_best_f_is_worst(self):
        assert GRADE_RANK["A"] == 0
        assert GRADE_RANK["F"] == 5

    def test_all_six_grades_covered(self):
        assert set(GRADE_RANK) == {"A", "B", "C", "D", "E", "F"}


class TestShortfall:
    """標準差距離必須與常模表一致,而且方向要對。"""

    def test_higher_better_below_mean_is_positive(self):
        """PSY 越高越好。低於平均 -> 正的落後值。"""
        metric = get_metric("psy")           # mean 21.52, sd 2.770
        assert shortfall_sd(20.63, metric) == pytest.approx(0.321, abs=0.01)

    def test_higher_better_above_mean_is_negative(self):
        metric = get_metric("psy")
        assert shortfall_sd(25.00, metric) < 0

    def test_lower_better_above_mean_is_positive(self):
        """離乳前死亡率越低越好。高於平均 -> 正的落後值。"""
        metric = get_metric("preweaning_mortality")   # mean 16.54, sd 5.660
        assert shortfall_sd(20.21, metric) == pytest.approx(0.648, abs=0.01)

    def test_lower_better_below_mean_is_negative(self):
        metric = get_metric("preweaning_mortality")
        assert shortfall_sd(10.0, metric) < 0

    def test_at_mean_is_zero(self):
        metric = get_metric("psy")
        assert shortfall_sd(metric["mean"], metric) == pytest.approx(0.0)

    def test_direction_derived_not_configured(self):
        """方向來自百分位,與 grading 同一套邏輯,不另外維護方向表。"""
        higher = get_metric("psy")
        lower = get_metric("npd")
        assert shortfall_sd(higher["mean"] - higher["sd"], higher) == pytest.approx(1.0)
        assert shortfall_sd(lower["mean"] + lower["sd"], lower) == pytest.approx(1.0)


class TestOrdering:
    def test_furthest_from_benchmark_first(self):
        result = rank_weaknesses(_graded(
            psy=(20.63, "D"),                  # 約 0.32 SD
            weaning_age=(21.97, "F"),          # 約 2.96 SD
        ))
        assert result[0].key == "weaning_age"

    def test_descending_shortfall(self):
        result = rank_weaknesses(_graded(
            psy=(20.63, "D"),
            weaning_age=(21.97, "F"),
            preweaning_mortality=(20.21, "E"),
        ))
        values = [w.shortfall_sd for w in result]
        assert values == sorted(values, reverse=True)

    def test_grade_alone_does_not_decide_order(self):
        """一個 D 級但離平均很遠的指標,應排在 E 級但很接近平均的之前。"""
        result = rank_weaknesses(_graded(
            lactation_days=(21.82, "D"),        # 約 2.54 SD,刻意標成 D
            preweaning_mortality=(16.60, "E"),  # 幾乎等於平均
        ))
        assert result[0].key == "lactation_days"


class TestWhatCounts:
    """弱項 = 低於全國中位數(D 級以下)。"""

    def test_good_grades_excluded(self):
        result = rank_weaknesses(_graded(
            psy=(25.0, "A"),
            litters_per_sow_year=(2.25, "B"),
            weaned_per_sow=(9.47, "D"),
        ))
        assert [w.key for w in result] == ["weaned_per_sow"]

    def test_grade_c_is_not_a_weakness(self):
        """C 級是「前 25~50%」,高於中位數,不該被叫使用者去改善。

        初版把 C 級算成弱項,導致清單出現「本場比全國平均好」的項目。
        """
        result = rank_weaknesses(_graded(live_born_per_sow_year=(27.00, "C")))
        assert result == []

    @pytest.mark.parametrize("grade", ["D", "E", "F"])
    def test_below_median_grades_count(self, grade):
        result = rank_weaknesses(_graded(weaned_per_sow=(9.0, grade)))
        assert len(result) == 1

    @pytest.mark.parametrize("grade", ["A", "B", "C"])
    def test_at_or_above_median_grades_do_not_count(self, grade):
        result = rank_weaknesses(_graded(weaned_per_sow=(11.0, grade)))
        assert result == []

    def test_above_mean_is_not_a_weakness_even_if_below_median(self):
        """偏態分布下,級距在中位數以下但數值高於平均是可能的。

        這種項目不該進改善清單 —— 否則畫面會出現
        「改善優先順序:優於全國平均」這種自相矛盾的內容。
        範例牧場的離乳到第一次配種間隔 7.05 天(全國均 7.38)就是這種情況。
        """
        result = rank_weaknesses(_graded(wean_to_service=(7.05, "D")))
        assert result == []

    def test_below_both_median_and_mean_counts(self):
        result = rank_weaknesses(_graded(wean_to_service=(9.5, "D")))
        assert len(result) == 1
        assert result[0].shortfall_sd > 0

    def test_every_listed_item_is_genuinely_behind(self):
        """不變式:清單中每一項的落後值都必須為正。"""
        graded = _graded(
            weaning_age=(21.97, "F"),
            wean_to_service=(7.05, "D"),
            preweaning_mortality=(20.21, "E"),
        )
        for item in rank_weaknesses(graded):
            assert item.shortfall_sd > 0, f"{item.name} 優於平均卻被列為弱項"

    def test_empty_when_all_good(self):
        assert rank_weaknesses(_graded(psy=(25.0, "A"))) == []

    def test_empty_input(self):
        assert rank_weaknesses({}) == []


class TestDownstream:
    """規格 US-4 驗收條件 3:指出改善這項會帶動哪些同樣落後的指標。"""

    def test_upstream_metric_lists_its_dependents(self):
        """離乳前死亡率是母豬平均離乳仔豬數的上游,兩者都落後時應標示連動。"""
        result = rank_weaknesses(_graded(
            preweaning_mortality=(24.0, "E"),
            weaned_per_sow=(9.0, "E"),
        ))
        item = next(w for w in result if w.key == "preweaning_mortality")
        assert "weaned_per_sow" in item.downstream

    def test_downstream_only_lists_weak_metrics(self):
        """下游表現良好時不列出 —— 沒有必要為了已經好的指標去改上游。"""
        result = rank_weaknesses(_graded(
            preweaning_mortality=(24.0, "E"),
            weaned_per_sow=(11.5, "A"),
        ))
        item = next(w for w in result if w.key == "preweaning_mortality")
        assert "weaned_per_sow" not in item.downstream


class TestPayload:
    def test_carries_name_unit_and_advice(self):
        result = rank_weaknesses(_graded(preweaning_mortality=(24.0, "E")))
        item = result[0]
        assert item.name == "離乳前死亡率"
        assert item.unit == "%"
        assert item.improvement


class TestDeterminism:
    def test_stable_order_across_runs(self):
        graded = _graded(
            weaned_per_sow=(9.47, "D"),
            preweaning_mortality=(20.21, "D"),
            farrowing_rate=(71.56, "D"),
            wean_to_service=(7.05, "D"),
        )
        orders = {tuple(w.key for w in rank_weaknesses(graded)) for _ in range(50)}
        assert len(orders) == 1, "排序必須穩定,不可每次不同"


class TestRealFarm:
    """範例牧場真實數據:排序結果必須說得出道理。"""

    VALUES = {
        "psy": 20.63, "litters_per_sow_year": 2.25, "npd": 59.99,
        "farrowing_rate": 71.56, "repeat_service_rate": 17.87,
        "wean_to_service": 7.05, "live_born_per_sow_year": 27.00,
        "total_born_per_litter": 13.29, "live_born_per_litter": 11.92,
        "preweaning_mortality": 20.21, "weaned_per_sow": 9.47,
        "weaned_per_litter": 9.79, "weaning_age": 21.97,
        "lactation_days": 21.82, "parity_sow_psy": 20.69,
        "farrowing_index": 2.42, "gestation_days": 114.08,
        "boars_inventory": 11.00,
    }

    @pytest.fixture(scope="class")
    def ranked(self):
        return rank_weaknesses(grade_all(self.VALUES, metrics_index()))

    def test_produces_a_ranking(self, ranked):
        assert ranked

    def test_top_item_is_furthest_outlier(self, ranked):
        """離乳日齡 21.97 vs 全國平均 27.42(sd 1.84)差近 3 個標準差,
        是這場最極端的偏離,應排第一。"""
        assert ranked[0].key == "weaning_age"
        assert ranked[0].shortfall_sd > 2.5

    def test_strong_metrics_absent(self, ranked):
        """B 級的年產胎數、分娩指數不是弱項。"""
        keys = {w.key for w in ranked}
        assert "litters_per_sow_year" not in keys
        assert "farrowing_index" not in keys

    def test_above_median_metrics_absent(self, ranked):
        """C 級的非生產天數與年產活仔數高於中位數,不該出現在改善清單。"""
        keys = {w.key for w in ranked}
        assert "npd" not in keys
        assert "live_born_per_sow_year" not in keys

    def test_no_item_claims_to_be_above_average(self, ranked):
        """整份清單不得出現「優於平均」的項目。"""
        for item in ranked:
            assert item.shortfall_sd > 0

    def test_wean_to_service_excluded(self, ranked):
        """本場 7.05 天優於全國平均 7.38,雖是 D 級但不是弱項。"""
        assert "wean_to_service" not in {w.key for w in ranked}

    def test_every_item_traceable_to_benchmark(self, ranked):
        """每一筆的落後值都必須能用常模表的 mean/sd 重算出來。"""
        for item in ranked:
            metric = get_metric(item.key)
            expected = shortfall_sd(self.VALUES[item.key], metric)
            assert item.shortfall_sd == pytest.approx(expected, abs=0.001)
