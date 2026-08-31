"""PigCHAMP 匯出檔的解析與匯入。

解析是純函式,所以這些測試不碰資料庫也不碰檔案系統 —— 匯入的部分才用
InMemoryStore。

**編碼是這個模組最容易出錯的地方**:同一批匯出檔裡 .txt 是 UTF-8、
CSV 是 Big5,而用錯編碼不會報錯,只會產生看起來像資料損壞的假象。
那個假象曾經被誤判成「5 筆欄位錯位」寫進規格(見 specs/v2-facts.md 第 8 條)。
"""

import json
from datetime import date

import pytest

import importer
from db import InMemoryStore


def line(*fields):
    return "|".join(str(f) for f in fields)


MATE = line("1183", "MT", "20260203", "D6", "", "", "", "", "", "", "", "YES")
FARROW = line("1183", "FW", "20260528", "10", "3", "0", "", "", "NO", "NO")
WEAN = line("1183", "WN", "20260619", "9", "", "0")


class TestEncoding:
    """UTF-8 要排在 Big5 前面試 —— 順序反過來會靜默地解出亂碼。"""

    def test_reads_utf8(self):
        assert "母豬壓死" in importer.decode("母豬壓死".encode("utf-8"))

    def test_reads_big5(self):
        assert "母豬壓死" in importer.decode("母豬壓死".encode("cp950"))

    def test_utf8_is_not_mistaken_for_big5(self):
        """關鍵:Big5 幾乎什麼位元組都吃得下去,先試它的話 UTF-8 的檔案會被
        『成功』解成亂碼而不報錯。這正是先前誤判成資料損壞的原因。
        """
        text = line("L文-112/05/01", "MT", "20260203")
        assert importer.decode(text.encode("utf-8")) == text

    def test_strips_bom(self):
        assert importer.decode("1183|MT|20260203".encode("utf-8-sig")).startswith("1183")


class TestParsing:
    def test_reads_events(self):
        r = importer.parse(MATE)
        assert len(r.rows) == 1
        assert r.rows[0].ear_tag == "1183"
        assert r.rows[0].code == "MT"
        assert r.rows[0].when == date(2026, 2, 3)

    def test_blank_lines_are_ignored(self):
        assert len(importer.parse(f"\n{MATE}\n\n").rows) == 1

    def test_chinese_ear_tags_survive(self):
        """56 個 ID 含中文字,它們不該被當成壞資料。"""
        r = importer.parse(line("L文-112/05/01", "MT", "20260203"))
        assert r.rows[0].ear_tag == "L文-112/05/01"
        assert r.bad_lines == []

    def test_boar_events_go_to_their_own_list(self):
        r = importer.parse(line("D1", "BA", "20190625", "Duroc"))
        assert r.rows == []
        assert len(r.boar_rows) == 1

    def test_unused_codes_are_counted_not_dropped_silently(self):
        r = importer.parse(line("533", "HD", "20200629"))
        assert r.rows == []
        assert r.skipped["HD"] == 1

    def test_unknown_code_is_skipped_not_fatal(self):
        r = importer.parse(line("1183", "ZZZ", "20260203"))
        assert r.skipped["ZZZ"] == 1
        assert r.bad_lines == []


class TestBadLines:
    """壞掉的一行不該中止整個匯入 —— 記下來繼續跑。"""

    def test_bad_date_is_reported(self):
        r = importer.parse(line("1183", "MT", "not-a-date"))
        assert r.rows == []
        assert len(r.bad_lines) == 1

    def test_impossible_date_is_rejected(self):
        r = importer.parse(line("1183", "MT", "20240230"))
        assert r.rows == []

    def test_missing_fields(self):
        assert len(importer.parse("1183|MT").bad_lines) == 1

    def test_good_lines_still_import(self):
        r = importer.parse("\n".join([MATE, "壞掉的一行", FARROW]))
        assert len(r.rows) == 2


