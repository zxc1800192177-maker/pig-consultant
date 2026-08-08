"""常模資料檔測試。

資料檔錯了,底下所有計算都跟著錯,而且錯得很安靜。
這裡驗證資料本身的完整性,以及它與官方報表數字一致。
"""

import pytest

from core.benchmark import BENCHMARK, get_metric, gradable_metrics, upstream_of
from core.labels import source_label


class TestSource:
    """憲法第三條:常模數據必須標註來源與年份。"""

    def test_has_source_metadata(self):
        src = BENCHMARK["source"]
        assert src["year"] == 2025
        assert src["farms"] == 110

    def test_source_label_shows_year_and_farm_count(self):
        label = source_label()
        assert "2025" in label
        assert "110" in label


class TestMetricIntegrity:
    def test_all_keys_unique(self):
        keys = [m["key"] for m in BENCHMARK["metrics"]]
        assert len(keys) == len(set(keys))

    @pytest.mark.parametrize("metric", BENCHMARK["metrics"], ids=lambda m: m["key"])
    def test_metric_has_required_fields(self, metric):
        for field in ("key", "name", "gradable", "sample_size", "mean", "definition"):
            assert field in metric, f"{metric.get('key')} 缺少欄位 {field}"

    @pytest.mark.parametrize("metric", BENCHMARK["metrics"], ids=lambda m: m["key"])
    def test_gradable_metrics_have_five_percentiles(self, metric):
        if metric["gradable"]:
            assert len(metric["percentiles"]) == 5, f"{metric['key']} 百分位數量錯誤"

    @pytest.mark.parametrize("metric", BENCHMARK["metrics"], ids=lambda m: m["key"])
    def test_percentiles_are_monotonic(self, metric):
        """百分位由最佳排到最差,必須單調 —— 不單調代表抄錯。"""
        if not metric["gradable"]:
            return
        p = metric["percentiles"]
        ascending = all(a < b for a, b in zip(p, p[1:]))
        descending = all(a > b for a, b in zip(p, p[1:]))
        assert ascending or descending, f"{metric['key']} 百分位不單調:{p}"

    @pytest.mark.parametrize("metric", BENCHMARK["metrics"], ids=lambda m: m["key"])
    def test_non_gradable_metrics_have_no_percentiles(self, metric):
        """規模型指標不得帶百分位,避免日後有人不小心拿去評級。"""
        if not metric["gradable"]:
            assert "percentiles" not in metric, f"{metric['key']} 不可評級卻帶了百分位"


class TestSampleSizeDisclosure:
    """憲法第三條:樣本數不足者必須能被辨識出來。"""

    @pytest.mark.parametrize("key,expected", [
        ("stillborn", 108),
        ("stillborn_per_litter", 108),
        ("mummified", 103),
        ("mummified_per_litter", 103),
        ("sow_deaths", 105),
        ("sow_death_rate", 105),
        ("boars_inventory", 104),
    ])
    def test_partial_sample_sizes_preserved(self, key, expected):
        assert get_metric(key)["sample_size"] == expected

    def test_full_sample_metrics_are_110(self):
        assert get_metric("psy")["sample_size"] == 110


class TestValuesMatchOfficialReport:
    """抽驗數值與 2025 年報一致。"""

    @pytest.mark.parametrize("key,mean,percentiles", [
        ("psy", 21.52, [25.65, 23.22, 21.11, 19.11, 18.24]),
        ("farrowing_rate", 75.41, [87.47, 82.37, 75.19, 69.71, 63.42]),
        ("preweaning_mortality", 16.54, [8.77, 12.75, 16.14, 20.21, 24.38]),
        ("npd", 62.07, [39.89, 47.93, 60.68, 73.78, 83.65]),
    ])
    def test_matches_report(self, key, mean, percentiles):
        m = get_metric(key)
        assert m["mean"] == mean
        assert m["percentiles"] == percentiles


class TestGradableSet:
    def test_gradable_count_is_eighteen(self):
        """年報中有百分位、可評級的指標共 18 項。"""
        assert len(gradable_metrics()) == 18

    def test_scale_metrics_excluded(self):
        keys = {m["key"] for m in gradable_metrics()}
        assert "total_services" not in keys
        assert "total_born" not in keys


class TestDomainKnowledgeLivesInData:
    """SRP:領域知識(權重、上游關係)集中在資料檔,不散落在程式碼裡。

    這兩節都需要領域專家覆核。分散在兩個地方會讓專家漏看其中一邊。
    """

    def test_upstream_section_exists_in_data_file(self):
        assert "upstream" in BENCHMARK

    def test_upstream_keys_reference_known_metrics(self):
        known = {m["key"] for m in BENCHMARK["metrics"]}
        for key, targets in BENCHMARK["upstream"].items():
            if key.startswith("_"):
                continue
            assert key in known, f"上游表指向不存在的指標 {key}"
            for target in targets:
                assert target in known, f"{key} 的上游 {target} 不存在"

    def test_upstream_of_returns_list(self):
        assert upstream_of("weaned_per_sow") == [
            "live_born_per_litter", "preweaning_mortality"
        ]

    def test_upstream_of_unknown_key_is_empty(self):
        assert upstream_of("not_a_metric") == []

    def test_no_metric_is_its_own_upstream(self):
        """自己是自己的上游會造成建議繞圈。"""
        for key, targets in BENCHMARK["upstream"].items():
            if key.startswith("_"):
                continue
            assert key not in targets, f"{key} 的上游包含自己"

    def test_provisional_notice_present(self):
        """憲法第三條:暫定值必須在資料檔中標明。"""
        assert "_domain_knowledge_notice" in BENCHMARK


class TestNoHandTunedWeights:
    """排序依據必須可追溯到常模表,不得回歸人為指定的權重。

    初版用過 3/2/1 的手工權重,因為無法驗證而移除。
    這條測試防止它被重新加回來。
    """

    def test_impact_weights_section_removed(self):
        assert "impact_weights" not in BENCHMARK

    def test_gradable_metrics_carry_mean_and_sd(self):
        """標準差距離的計算依賴這兩個欄位,缺一不可。"""
        for metric in BENCHMARK["metrics"]:
            if not metric["gradable"]:
                continue
            assert metric["sd"] > 0, f"{metric['key']} 缺少標準差,無法計算落後程度"
            assert metric["mean"] > 0, f"{metric['key']} 缺少平均值"


class TestGetMetric:
    def test_unknown_key_raises(self):
        with pytest.raises(KeyError):
            get_metric("not_a_real_metric")
