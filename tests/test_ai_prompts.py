"""提示詞測試。

提示詞單獨成一個模組(SRP):它因「顧問語氣與要求改變」而修改,
與「怎麼呼叫 CLI」是兩回事。

這些測試不呼叫 AI,只驗證送出去的文字內容正確。
"""

import pytest

from ai.prompts import (
    ADVICE_SYSTEM_PROMPT,
    DISEASE_SYSTEM_PROMPT,
    build_farm_context,
    build_history_context,
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


class TestDiseasePrompt:
    """憲法第一條:用藥建議必須帶風險評估與獸醫確診提醒。"""

    def test_requires_risk_assessment(self):
        for term in ("休藥期", "抗藥性"):
            assert term in DISEASE_SYSTEM_PROMPT

    def test_requires_vet_referral(self):
        assert "獸醫" in DISEASE_SYSTEM_PROMPT

    def test_forbids_diagnostic_certainty(self):
        """憲法第一條:不得輸出確診語氣。"""
        assert "確診" in DISEASE_SYSTEM_PROMPT

    def test_specifies_traditional_chinese(self):
        assert "繁體中文" in DISEASE_SYSTEM_PROMPT

    def test_each_answer_stands_alone(self):
        """支援追問後,每則回答仍必須自成完整內容。

        使用者可能只看最後一則就去用藥,不能出現「詳見上一則」這種回答。
        """
        assert "獨立看懂" in DISEASE_SYSTEM_PROMPT


class TestAdvicePrompt:
    """憲法第二條:AI 只解讀已算好的級距,不自己算。"""

    def test_forbids_recomputing_grades(self):
        assert "不要自行計算" in ADVICE_SYSTEM_PROMPT or "已計算" in ADVICE_SYSTEM_PROMPT

    def test_asks_for_actionable_advice(self):
        assert "具體" in ADVICE_SYSTEM_PROMPT


class TestFarmContext:
    """US-1 驗收條件 7:健檢弱項可作為背景資訊送進疾病諮詢。"""

    WEAK = [
        {"name": "離乳前死亡率", "grade": "E", "shortfall_sd": 0.65},
        {"name": "分娩率", "grade": "D", "shortfall_sd": 0.42},
    ]

    def test_empty_when_no_health_check(self):
        """沒做過健檢的使用者也要能用,不得因此擋下。"""
        assert build_farm_context([]) == ""
        assert build_farm_context(None) == ""

    def test_includes_weak_metric_names(self):
        context = build_farm_context(self.WEAK)
        assert "離乳前死亡率" in context
        assert "分娩率" in context

    def test_marks_as_background_not_instruction(self):
        """必須讓 AI 知道這是背景參考,不是使用者的提問內容。"""
        context = build_farm_context(self.WEAK)
        assert "背景" in context or "參考" in context

    def test_limits_to_top_items(self):
        """弱項可能很多,全部塞進去會稀釋提問本身。

        數的是條列行數,不是名稱出現次數 —— 標題裡也可能含有同樣的字。
        """
        many = [{"name": f"甲{i}", "grade": "F", "shortfall_sd": 1.0} for i in range(20)]
        context = build_farm_context(many)
        bullet_lines = [ln for ln in context.splitlines() if ln.startswith("- ")]
        assert len(bullet_lines) == 5

    def test_keeps_the_worst_items(self):
        """帶進去的應該是排在最前面的(落後最嚴重的)那幾項。"""
        many = [{"name": f"甲{i}", "grade": "F", "shortfall_sd": 1.0} for i in range(20)]
        context = build_farm_context(many)
        assert "甲0" in context
        assert "甲19" not in context

    def test_deterministic(self):
        assert build_farm_context(self.WEAK) == build_farm_context(self.WEAK)