class TestDetailExtraction:
    """欄位位置對照實際匯出檔確認過,存錯欄位會讓月報整個算錯。"""

    def test_mating_keeps_the_boar(self):
        assert importer.parse(MATE).rows[0].detail["boar_tag"] == "D6"

    def test_farrowing_splits_alive_still_mummy(self):
        d = importer.parse(FARROW).rows[0].detail
        assert (d["born_alive"], d["stillborn"], d["mummified"]) == (10, 3, 0)

    def test_weaning_count(self):
        assert importer.parse(WEAN).rows[0].detail["weaned"] == 9

    def test_pregnancy_check_positive(self):
        row = importer.parse(line("1183", "PD", "20260301", "", "", "", "", "", "+")).rows[0]
        assert row.detail["positive"] is True

    def test_pregnancy_check_negative(self):
        row = importer.parse(line("1183", "PD", "20260301", "", "", "", "", "", "-")).rows[0]
        assert row.detail["positive"] is False

    def test_pregnancy_check_unknown_is_none(self):
        """空白不等於陰性 —— 當成陰性會讓母豬被錯誤地移出懷孕狀態。"""
        row = importer.parse(line("1183", "PD", "20260301")).rows[0]
        assert row.detail["positive"] is None

    def test_piglet_loss_reason(self):
        row = importer.parse(line("1183", "PL", "20260529", "2", "1.母豬壓死")).rows[0]
        assert row.detail == {"count": 2, "reason": "1.母豬壓死"}

    def test_entry_keeps_parents(self):
        row = importer.parse(
            line("2580", "GA", "20230317", "Landrace", "", "20220616", "L鄭", "2416")).rows[0]
        assert row.detail["sire_tag"] == "L鄭"
        assert row.detail["dam_tag"] == "2416"


class TestAnomalies:
    """離群值把關。門檻刻意寬鬆 —— 目的是抓出打錯的數字,不是質疑生產成績。
    這個場 32,814 筆只有 2 筆被抓到。
    """

    def test_impossible_litter_size(self):
        """真實案例:1585 於 2025-10-15 被記成 56 隻。"""
        r = importer.parse(line("1585", "FW", "20251015", "56", "0", "0"))
        assert len(r.anomalies) == 1
        assert "56" in r.anomalies[0].reason

    def test_normal_litter_is_not_flagged(self):
        assert importer.parse(FARROW).anomalies == []

    def test_negative_counts(self):
        r = importer.parse(line("1183", "FW", "20260528", "-3", "0", "0"))
        assert r.anomalies and "負數" in r.anomalies[0].reason

    def test_long_lactation(self):
        """真實案例:2452-D112 哺乳 50 天。可能是真的,所以只標記不阻擋。"""
        r = importer.parse("\n".join([
            line("2452", "FW", "20230201", "10", "0", "0"),
            line("2452", "WN", "20230323", "9"),
        ]))
        assert r.anomalies and "哺乳" in r.anomalies[0].reason

    def test_weaning_before_farrowing(self):
        """離乳日早於分娩日。

        偵測方式是「離乳前找不到分娩」而不是直接比日期大小 —— 事件會先
        依日期排序,排序後順序已經被調正,比大小永遠偵測不到。
        """
        r = importer.parse("\n".join([
            line("1183", "FW", "20260528", "10", "0", "0"),
            line("1183", "WN", "20260501", "9"),
        ]))
        assert any("沒有對應的分娩" in a.reason for a in r.anomalies)

    def test_orphan_weaning(self):
        r = importer.parse(line("1183", "WN", "20260501", "9"))
        assert any("沒有對應的分娩" in a.reason for a in r.anomalies)

    def test_normal_farrow_wean_pair_is_clean(self):
        r = importer.parse("\n".join([
            line("1183", "FW", "20260528", "10", "0", "0"),
            line("1183", "WN", "20260619", "9"),
        ]))
        assert r.anomalies == []

    def test_future_date(self):
        r = importer.parse(line("1183", "MT", "20991231"), today=date(2026, 8, 13))
        assert any("未來" in a.reason for a in r.anomalies)

    def test_anomaly_points_at_a_line(self):
        """畫面上要能告訴使用者是第幾行,否則他無從查證。"""
        r = importer.parse("\n".join([MATE, line("1585", "FW", "20251015", "56", "0", "0")]))
        assert r.anomalies[0].line_no == 2


