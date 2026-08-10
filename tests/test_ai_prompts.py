"""提示詞測試。

提示詞單獨成一個模組(SRP):它因「顧問語氣與要求改變」而修改,
與「怎麼呼叫 CLI」是兩回事。

這些測試不呼叫 AI,只驗證送出去的文字內容正確。
"""

import pytest

from ai.prompts import (
    ADVICE_SYSTEM_PROMPT,
    DISEASE_SYSTEM_PROMPT,
    build_dosage_reference,
    build_farm_context,
    build_history_context,
    build_my_drugs_context,
    build_reference_factors,
)
from core.dosage import DosageEntry


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

    def test_forbids_fabricating_dosage_numbers(self):
        """劑量查表化的核心規範:AI 不得自己生成劑量與休藥期數字。"""
        assert "不可自行更改" in DISEASE_SYSTEM_PROMPT or "不可以自己編劑量" in DISEASE_SYSTEM_PROMPT

    def test_restricts_dosage_sources_to_named_references(self):
        assert "官方劑量對照表" in DISEASE_SYSTEM_PROMPT
        assert "牧場主自己的藥品庫" in DISEASE_SYSTEM_PROMPT


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


class TestDosageReference:
    """官方劑量對照表比對結果的呈現方式。"""

    MATCH = DosageEntry(
        id="piglet-scours",
        disease_name="仔豬下痢",
        drugs=[{"name": "範例藥品A", "dosage": "每公斤體重 10mg,一天兩次", "withdrawalDays": 7}],
        source_note="測試用途",
    )

    def test_empty_when_no_match(self):
        assert build_dosage_reference([]) == ""
        assert build_dosage_reference(None) == ""

    def test_includes_drug_name_and_dosage(self):
        context = build_dosage_reference([self.MATCH])
        assert "範例藥品A" in context
        assert "每公斤體重 10mg" in context

    def test_includes_withdrawal_days(self):
        context = build_dosage_reference([self.MATCH])
        assert "7 天" in context

    def test_omits_withdrawal_when_none(self):
        entry = self.MATCH._replace(
            drugs=[{"name": "範例藥品B", "dosage": "每公斤 5mg", "withdrawalDays": None}]
        )
        context = build_dosage_reference([entry])
        assert "None" not in context

    def test_marks_as_verified_not_editable(self):
        """必須明講這些數字已查證、不可更改,否則模型可能自行「潤飾」數字。"""
        context = build_dosage_reference([self.MATCH])
        assert "已經查證" in context
        assert "不可" in context


class TestMyDrugsContext:
    """牧場主自己輸入的藥品庫。信任邊界跟官方對照表不同,要讓 AI 分得清楚。"""

    DRUGS = [
        {"name": "阿莫西林可溶性粉", "dosage_note": "每公斤體重 10mg,一天兩次", "withdrawal_days": 7},
    ]

    def test_empty_when_no_drugs(self):
        assert build_my_drugs_context([]) == ""
        assert build_my_drugs_context(None) == ""

    def test_includes_drug_name_and_note(self):
        context = build_my_drugs_context(self.DRUGS)
        assert "阿莫西林可溶性粉" in context
        assert "每公斤體重 10mg" in context

    def test_includes_withdrawal_days_when_present(self):
        context = build_my_drugs_context(self.DRUGS)
        assert "7 天" in context

    def test_works_without_optional_fields(self):
        context = build_my_drugs_context([{"name": "只有名字的藥"}])
        assert "只有名字的藥" in context

    def test_marks_source_as_user_provided_unverified(self):
        """必須讓 AI 知道這份資料沒有經過系統查證,信任邊界跟官方對照表不同。"""
        context = build_my_drugs_context(self.DRUGS)
        assert "牧場主" in context
        assert "未另外查證" in context


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
