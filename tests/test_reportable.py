"""法定動物傳染病偵測測試。

憲法第一、二條:通報提示由關鍵字比對觸發,不是 AI 自行判斷。
漏報的後果是防疫延誤;誤報的後果是使用者對提示麻痺。兩者都要測。

資料來源:specs/reference/reportable-diseases.md
"""

import pytest

from core.reportable import baseline_notice, detect_reportable, is_list_complete


class TestBaselineNotice:
    """最重要的一組測試:沒命中關鍵字時,系統不得沉默。

    關鍵字清單不可能完整(官方分類表在法規 PDF 附件,最後修正民國101年)。
    若沒命中就完全沒有提示,使用者會把沉默當成「系統說沒事」。
    """

    def test_baseline_notice_always_available(self):
        assert baseline_notice()

    def test_baseline_states_system_cannot_judge(self):
        """必須明講本系統無法判斷,不能讓使用者以為沉默等於安全。"""
        assert "無法判斷" in baseline_notice()

    def test_baseline_carries_hotline(self):
        assert "1959" in baseline_notice()

    def test_baseline_is_fixed_template(self):
        assert baseline_notice() == baseline_notice()

    def test_list_is_never_claimed_complete(self):
        """清單完整度必須誠實回報,畫面才能據此標示。"""
        assert is_list_complete() is False

    def test_baseline_differs_from_escalated(self):
        """基線須知與命中後的警示是兩段不同文字,否則升級沒有意義。"""
        match = detect_reportable("非洲豬瘟")
        assert match.notice != baseline_notice()


class TestDiseaseNameMatch:
    """直接講出病名,必定觸發。"""

    @pytest.mark.parametrize("text,expected", [
        ("懷疑是非洲豬瘟怎麼辦", "非洲豬瘟"),
        ("ASF 要怎麼防", "非洲豬瘟"),
        ("asf 症狀", "非洲豬瘟"),
        ("是不是口蹄疫", "口蹄疫"),
        ("FMD 的處理方式", "口蹄疫"),
        ("古典豬瘟疫苗", "豬瘟"),
        ("CSF 判別", "豬瘟"),
    ])
    def test_disease_name_triggers(self, text, expected):
        match = detect_reportable(text)
        assert match is not None, f"「{text}」應觸發通報提示"
        assert match.disease == expected


class TestSymptomCombination:
    """單一症狀不觸發,多個症狀組合才觸發 —— 避免「發燒」就跳警告。"""

    def test_single_generic_symptom_does_not_trigger(self):
        assert detect_reportable("豬隻發燒怎麼辦") is None

    def test_two_symptoms_trigger(self):
        match = detect_reportable("豬隻高熱不退,而且開始群聚暴斃")
        assert match is not None
        assert len(match.matched_terms) >= 2

    def test_fmd_symptom_combination(self):
        match = detect_reportable("豬蹄部出現水泡,而且跛行得很嚴重")
        assert match is not None
        assert match.disease == "口蹄疫"


class TestNoFalsePositive:
    """一般諮詢不得誤觸發。"""

    @pytest.mark.parametrize("text", [
        "小豬下痢要用甚麼藥",
        "保育豬咳嗽喘氣可能是什麼病",
        "母豬產後不吃東西怎麼辦",
        "離乳仔豬水腫病如何預防",
        "今天台北天氣如何",
        "",
        "   ",
    ])
    def test_ordinary_question_does_not_trigger(self, text):
        assert detect_reportable(text) is None, f"「{text}」不應觸發通報提示"


class TestNormalization:
    """現場輸入格式不一,不能因為空白或全形就漏掉。"""

    def test_full_width_letters(self):
        assert detect_reportable("ＡＳＦ 疑似案例") is not None

    def test_spaces_inside_disease_name(self):
        assert detect_reportable("非 洲 豬 瘟") is not None

    def test_mixed_case(self):
        assert detect_reportable("AsF 通報") is not None


class TestMatchPayload:
    """回傳內容要足以讓上層組出提示,且可追溯為什麼觸發。"""

    def test_carries_disease_and_terms(self):
        match = detect_reportable("懷疑非洲豬瘟")
        assert match.disease == "非洲豬瘟"
        assert match.matched_terms

    def test_notice_text_is_fixed_template(self):
        """提示文字是固定樣板,不是 AI 生成(憲法第一條)。"""
        a = detect_reportable("非洲豬瘟")
        b = detect_reportable("ASF")
        assert a.notice == b.notice
        assert "24 小時" in a.notice
        assert "1959" in a.notice


class TestDeterminism:
    def test_same_input_same_result(self):
        results = {detect_reportable("非洲豬瘟疑似案例").disease for _ in range(50)}
        assert results == {"非洲豬瘟"}