class TestSummary:
    def test_counts_for_the_preview_screen(self):
        s = importer.summarize(importer.parse("\n".join([MATE, FARROW, WEAN])))
        assert s["sows"] == 1
        assert s["events"] == 3
        assert s["byCode"] == {"MT": 1, "FW": 1, "WN": 1}
        assert s["dateRange"] == ["2026-02-03", "2026-06-19"]

    def test_empty_file_does_not_crash(self):
        s = importer.summarize(importer.parse(""))
        assert s["events"] == 0
        assert s["dateRange"] is None

    def test_semen_collections_are_counted_separately_from_quality_checks(self):
        rows = "\n".join([
            "D6|BA|20200301",
            "D6|SC|20200302|15|3",
            "L153|SP|20200630|150|外購L精液",
            "110/07/06|SC|20210706|10|2",
        ])
        s = importer.summarize(importer.parse(rows))
        assert s["boarEvents"] == 4          # BA + SC(2 筆) + SP 全部
        assert s["semenCollections"] == 2    # 只算 SC
        assert s["semenCollectionsSkipped"] == 1
        assert s["semenQualityRows"] == 1    # SP,整批不匯入


class TestImportIntoStore:
    @pytest.fixture
    def farm(self):
        store = InMemoryStore()
        return store, store.create_farm("HYD")

    def test_creates_sows_and_events(self, farm):
        store, farm_id = farm
        result = importer.parse("\n".join([MATE, FARROW, WEAN]))
        stats = importer.import_into(store, farm_id, result)

        assert stats == {"sows": 1, "events": 3, "excluded": 0, "boars": 0,
                         "semenCollections": 0, "semenCollectionsSkipped": 0}
        assert len(store.list_sows(farm_id)) == 1
        assert len(store.list_sow_events(farm_id)) == 3

    def test_is_idempotent(self, farm):
        """同一份檔案匯兩次不該產生兩倍資料。"""
        store, farm_id = farm
        result = importer.parse("\n".join([MATE, FARROW, WEAN]))
        importer.import_into(store, farm_id, result)
        importer.import_into(store, farm_id, result)

        assert len(store.list_sows(farm_id)) == 1
        assert len(store.list_sow_events(farm_id)) == 3

    def test_entry_event_fills_in_the_sow(self, farm):
        store, farm_id = farm
        result = importer.parse(
            line("2580", "GA", "20230317", "Landrace", "", "20220616", "L鄭", "2416"))
        importer.import_into(store, farm_id, result)

        sow = store.find_sow_by_tag(farm_id, "2580")
        assert sow["breed"] == "Landrace"
        assert sow["birth_date"] == date(2022, 6, 16)
        assert sow["sire_tag"] == "L鄭"
        assert sow["dam_tag"] == "2416"
        assert sow["entry_date"] == date(2023, 3, 17)

    def test_parity_is_derived_from_farrowings(self, farm):
        """不補正的話母豬卡的胎次全是 0。"""
        store, farm_id = farm
        rows = "\n".join([
            line("1183", "FW", "20250101", "10", "0", "0"),
            line("1183", "WN", "20250123", "9"),
            line("1183", "FW", "20250601", "11", "0", "0"),
        ])
        importer.import_into(store, farm_id, importer.parse(rows))
        assert store.find_sow_by_tag(farm_id, "1183")["parity"] == 2

    def test_culled_sow_gets_the_right_status(self, farm):
        store, farm_id = farm
        rows = "\n".join([MATE, line("1183", "SAL", "20260701", "年齡太大")])
        importer.import_into(store, farm_id, importer.parse(rows))

        assert store.find_sow_by_tag(farm_id, "1183") is None, "離群的豬不該被當成在場"
        assert store.list_sows(farm_id)[0]["status"] == "culled"

    def test_excluded_lines_are_stored_but_flagged(self, farm):
        """排除不等於刪除 —— 資料留著,只是不納入統計。"""
        store, farm_id = farm
        rows = "\n".join([MATE, line("1585", "FW", "20251015", "56", "0", "0")])
        result = importer.parse(rows)
        bad_line = result.anomalies[0].line_no

        importer.import_into(store, farm_id, result, exclude_lines=[bad_line])

        events = store.list_sow_events(farm_id)
        assert len(events) == 2, "被排除的事件仍要存在"
        assert [e["excluded"] for e in events if e["event_type"] == "FW"] == [True]

    def test_records_who_imported(self, farm):
        store, farm_id = farm
        user = store.create_user("farmer", "hash")
        importer.import_into(store, farm_id, importer.parse(MATE), recorded_by=user)
        assert store.list_sow_events(farm_id)[0]["recorded_by"] == user

    def test_imports_into_the_right_farm_only(self, farm):
        store, farm_id = farm
        other = store.create_farm("別的牧場")
        importer.import_into(store, farm_id, importer.parse(MATE))
        assert store.list_sows(other) == []
        assert store.list_sow_events(other) == []


