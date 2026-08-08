"""輸入驗證測試 —— 規格 US-2 驗收條件 2、3。

決策:明顯錯的擋下(errors),可疑的只提醒(warnings)。
理由是填錯一個數字就整份不給算會很煩,但 300% 的分娩率算出來的級距毫無意義。
"""

import pytest

from core.metrics import ValidationReport, validate


class TestObviousErrorsBlock:
    """超出合理範圍 = 明顯填錯,擋下不給算。"""

    @pytest.mark.parametrize("key,value", [
        ("farrowing_rate", 300),          # 分娩率不可能超過 100%
        ("farrowing_rate", -5),
        ("preweaning_mortality", 95),     # 上限 60
        ("gestation_days", 5),            # 生理範圍 108~120
        ("gestation_days", 200),
        ("litters_per_sow_year", 8),      # 上限 2.6
        ("total_born_per_litter", 40),    # 上限 25
        ("npd", -1),
    ])
    def test_out_of_range_is_error(self, key, value):
        report = validate({key: value})
        assert not report.ok
        assert any(e.key == key for e in report.errors)

    @pytest.mark.parametrize("key,value", [
        ("farrowing_rate", 71.56),
        ("preweaning_mortality", 20.21),
        ("gestation_days", 114.08),
        ("litters_per_sow_year", 2.25),
        ("psy", 20.63),
    ])
    def test_normal_value_passes(self, key, value):
        report = validate({key: value})
        assert report.ok
        assert not report.errors

    def test_error_message_names_the_field_and_range(self):
        report = validate({"farrowing_rate": 300})
        msg = report.errors[0].message
        assert "分娩率" in msg
        assert "100" in msg


class TestNonNumeric:
    @pytest.mark.parametrize("value", ["abc", "", None, [], {}])
    def test_non_numeric_is_error(self, value):
        report = validate({"farrowing_rate": value})
        assert not report.ok

    def test_numeric_string_is_accepted(self):
        """使用者從表單送來的常是字串,不該因此擋下。"""
        report = validate({"farrowing_rate": "71.56"})
        assert report.ok

    def test_boolean_is_rejected(self):
        """True 在 Python 裡是 1,不擋會靜默算出錯誤結果。"""
        report = validate({"farrowing_rate": True})
        assert not report.ok


class TestCrossFieldWarnings:
    """欄位互相矛盾 = 可疑但可能是真的,只提醒不擋。"""

    def test_live_born_exceeding_total_born_warns(self):
        report = validate({
            "total_born_per_litter": 12.0,
            "live_born_per_litter": 13.0,   # 活仔數不該大於總仔數
        })
        assert report.ok, "矛盾只提醒,不應擋下"
        assert report.warnings
        assert any("活仔" in w.message for w in report.warnings)

    def test_weaned_exceeding_live_born_warns(self):
        report = validate({
            "live_born_per_litter": 11.0,
            "weaned_per_sow": 12.0,
        })
        assert report.ok
        assert report.warnings

    def test_consistent_values_produce_no_warning(self):
        report = validate({
            "total_born_per_litter": 13.29,
            "live_born_per_litter": 11.92,
            "weaned_per_sow": 9.47,
        })
        assert report.ok
        assert not report.warnings

    def test_real_farm_data_produces_no_warning(self):
        """迴歸測試:範例牧場的真實數據不得被誤判為矛盾。

        年報公式為 (365.25 − 非生產天數) / (哺乳 + 懷孕),漏掉非生產天數那段
        會讓每份填寫正確的表單都跳出假警告。
        (365.25 − 59.99) / (21.82 + 114.08) = 2.2462,與填報值 2.25 相差 0.17%
        """
        report = validate({
            "litters_per_sow_year": 2.25,
            "lactation_days": 21.82,
            "gestation_days": 114.08,
            "npd": 59.99,
        })
        assert report.ok
        assert not report.warnings, f"真實資料不應觸發警告:{report.warnings}"

    def test_litters_per_year_inconsistent_warns(self):
        """(365.25 − 60) / 142 = 2.15,填 2.60 相差約 21% -> 應提醒。"""
        report = validate({
            "litters_per_sow_year": 2.60,
            "lactation_days": 27.0,
            "gestation_days": 115.0,
            "npd": 60.0,
        })
        assert report.ok
        assert any("年產胎數" in w.message for w in report.warnings)

    def test_litters_check_skipped_without_npd(self):
        """缺非生產天數時無法套用年報公式,應跳過檢查而非用錯誤的公式硬算。"""
        report = validate({
            "litters_per_sow_year": 2.25,
            "lactation_days": 21.82,
            "gestation_days": 114.08,
        })
        assert not report.warnings


class TestPartialInput:
    """規格 US-2 驗收條件 1:只填部分欄位也要能用。"""

    def test_empty_input_is_valid(self):
        assert validate({}).ok

    def test_single_field_is_valid(self):
        assert validate({"psy": 20.63}).ok

    def test_cross_check_skipped_when_counterpart_missing(self):
        """只填活仔數、沒填總仔數時,不該因為無法比較就報警。"""
        report = validate({"live_born_per_litter": 11.92})
        assert report.ok
        assert not report.warnings


class TestUnknownKeys:
    def test_unknown_key_is_ignored(self):
        report = validate({"not_a_metric": 999})
        assert report.ok


class TestReportShape:
    def test_ok_is_false_when_any_error(self):
        report = validate({"farrowing_rate": 300, "psy": 20.63})
        assert not report.ok

    def test_warnings_do_not_affect_ok(self):
        report = validate({
            "total_born_per_litter": 12.0,
            "live_born_per_litter": 13.0,
        })
        assert report.ok
        assert report.warnings

    def test_returns_validation_report(self):
        assert isinstance(validate({}), ValidationReport)

    def test_cleaned_values_are_numeric(self):
        """驗證通過後要給出可直接計算的數值,字串已轉成 float。"""
        report = validate({"farrowing_rate": "71.56"})
        assert report.cleaned["farrowing_rate"] == pytest.approx(71.56)
