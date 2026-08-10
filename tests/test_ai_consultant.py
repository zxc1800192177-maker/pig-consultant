"""諮詢流程測試。

這一層把「確定性的部分」與「AI 生成的部分」組起來。
關鍵驗證:確定性的部分(通報須知)不得依賴 AI —— AI 掛了它照樣要在。
"""

import pytest

from ai.consultant import Consultant
from ai.prompts import ADVICE_SYSTEM_PROMPT
from ai.transport import FakeTransport, NotLoggedIn, QuotaExceeded


def _consultant(chunks=None, error=None):
    return Consultant(transport=FakeTransport(chunks=chunks or ["建議內容"], error=error))


class TestConsultationShape:
    def test_returns_baseline_notice_before_streaming(self):
        """通報須知是計算出來的,在 AI 開口之前就該備妥。"""
        result = _consultant().consult("小豬下痢要用什麼藥")
        assert result.baseline_notice
        assert "無法判斷" in result.baseline_notice

    def test_stream_yields_ai_text(self):
        result = _consultant(chunks=["第一段", "第二段"]).consult("問題")
        assert "".join(result.stream) == "第一段第二段"

    def test_no_escalation_for_ordinary_question(self):
        result = _consultant().consult("小豬下痢要用什麼藥")
        assert result.escalation is None


class TestReportableEscalation:
    """憲法第一、二條:通報升級由關鍵字決定,不由 AI 決定。"""

    def test_escalates_on_disease_name(self):
        result = _consultant().consult("懷疑是非洲豬瘟")
        assert result.escalation is not None
        assert result.escalation.disease == "非洲豬瘟"

    def test_escalation_available_without_consuming_stream(self):
        """升級判斷不能等 AI 回完才知道 —— 那時使用者可能已經關掉頁面。"""
        result = _consultant().consult("懷疑是非洲豬瘟")
        assert result.escalation is not None  # 尚未讀取 stream

    def test_escalation_survives_ai_failure(self):
        """AI 掛掉時,通報提示仍然必須送到使用者眼前。"""
        result = _consultant(error=QuotaExceeded("額度用盡")).consult("懷疑是非洲豬瘟")
        assert result.escalation is not None
        assert result.baseline_notice
        with pytest.raises(QuotaExceeded):
            list(result.stream)


class TestInputGuards:
    """憲法第九條:用量保護。"""

    def test_rejects_empty_question(self):
        with pytest.raises(ValueError):
            _consultant().consult("   ")

    def test_rejects_overlong_question(self):
        import config
        with pytest.raises(ValueError):
            _consultant().consult("痢" * (config.MAX_QUESTION_CHARS + 1))

    def test_accepts_question_at_limit(self):
        import config
        result = _consultant().consult("痢" * config.MAX_QUESTION_CHARS)
        assert result is not None


class TestFarmContext:
    """US-1 驗收條件 7、8:健檢弱項作為背景,但沒做過健檢也要能用。"""

    def test_context_reaches_the_model(self):
        transport = FakeTransport(chunks=["ok"])
        c = Consultant(transport=transport)
        result = c.consult("豬隻咳嗽", weaknesses=[
            {"name": "離乳前死亡率", "grade": "E", "shortfall_sd": 0.65},
        ])
        list(result.stream)
        assert "離乳前死亡率" in transport.last_prompt

    def test_works_without_context(self):
        """問句刻意不含任何官方劑量對照表關鍵字,單純測健檢背景這一個軸,
        不要跟劑量查表化的比對結果混在一起判斷。
        """
        transport = FakeTransport(chunks=["ok"])
        c = Consultant(transport=transport)
        result = c.consult("豬隻精神沉鬱食慾不振")
        list(result.stream)
        assert transport.last_prompt.strip() == "豬隻精神沉鬱食慾不振"


class TestDosageMatching:
    """劑量查表化:比對結果是計算出來的,取得時即已確定,不必等 AI 回完。"""

    def test_matches_for_relevant_question(self):
        result = _consultant().consult("小豬下痢怎麼辦")
        assert result.dosage_matches != []

    def test_no_match_for_unrelated_question(self):
        result = _consultant().consult("豬隻精神沉鬱食慾不振")
        assert result.dosage_matches == []

    def test_available_without_consuming_stream(self):
        result = _consultant().consult("小豬下痢怎麼辦")
        assert result.dosage_matches != []  # 尚未讀取 stream 也拿得到

    def test_survives_ai_failure(self):
        """AI 掛掉時,已經算好的比對結果仍要送達 —— 不依賴 AI 是否成功。"""
        result = _consultant(error=QuotaExceeded("額度用盡")).consult("小豬下痢")
        assert result.dosage_matches != []
        with pytest.raises(QuotaExceeded):
            list(result.stream)


class TestMyDrugsReachTheModel:
    """牧場主自己的藥品庫要能影響 AI 看到的內容,且型別/長度要被收斂。"""

    def test_drug_name_reaches_the_model(self):
        transport = FakeTransport(chunks=["ok"])
        c = Consultant(transport=transport)
        result = c.consult("小豬下痢", my_drugs=[
            {"name": "阿莫西林可溶性粉", "dosageNote": "每公斤10mg", "withdrawalDays": 7},
        ])
        list(result.stream)
        assert "阿莫西林可溶性粉" in transport.last_prompt

    def test_works_without_my_drugs(self):
        transport = FakeTransport(chunks=["ok"])
        c = Consultant(transport=transport)
        result = c.consult("小豬下痢")
        list(result.stream)
        assert result is not None

    def test_non_list_my_drugs_is_ignored_not_raised(self):
        """跟對話歷史一樣:壞掉的格式直接忽略,不能讓整個請求爆掉。"""
        transport = FakeTransport(chunks=["ok"])
        c = Consultant(transport=transport)
        result = c.consult("小豬下痢", my_drugs="不是陣列")
        list(result.stream)  # 不拋例外即為通過

    def test_entries_missing_name_are_dropped(self):
        transport = FakeTransport(chunks=["ok"])
        c = Consultant(transport=transport)
        result = c.consult("小豬下痢", my_drugs=[{"dosageNote": "沒有名字"}])
        list(result.stream)
        assert "沒有名字" not in transport.last_prompt

    def test_over_limit_count_is_truncated(self):
        import config
        transport = FakeTransport(chunks=["ok"])
        c = Consultant(transport=transport)
        many = [{"name": f"藥{i}"} for i in range(config.MAX_MY_DRUGS + 5)]
        result = c.consult("小豬下痢", my_drugs=many)
        list(result.stream)
        assert f"藥{config.MAX_MY_DRUGS + 4}" not in transport.last_prompt

    def test_overlong_name_is_truncated(self):
        import config
        transport = FakeTransport(chunks=["ok"])
        c = Consultant(transport=transport)
        result = c.consult("小豬下痢", my_drugs=[
            {"name": "藥" * (config.MAX_DRUG_NAME_CHARS + 20)},
        ])
        list(result.stream)
        assert "藥" * (config.MAX_DRUG_NAME_CHARS + 1) not in transport.last_prompt


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


class TestAvailability:
    """規格 6.5:AI 不可用時要能明確辨識,好讓上層降級而非顯示通用錯誤。"""

    def test_reports_quota_error_distinctly(self):
        result = _consultant(error=QuotaExceeded("額度用盡")).consult("問題")
        with pytest.raises(QuotaExceeded):
            list(result.stream)

    def test_reports_login_error_distinctly(self):
        result = _consultant(error=NotLoggedIn("尚未登入")).consult("問題")
        with pytest.raises(NotLoggedIn):
            list(result.stream)