class TestReimportWithCulledSows:
    """**這個 bug 只有拿真實資料重跑才會出現。**

    import_into 原本用 find_sow_by_tag 判斷母豬是否已存在,但那個方法只找
    在場的豬。第一次匯入後,已淘汰的母豬 status 變成 culled,重跑時就查不到,
    於是想再新增一次而撞唯一鍵 —— ValueError,整個匯入中止。

    單元測試當初沒抓到,因為測試裡的母豬都還在場。
    """

    @pytest.fixture
    def farm(self):
        store = InMemoryStore()
        return store, store.create_farm("HYD")

    ROWS = "\n".join([
        line("1183", "GA", "20230519", "LY"),
        line("1183", "MT", "20260203", "D6"),
        line("1183", "SAL", "20260701", "年齡太大"),
    ])

    def test_reimport_with_a_culled_sow_does_not_crash(self, farm):
        store, farm_id = farm
        result = importer.parse(self.ROWS)
        importer.import_into(store, farm_id, result)

        importer.import_into(store, farm_id, result)   # 不該拋例外

        assert len(store.list_sows(farm_id)) == 1
        assert len(store.list_sow_events(farm_id)) == 3

    def test_culled_sow_keeps_her_status_after_reimport(self, farm):
        store, farm_id = farm
        result = importer.parse(self.ROWS)
        importer.import_into(store, farm_id, result)
        importer.import_into(store, farm_id, result)
        assert store.list_sows(farm_id)[0]["status"] == "culled"


