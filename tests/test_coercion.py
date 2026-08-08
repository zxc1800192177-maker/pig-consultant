"""輸入轉換測試。

這一層從 metrics.py 拆出來(SRP),因為它會隨「輸入來源」改變而變動,
而合理範圍的定義不該跟著動。v0.2 要支援 PDF 解析時,新增的是這裡的規則。
"""

import pytest

from core.coercion import to_number


class TestNumbers:
    @pytest.mark.parametrize("value,expected", [
        (71.56, 71.56),
        (12, 12.0),
        (0, 0.0),
        (-5, -5.0),
    ])
    def test_passes_through(self, value, expected):
        assert to_number(value) == pytest.approx(expected)


class TestStrings:
    @pytest.mark.parametrize("value,expected", [
        ("71.56", 71.56),
        ("  71.56  ", 71.56),
        ("12", 12.0),
        ("-5.5", -5.5),
    ])
    def test_plain_numeric_string(self, value, expected):
        assert to_number(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value,expected", [
        ("1,537.45", 1537.45),      # 報表複製貼上會帶千分位
        ("15,826", 15826.0),
        ("71.56%", 71.56),          # 百分比欄位可能帶符號
        ("７１.５６", 71.56),         # 全形數字
    ])
    def test_real_world_formats(self, value, expected):
        assert to_number(value) == pytest.approx(expected)


class TestRejected:
    @pytest.mark.parametrize("value", [
        None, "", "   ", "abc", "12abc", [], {}, (), "1.2.3",
    ])
    def test_returns_none(self, value):
        assert to_number(value) is None

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_rejected(self, value):
        """True 在 Python 裡等於 1,放行會靜默算出一個看似合理的級距。"""
        assert to_number(value) is None
