"""官方劑量對照表比對測試。

核心風控:結果必須是固定資料查表,不是 AI 生成 —— 休藥期算錯會讓藥物
殘留的豬肉流入食物鏈,傷害的是第三方消費者。

data/dosage_table.json 目前 entries 是空陣列(還沒有人提供經查證的資料
來源),所以這裡的比對邏輯測試改用注入的假資料;正式資料檔本身另外
用一組測試鎖住「保持空白,直到有人查證」這件事。
"""

import json

from core.dosage import DATA_PATH, match_dosage_entries

SCOURS = {
    "id": "piglet-scours",
    "diseaseName": "仔豬下痢",
    "keywords": ["下痢", "拉肚子", "腹瀉"],
    "drugs": [
        {"name": "範例藥品A", "dosage": "每公斤體重 10mg,一天兩次", "withdrawalDays": 7},
    ],
    "sourceNote": "測試用途,非真實資料",
    "verified": True,
}

UNVERIFIED_DRAFT = {**SCOURS, "id": "draft-only", "verified": False}


class TestKeywordMatching:
    def test_matches_when_keyword_present(self):
        matches = match_dosage_entries("小豬一直下痢怎麼辦", entries=[SCOURS])
        assert len(matches) == 1
        assert matches[0].disease_name == "仔豬下痢"

    def test_matches_any_alias_keyword(self):
        matches = match_dosage_entries("豬仔一直拉肚子", entries=[SCOURS])
        assert len(matches) == 1

    def test_no_match_returns_empty_list(self):
        assert match_dosage_entries("保育豬咳嗽喘氣", entries=[SCOURS]) == []

    def test_empty_question_returns_empty_list(self):
        assert match_dosage_entries("", entries=[SCOURS]) == []
        assert match_dosage_entries("   ", entries=[SCOURS]) == []

    def test_multiple_entries_can_all_match(self):
        cough = {**SCOURS, "id": "cough", "diseaseName": "咳嗽", "keywords": ["咳嗽"]}
        matches = match_dosage_entries("小豬下痢又咳嗽", entries=[SCOURS, cough])
        assert {m.id for m in matches} == {"piglet-scours", "cough"}


class TestVerifiedGuard:
    """第二層防呆:草稿資料就算被貼進資料檔,也不會流向使用者。"""

    def test_unverified_entry_never_matches(self):
        assert match_dosage_entries("小豬一直下痢", entries=[UNVERIFIED_DRAFT]) == []

    def test_missing_verified_field_defaults_to_excluded(self):
        entry = {k: v for k, v in SCOURS.items() if k != "verified"}
        assert match_dosage_entries("小豬一直下痢", entries=[entry]) == []

    def test_verified_entry_alongside_draft_only_returns_verified(self):
        matches = match_dosage_entries(
            "小豬一直下痢", entries=[SCOURS, UNVERIFIED_DRAFT]
        )
        assert [m.id for m in matches] == ["piglet-scours"]


class TestEntryShape:
    def test_drugs_list_is_passed_through(self):
        matches = match_dosage_entries("小豬一直下痢", entries=[SCOURS])
        assert matches[0].drugs == SCOURS["drugs"]

    def test_source_note_is_passed_through(self):
        matches = match_dosage_entries("小豬一直下痢", entries=[SCOURS])
        assert matches[0].source_note == "測試用途,非真實資料"


class TestProductionDataFileStartsEmpty:
    """正式資料檔的現況:還沒有查證過的資料,不得顯示任何劑量數字。

    這條測試會在管理者填入真實資料後失敗 —— 屆時應該直接刪掉這條測試,
    不是把它改成通過;它存在的目的只是防止有人在資料查證完成前不小心
    把草稿資料當成正式資料上線。
    """

    def test_entries_still_empty_pending_verification(self):
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        assert data["entries"] == [], (
            "dosage_table.json 已經有資料了 —— 如果這些資料已經過查證,"
            "請直接刪除這條測試(它的任務到此結束);"
            "如果還沒查證,請先移除或改成 verified:false。"
        )

    def test_real_data_file_never_crashes_matcher(self):
        """就算正式資料檔是空的,比對邏輯本身也不能壞掉。"""
        assert match_dosage_entries("小豬一直下痢已經兩天") == []