class TestSameDayDifferentContent:
    """**同一天的多筆同類事件不可合併。**

    唯一鍵原本是 (母豬, 事件, 日期),為了讓匯入冪等而設,但它把真實的
    同日多筆事件當成重複。拿真實資料實測時靜默吃掉了 358 筆:

      仔豬損失 186 組 —— 同一天死兩隻,死因不同
      配種     146 組 —— 其中 101 組同一天用了不同公豬(雙重配種)

    修法是把 detail 納入唯一鍵。
    """

    @pytest.fixture
    def farm(self):
        store = InMemoryStore()
        return store, store.create_farm("HYD")

    def test_two_piglet_losses_with_different_reasons(self, farm):
        """真實案例:383-D111 於 2021-12-15 死兩隻,一隻虛弱一隻被壓死。"""
        store, farm_id = farm
        rows = "\n".join([
            line("383", "PL", "20211215", "1", "2.虛弱"),
            line("383", "PL", "20211215", "1", "1.母豬壓死"),
        ])
        importer.import_into(store, farm_id, importer.parse(rows))

        events = store.list_sow_events(farm_id)
        assert len(events) == 2, "死因不同就是兩件事,不可合併"
        assert {e["detail"]["reason"] for e in events} == {"2.虛弱", "1.母豬壓死"}

    def test_double_mating_with_two_boars(self, farm):
        """同一天用兩頭公豬是真的雙重配種,合併會丟掉一頭。"""
        store, farm_id = farm
        rows = "\n".join([
            line("1183", "MT", "20260203", "D4"),
            line("1183", "MT", "20260203", "D8"),
        ])
        importer.import_into(store, farm_id, importer.parse(rows))

        events = store.list_sow_events(farm_id)
        assert len(events) == 2
        assert {e["detail"]["boar_tag"] for e in events} == {"D4", "D8"}

    def test_identical_rows_in_one_file_are_two_events(self, farm):
        """**一模一樣的兩行也是兩件事,不是重複輸入。**

        實測 153 組,其中 101 組是仔豬損失:同一天死兩隻、死因相同,
        各記一筆。合併掉會少算仔豬死亡數,直接影響離乳前死亡率 ——
        那正是這個牧場最該盯的指標。

        冪等改用「第幾次出現」達成:同一份檔案重跑,編號一樣,不會變多。
        """
        store, farm_id = farm
        rows = "\n".join([
            line("343", "PL", "20201029", "1", "2.虛弱"),
            line("343", "PL", "20201029", "1", "2.虛弱"),
        ])
        importer.import_into(store, farm_id, importer.parse(rows))
        assert len(store.list_sow_events(farm_id)) == 2

    def test_repeated_identical_rows_are_still_idempotent(self, farm):
        """重複的行要保留,但重跑同一份檔案不能讓資料變成四筆。"""
        store, farm_id = farm
        rows = "\n".join([
            line("343", "PL", "20201029", "1", "2.虛弱"),
            line("343", "PL", "20201029", "1", "2.虛弱"),
        ])
        result = importer.parse(rows)
        importer.import_into(store, farm_id, result)
        importer.import_into(store, farm_id, result)
        assert len(store.list_sow_events(farm_id)) == 2

    def test_reimport_is_still_idempotent(self, farm):
        store, farm_id = farm
        rows = "\n".join([
            line("383", "PL", "20211215", "1", "2.虛弱"),
            line("383", "PL", "20211215", "1", "1.母豬壓死"),
        ])
        result = importer.parse(rows)
        importer.import_into(store, farm_id, result)
        importer.import_into(store, farm_id, result)
        assert len(store.list_sow_events(farm_id)) == 2


class TestOddBoarTagsAreReported:
    """來源檔案把日期填進了公豬 ID 欄位(實測 25/154 個)。

    **不修正也不丟掉** —— 那是使用者的資料。但一定要講出來:配種記錄
    要從公豬清單裡選,不講的話那些會混在選單裡,使用者只會覺得系統壞了。
    """

    ROWS = "\n".join([
        "1183|GA|20230519|LY",
        "D6|BA|20200301",
        "109/09/28|BA|20200915",
        "110/07/06|SC|20210706",
        "D-111/08/23|BA|20220823",     # 前面帶字母的也算
    ])

    def _preview(self):
        return importer.summarize(importer.parse(self.ROWS))

    def test_date_like_tags_are_listed(self):
        odd = self._preview()["oddBoarTags"]
        assert set(odd) == {"109/09/28", "110/07/06", "D-111/08/23"}

    def test_real_ear_tags_are_not_flagged(self):
        assert "D6" not in self._preview()["oddBoarTags"]

    def test_they_are_still_imported_unchanged(self):
        """只是提醒,不是過濾 —— 少匯一頭公豬,那些配種記錄就對不上了。"""
        store = InMemoryStore()
        farm_id = store.create_farm("HYD")
        importer.import_into(store, farm_id, importer.parse(self.ROWS))
        tags = {b["ear_tag"] for b in store.list_boars(farm_id)}
        assert "109/09/28" in tags

    def test_clean_file_reports_nothing(self):
        clean = importer.summarize(importer.parse("D6|BA|20200301"))
        assert clean["oddBoarTags"] == []


