"""改善建議的組裝。

v2 只剩一條 AI 路徑:健檢弱項 → 改善建議 → 追問。
疾病諮詢與拍照辨識已隨 v1 移除。
"""

import pytest

from ai.consultant import Consultant
from ai.prompts import ADVICE_SYSTEM_PROMPT
from ai.transport import FakeTransport, NotLoggedIn, QuotaExceeded


def _consultant(chunks=None, error=None):
    return Consultant(transport=FakeTransport(chunks=chunks or ["建議內容"], error=error))


class TestImprovementAdvice:
    """生產健檢的改善建議:AI 只解讀已算好的結果。"""

    def test_sends_precomputed_grades(self):
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        list(c.advise([
            {"name": "離乳前死亡率", "grade": "E", "shortfall_sd": 0.65,
             "improvement": "加強巡視"},
        ]))
        assert "離乳前死亡率" in transport.last_prompt
        assert "E" in transport.last_prompt

    def test_empty_weaknesses_needs_no_ai_call(self):
        """沒有弱項就沒必要花額度問 AI。"""
        transport = FakeTransport(chunks=["不該被呼叫"])
        c = Consultant(transport=transport)
        assert list(c.advise([])) == []
        assert transport.last_prompt is None

    def test_uses_advice_persona_not_disease_persona(self):
        """必須是 ADVICE_SYSTEM_PROMPT,不能因為加了追問功能就不小心
        接錯 persona —— 使用者問的是經營建議,不是在問診。
        """
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        list(c.advise([{"name": "PSY", "grade": "F", "shortfall_sd": 1.0}]))
        assert transport.last_system == ADVICE_SYSTEM_PROMPT


class TestReferenceFactorsReachTheModel:
    """生產健檢的其他參考因素。跟弱項不同,這些是使用者自己說的,
    不是系統算出來的,型別/長度一樣要在伺服器端收斂。
    """

    WEAK = [{"name": "PSY", "grade": "F", "shortfall_sd": 1.0}]

    def test_factor_reaches_the_model(self):
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, reference_factors=[
            {"name": "豬舍類型", "value": "開放式豬舍"},
        ]))
        assert "豬舍類型" in transport.last_prompt
        assert "開放式豬舍" in transport.last_prompt

    def test_works_without_factors(self):
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK))
        assert transport.last_prompt is not None

    def test_non_list_factors_ignored_not_raised(self):
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, reference_factors="不是陣列"))  # 不拋例外即為通過

    def test_entries_missing_name_are_dropped(self):
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, reference_factors=[{"value": "沒有名字的因素"}]))
        assert "沒有名字的因素" not in transport.last_prompt

    def test_overlong_value_is_truncated(self):
        import config
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, reference_factors=[
            {"name": "備註", "value": "字" * (config.MAX_FACTOR_CHARS + 20)},
        ]))
        assert "字" * (config.MAX_FACTOR_CHARS + 1) not in transport.last_prompt

    def test_factors_come_before_the_instruction(self):
        """實際踩過的 bug:參考因素被接在「請針對這些項目給出改善建議」
        之後,模型當成講完才補的附註,建議內容完全沒反映牧場實際條件
        (使用者回報「AI 還是沒有跟健檢數字一起分析」)。

        背景一律要排在指令前面 —— consult() 就是這樣組的。
        """
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, reference_factors=[
            {"name": "豬舍類型", "value": "開放式豬舍"},
        ]))
        prompt = transport.last_prompt
        assert prompt.index("豬舍類型") < prompt.index("請針對這些項目給出改善建議")

    def test_tells_the_model_to_actually_use_them(self):
        """只是「附上」不夠 —— 要明講建議必須貼著這些條件寫,
        否則模型傾向回一份換到別場也通用的答案。
        """
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, reference_factors=[
            {"name": "豬舍類型", "value": "開放式豬舍"},
        ]))
        assert "納入考量" in transport.last_prompt

    def test_over_limit_count_is_truncated(self):
        import config
        transport = FakeTransport(chunks=["建議"])
        c = Consultant(transport=transport)
        many = [{"name": f"因素{i}", "value": f"值{i}"}
                for i in range(config.MAX_REFERENCE_FACTORS + 5)]
        list(c.advise(self.WEAK, reference_factors=many))
        assert f"因素{config.MAX_REFERENCE_FACTORS + 4}" not in transport.last_prompt


class TestAdviseFollowUp:
    """健檢改善建議的追問。延續同一個 persona 討論,不是重新生成整份建議。"""

    WEAK = [{"name": "PSY", "grade": "F", "shortfall_sd": 1.0}]

    def test_question_reaches_the_model(self):
        transport = FakeTransport(chunks=["回覆"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, question="這幾項應該先做哪一個?"))
        assert "這幾項應該先做哪一個" in transport.last_prompt

    def test_still_includes_the_original_weaknesses(self):
        """追問時不能只有問題、丟掉原本的健檢結果脈絡。"""
        transport = FakeTransport(chunks=["回覆"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, question="怎麼做?"))
        assert "PSY" in transport.last_prompt

    def test_history_reaches_the_model(self):
        transport = FakeTransport(chunks=["回覆"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, question="還有呢?", history=[
            {"role": "assistant", "content": "先處理 PSY 的問題"},
        ]))
        assert "先處理 PSY 的問題" in transport.last_prompt

    def test_still_uses_advice_persona(self):
        """追問也不能切換成疾病諮詢的語氣。"""
        transport = FakeTransport(chunks=["回覆"])
        c = Consultant(transport=transport)
        list(c.advise(self.WEAK, question="怎麼做?"))
        assert transport.last_system == ADVICE_SYSTEM_PROMPT

    def test_empty_question_rejected(self):
        with pytest.raises(ValueError):
            list(Consultant(transport=FakeTransport(chunks=["x"])).advise(
                self.WEAK, question="   "
            ))

    def test_overlong_question_rejected(self):
        import config
        with pytest.raises(ValueError):
            list(Consultant(transport=FakeTransport(chunks=["x"])).advise(
                self.WEAK, question="問" * (config.MAX_QUESTION_CHARS + 1)
            ))

    def test_non_string_question_rejected_not_crashed(self):
        with pytest.raises(ValueError):
            list(Consultant(transport=FakeTransport(chunks=["x"])).advise(
                self.WEAK, question={"a": "b"}
            ))

    def test_no_weaknesses_no_ai_call_even_with_question(self):
        """沒有弱項就沒有可以討論的東西,不該為此花額度。"""
        transport = FakeTransport(chunks=["不該被呼叫"])
        c = Consultant(transport=transport)
        assert list(c.advise([], question="還有呢?")) == []
        assert transport.last_prompt is None


