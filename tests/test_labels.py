"""顯示文字測試。

這一層從 benchmark.py 拆出來(SRP):改措辭不該動到資料存取模組。
"""

import pytest

from core.labels import (
    grade_label,
    reportable_disclaimer,
    sample_size_note,
    shortfall_note,
    source_label,
    upstream_note,
)


class TestSourceLabel:
    """憲法第三條:顯示常模數字時必須一併標註來源與年份。"""

    def test_contains_year_and_farm_count(self):
        label = source_label()
        assert "2025" in label
        assert "110" in label

    def test_is_stable(self):
        assert source_label() == source_label()


class TestSampleSizeNote:
    """憲法第三條:樣本數不足者必須顯示實際樣本數,不得隱藏。"""

    @pytest.mark.parametrize("key,expected_count", [
        ("stillborn", "108"),
        ("mummified", "103"),
        ("sow_deaths", "105"),
        ("boars_inventory", "104"),
    ])
    def test_partial_sample_disclosed(self, key, expected_count):
        note = sample_size_note(key)
        assert expected_count in note

    def test_full_sample_produces_no_note(self):
        assert sample_size_note("psy") == ""


class TestGradeLabel:
    @pytest.mark.parametrize("letter,expected_range", [
        ("A", "0~10"),
        ("B", "10~25"),
        ("C", "25~50"),
        ("D", "50~75"),
        ("E", "75~90"),
    ])
    def test_shows_percentile_range(self, letter, expected_range):
        assert expected_range in grade_label(letter)

    def test_worst_grade_shows_bottom_band(self):
        label = grade_label("F")
        assert "F" in label
        assert "10" in label  # 後 10%


class TestShortfallNote:
    def test_explains_the_basis(self):
        """排序依據要讓使用者知道是統計距離,不是主觀判斷。"""
        note = shortfall_note()
        assert "標準差" in note


class TestUpstreamNote:
    def test_marks_as_interpretation(self):
        note = upstream_note()
        assert "覆核" in note


