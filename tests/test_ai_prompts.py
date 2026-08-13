"""提示詞測試。

提示詞單獨成一個模組(SRP):它因「顧問語氣與要求改變」而修改,
與「怎麼呼叫 CLI」是兩回事。

這些測試不呼叫 AI,只驗證送出去的文字內容正確。
"""

import pytest

from ai.prompts import (
    ADVICE_SYSTEM_PROMPT,
    build_history_context,
    build_reference_factors,
)


class TestHistoryContext:
    """對話歷史的呈現方式。歷史來自瀏覽器,格式不可信。"""

    HISTORY = [
        {"role": "user", "content": "保育豬咳嗽怎麼辦"},
        {"role": "assistant", "content": "可能是黴漿菌肺炎"},
    ]

    def test_empty_when_no_history(self):
        assert build_history_context([]) == ""
        assert build_history_context(None) == ""

    def test_includes_both_sides(self):
        context = build_history_context(self.HISTORY)
        assert "保育豬咳嗽怎麼辦" in context
        assert "黴漿菌肺炎" in context

    def test_labels_who_said_what(self):
        """沒有標明角色,模型會分不清哪句是自己說的。"""
        context = build_history_context(self.HISTORY)
        assert "使用者" in context
        assert "顧問" in context

    def test_marks_where_history_ends(self):
        """必須清楚區隔歷史與本次提問,否則模型會把舊問題當成新問題回答。"""
        context = build_history_context(self.HISTORY)
        assert "以上為歷史紀錄" in context

    def test_skips_blank_entries(self):
        context = build_history_context([{"role": "user", "content": "   "}])
        assert context == ""


class TestAdvicePrompt:
    """憲法第二條:AI 只解讀已算好的級距,不自己算。"""

    def test_forbids_recomputing_grades(self):
        assert "不要自行計算" in ADVICE_SYSTEM_PROMPT or "已計算" in ADVICE_SYSTEM_PROMPT

    def test_asks_for_actionable_advice(self):
        assert "具體" in ADVICE_SYSTEM_PROMPT


class TestReferenceFactors:
    """生產健檢的「其他參考因素」。跟弱項不同 —— 弱項是系統算出來的
    (憲法第二條),這裡完全是使用者自己說的,AI 只能拿來當背景參考。
    """

    FACTORS = [
        {"name": "豬舍類型", "value": "開放式豬舍,夏季悶熱"},
        {"name": "飼養規模", "value": "母豬 300 頭"},
    ]

    def test_empty_when_no_factors(self):
        assert build_reference_factors([]) == ""
        assert build_reference_factors(None) == ""

    def test_includes_name_and_value(self):
        context = build_reference_factors(self.FACTORS)
        assert "豬舍類型" in context
        assert "開放式豬舍,夏季悶熱" in context
        assert "飼養規模" in context

    def test_marks_as_reference_not_grading_basis(self):
        """不能讓 AI 誤以為這些是系統評出來的級距,或拿來重新計算什麼。"""
        context = build_reference_factors(self.FACTORS)
        assert "參考" in context
        assert "不是評級依據" in context

    def test_skips_entries_without_value(self):
        """只有名字沒有說明的因素,對 AI 沒有資訊量,不必送進提示詞。"""
        context = build_reference_factors([{"name": "豬舍類型", "value": ""}])
        assert context == ""

    def test_all_blank_values_results_in_empty_string(self):
        context = build_reference_factors([
            {"name": "甲", "value": ""}, {"name": "乙", "value": ""},
        ])
        assert context == ""

    def test_deterministic(self):
        assert build_reference_factors(self.FACTORS) == build_reference_factors(self.FACTORS)