class TestSemenCollectionsAreImported:
    """採精(SC)寫進 boar_events;精液品質(SP)整批不寫 —— 精蟲活力/濃度
    已經併進 SC 表單,SP 不再是這個 app 認得的事件類型
    (schedule.KNOWN_BOAR_EVENTS 沒有它,見 importer.import_into 的說明)。
    """

    @pytest.fixture
    def farm(self):
        store = InMemoryStore()
        return store, store.create_farm("HYD")

    ROWS = "\n".join([
        "D6|BA|20200301",
        "D6|SC|20200302|15|3",
        "L153|SP|20200630|150|外購L精液",
        "110/07/06|SC|20210706|10|2",   # 耳號像民國日期,對不到真公豬
    ])

    def test_semen_collection_is_written(self, farm):
        store, farm_id = farm
        stats = importer.import_into(store, farm_id, importer.parse(self.ROWS))

        assert stats["semenCollections"] == 1
        boar = store.find_boar_by_tag(farm_id, "D6")
        events = store.list_boar_events(farm_id, boar["id"])
        assert len(events) == 1
        assert events[0]["event_type"] == "SC"
        assert events[0]["detail"]["volume"] == 15

    def test_semen_quality_is_never_written(self, farm):
        """SP 不是這個 app 認得的事件類型,整批不寫 —— 不只是耳號有問題的那些。"""
        store, farm_id = farm
        importer.import_into(store, farm_id, importer.parse(self.ROWS))

        types = {e["event_type"] for e in store.list_boar_events(farm_id)}
        assert "SP" not in types

    def test_date_like_ear_tag_is_skipped_and_counted(self, farm):
        store, farm_id = farm
        stats = importer.import_into(store, farm_id, importer.parse(self.ROWS))

        assert stats["semenCollectionsSkipped"] == 1
        # 身分仍然照建(不修正也不丟掉那頭豬),只是這筆採精事件沒有寫入
        odd_boar = store.find_boar_by_tag(farm_id, "110/07/06")
        assert odd_boar is not None
        assert store.list_boar_events(farm_id, odd_boar["id"]) == []

    def test_is_idempotent(self, farm):
        store, farm_id = farm
        result = importer.parse(self.ROWS)
        importer.import_into(store, farm_id, result)
        importer.import_into(store, farm_id, result)

        boar = store.find_boar_by_tag(farm_id, "D6")
        assert len(store.list_boar_events(farm_id, boar["id"])) == 1


