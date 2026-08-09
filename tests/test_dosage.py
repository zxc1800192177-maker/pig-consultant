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


class TestProductionDataFilePendingVerification:
    """正式資料檔的現況:AI 從官方手冊轉錄了草稿資料,但還沒有人逐條核對過,
    不得顯示任何劑量數字。

    這條測試會在有人把某筆資料的 verified 改成 true 後,對那一筆失敗 ——
    屆時代表查證完成,應該把那個 id 從下面的清單移除,不是整條測試刪掉
    (只要還有其他未查證的草稿留著,這條測試就還有事情要做)。
    """

    def test_no_entry_is_verified_yet(self):
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        unverified_by_default = [e for e in data["entries"] if e.get("verified")]
        assert unverified_by_default == [], (
            f"以下項目已標記 verified:true,但這條測試假設全部草稿都還沒查證:"
            f"{[e['id'] for e in unverified_by_default]}。"
            "如果真的已經人工核對過手冊原文,這是預期之內的變化 —— "
            "請直接刪除這條測試對應的斷言或整條測試,不是回頭改資料。"
        )

    def test_real_data_file_never_crashes_matcher(self):
        """就算正式資料檔的項目都還沒查證,比對邏輯本身也不能壞掉。"""
        assert match_dosage_entries("小豬一直下痢已經兩天") == []

    def test_real_data_file_has_no_match_until_verified(self):
        """草稿資料再詳細,只要沒有人核對過,就不能顯示給使用者看。"""
        assert match_dosage_entries("小豬下痢") == []
        assert match_dosage_entries("保育豬咳嗽喘氣") == []
        assert match_dosage_entries("豬隻皮膚出現紅色斑點") == []
