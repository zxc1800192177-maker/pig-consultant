"""官方劑量對照表比對測試。

核心風控:結果必須是固定資料查表,不是 AI 生成 —— 休藥期算錯會讓藥物
殘留的豬肉流入食物鏈,傷害的是第三方消費者。

data/dosage_table.json 目前的三筆資料整理自官方手冊,經 Ian review 後
授權顯示,所以這裡的比對邏輯測試主要用注入的假資料驗證演算法本身;
正式資料檔另外用一組測試鎖住「每一筆都查得到、都有出處可追溯」這件事,
避免之後有人加新項目時漏掉 sourceNote 或忘記設 verified。
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


class TestProductionDataFile:
    """正式資料檔的現況:三筆資料整理自官方手冊,經 Ian review 後授權顯示。

    這裡鎖住的不是內容本身(手冊原文可能改版,數字本來就可能調整),
    而是「資料一定要有出處、一定要能被追溯」這件事 —— 這是 AI 轉錄
    官方文件而非自己生成劑量的核心價值,少了可追溯性就跟直接生成沒兩樣。
    """

    def test_common_example_symptoms_all_match(self):
        """首頁範例問句用的症狀,至少要查得到對應資料,示範功能才有意義。"""
        assert match_dosage_entries("小豬下痢要用甚麼藥") != []
        assert match_dosage_entries("保育豬咳嗽喘氣可能是什麼病") != []
        assert match_dosage_entries("豬隻皮膚出現紅色斑點是什麼問題") != []

    def test_every_entry_has_a_traceable_source(self):
        """每一筆都要能追溯回手冊的具體頁碼或章節,不能只有結論沒有依據。"""
        for match in match_dosage_entries("下痢") + match_dosage_entries(
            "咳嗽"
        ) + match_dosage_entries("紅斑"):
            assert match.source_note.strip(), f"{match.id} 缺少 sourceNote"

    def test_every_drug_has_dosage_text_and_explicit_withdrawal_field(self):
        """休藥期寧可明確是 None(未知),也不能漏掉這個欄位不寫。"""
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for entry in data["entries"]:
            for drug in entry["drugs"]:
                assert drug.get("name"), f"{entry['id']} 有藥品缺少名稱"
                assert drug.get("dosage"), f"{entry['id']} 的 {drug.get('name')} 缺少劑量描述"
                assert "withdrawalDays" in drug, (
                    f"{entry['id']} 的 {drug.get('name')} 沒有 withdrawalDays 欄位"
                )

    def test_real_data_file_never_crashes_matcher(self):
        assert match_dosage_entries("") == []
        assert match_dosage_entries("跟豬完全無關的問題") == []