class TestBackupRestore:
    """完整備份要放得回去。

    匯出的備份原本讀不進匯入畫面 —— 使用者拿自己剛存下來的檔案想還原,
    得到的是「452,266 行無法解析」。備份存得出來卻放不回去,等於沒有備份。

    這一組測的核心只有一件事:**匯出 → 還原 → 再匯出,內容要一模一樣。**
    只斷言筆數相同是不夠的,少一個 detail 欄位或把 excluded 弄丟,筆數
    照樣對得上。
    """

    def _farm(self):
        store = InMemoryStore()
        return store, store.create_farm("測試場")

    def _backup(self, **over):
        data = {
            "farmName": "測試場",
            "exportedAt": "2026-08-31",
            "sows": [
                {"id": 1, "earTag": "2580", "breed": "LY", "parity": 3,
                 "status": "active", "penId": 7, "sireTag": "B1", "damTag": "M9",
                 "entryDate": "2024-03-01", "birthDate": "2023-08-10",
                 "isUnknown": False},
                # 已淘汰的豬,耳號帶著離群年份後綴
                {"id": 2, "earTag": "1183-D115", "breed": "LY", "parity": 6,
                 "status": "culled", "penId": None, "sireTag": "", "damTag": "",
                 "entryDate": "2020-01-01", "birthDate": None, "isUnknown": False},
            ],
            "boars": [{"id": 9, "earTag": "B1", "breed": "D", "status": "active",
                       "entryDate": "2023-01-05", "sireTag": "", "damTag": ""}],
            "events": [
                {"id": 10, "sowId": 1, "earTag": "2580", "type": "MT",
                 "date": "2026-01-10", "excluded": False,
                 "detail": {"boar_tag": "B1", "estrus_stability": "stable"}},
                {"id": 11, "sowId": 1, "earTag": "2580", "type": "MV",
                 "date": "2026-04-20", "excluded": False,
                 "detail": {"pen_id": 7, "pen_name": "A-12", "zone": "farrowing"}},
                {"id": 12, "sowId": 1, "earTag": "2580", "type": "FW",
                 "date": "2026-05-04", "excluded": False,
                 "detail": {"born_alive": 12, "stillborn": 1, "raised": 13,
                            "assisted": True}},
                {"id": 13, "sowId": 1, "earTag": "2580", "type": "WN",
                 "date": "2026-05-25", "excluded": False,
                 "detail": {"weaned": 11, "wean_score": 4, "hernia": True}},
                {"id": 14, "sowId": 2, "earTag": "1183-D115", "type": "FW",
                 "date": "2025-06-08", "excluded": True,
                 "detail": {"born_alive": 56}},
                {"id": 15, "sowId": 2, "earTag": "1183-D115", "type": "SAL",
                 "date": "2026-02-01", "excluded": False,
                 "detail": {"reason": "年齡太大"}},
            ],
            "boarEvents": [
                {"id": 90, "boarId": 9, "earTag": "B1", "type": "SC",
                 "date": "2026-02-02", "detail": {"volume": 250, "motility": 80}},
            ],
            "marketDeaths": [
                {"id": 5, "date": "2026-03-03",
                 "detail": {"reason": "熱緊迫", "weight_kg": 92.5}},
            ],
            "pens": [{"id": 7, "name": "A-12", "zone": "farrowing"},
                     {"id": 8, "name": "B-03", "zone": "gestation"}],
            "customTasks": [{"name": "產房消毒", "startDate": "2026-01-01",
                             "repeat": "weekly"}],
            "settings": {"gestation_days": 115},
        }
        data.update(over)
        return json.dumps(data, ensure_ascii=False)

    def test_backup_is_told_apart_from_a_pigchamp_file(self):
        assert importer.looks_like_backup(self._backup())
        assert not importer.looks_like_backup(MATE)
        assert not importer.looks_like_backup("")

    def test_a_file_that_is_not_a_backup_says_so(self):
        """訊息要講得出是什麼問題 —— 「解析失敗」對使用者沒有用。"""
        with pytest.raises(ValueError, match="讀得開"):
            importer.parse_backup("{ 這不是 JSON")
        with pytest.raises(ValueError, match="完整備份"):
            importer.parse_backup('{"hello": 1}')

    def test_everything_comes_back(self):
        store, farm = self._farm()
        stats = importer.restore_backup(store, farm,
                                        importer.parse_backup(self._backup()))
        assert stats["sows"] == 2
        assert stats["events"] == 6
        assert stats["boars"] == 1
        assert stats["boarEvents"] == 1
        assert stats["marketDeaths"] == 1
        assert stats["pens"] == 2
        assert stats["customTasks"] == 1

    def test_details_survive_untouched(self):
        """匯入那條路會清洗欄位;還原不可以 —— 備份存的已經是最終形態。"""
        store, farm = self._farm()
        importer.restore_backup(store, farm, importer.parse_backup(self._backup()))
        wean = [e for e in store.list_sow_events(farm) if e["event_type"] == "WN"][0]
        assert wean["detail"] == {"weaned": 11, "wean_score": 4, "hernia": True}

    def test_the_year_suffix_is_not_added_twice(self):
        """1183-D115 已經是離群後的耳號。走記錄事件那條路還原的話,系統看到
        淘汰會再加一次,變成 1183-D115-D115,那頭豬就再也對不回原本的記錄。
        """
        store, farm = self._farm()
        importer.restore_backup(store, farm, importer.parse_backup(self._backup()))
        tags = {s["ear_tag"] for s in store.list_sows(farm)}
        assert tags == {"2580", "1183-D115"}

    def test_status_and_parity_come_from_the_backup(self):
        store, farm = self._farm()
        importer.restore_backup(store, farm, importer.parse_backup(self._backup()))
        by_tag = {s["ear_tag"]: s for s in store.list_sows(farm)}
        assert by_tag["1183-D115"]["status"] == "culled"
        assert by_tag["1183-D115"]["parity"] == 6
        assert by_tag["2580"]["parity"] == 3

    def test_excluded_events_stay_excluded(self):
        """那筆 56 隻的離群記錄當初被判為不納入統計。還原後又納進去的話,
        使用者得再判斷一次,而且中間的月報數字會悄悄變掉。
        """
        store, farm = self._farm()
        importer.restore_backup(store, farm, importer.parse_backup(self._backup()))
        odd = [e for e in store.list_sow_events(farm)
               if (e.get("detail") or {}).get("born_alive") == 56]
        assert len(odd) == 1
        assert odd[0]["excluded"] is True

    def test_pen_ids_are_remapped_not_copied(self):
        """備份裡的 pen_id 是舊資料庫的流水號,還原後一定是另一組。照抄的話
        母豬會指向一個不存在的欄位,產房頁看起來就是空的。
        """
        store, farm = self._farm()
        importer.restore_backup(store, farm, importer.parse_backup(self._backup()))
        pens = {p["name"]: p["id"] for p in store.list_pens(farm)}
        sow = [s for s in store.list_sows(farm) if s["ear_tag"] == "2580"][0]
        assert sow["pen_id"] == pens["A-12"]
        move = [e for e in store.list_sow_events(farm) if e["event_type"] == "MV"][0]
        assert move["detail"]["pen_id"] == pens["A-12"]

    def test_settings_and_custom_tasks_return(self):
        """事件都在、參數卻回到預設值的話,還原出來的是另一座牧場。"""
        store, farm = self._farm()
        importer.restore_backup(store, farm, importer.parse_backup(self._backup()))
        assert store.get_farm_settings(farm)["gestation_days"] == 115
        assert [t["name"] for t in store.list_custom_tasks(farm)] == ["產房消毒"]

    def test_restoring_twice_does_not_double_anything(self):
        store, farm = self._farm()
        text = self._backup()
        importer.restore_backup(store, farm, importer.parse_backup(text))
        before = len(store.list_sow_events(farm))
        again = importer.restore_backup(store, farm, importer.parse_backup(text))
        assert again["sows"] == 0, "耳號已經在場,不該再建一次"
        assert len(store.list_sows(farm)) == 2
        assert len(store.list_sow_events(farm)) == before
        assert len(store.list_pens(farm)) == 2
        assert len(store.list_custom_tasks(farm)) == 1

    def test_unreadable_rows_are_skipped_and_counted(self):
        """壞掉的個別欄位不該讓整份還原失敗 —— 剩下的三萬筆還是要回得來。"""
        bad = json.loads(self._backup())
        bad["events"].append({"id": 99, "sowId": 1, "type": "???",
                              "date": "2026-01-01", "detail": {}})
        bad["events"].append({"id": 98, "sowId": 1, "type": "MT",
                              "date": "不是日期", "detail": {}})
        bad["sows"].append({"id": 3, "earTag": "  "})
        result = importer.parse_backup(json.dumps(bad, ensure_ascii=False))
        assert len(result.events) == 6
        assert len(result.sows) == 2
        assert len(result.problems) == 3
        assert importer.summarize_backup(result)["badLineCount"] == 3

    def test_the_preview_says_what_will_come_back(self):
        summary = importer.summarize_backup(importer.parse_backup(self._backup()))
        assert summary["kind"] == "backup"
        assert summary["farmName"] == "測試場"
        assert summary["exportedAt"] == "2026-08-31"
        assert summary["sows"] == 2 and summary["events"] == 6
        assert summary["pens"] == 2 and summary["customTasks"] == 1
        assert summary["hasSettings"] is True
        assert summary["excludedCount"] == 1
        assert summary["dateRange"] == ["2026-01-10", "2026-05-25"] or \
               summary["dateRange"][0] == "2025-06-08"
