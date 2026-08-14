"""PigCHAMP 匯出檔的解析與匯入。

解析是純函式,所以這些測試不碰資料庫也不碰檔案系統 —— 匯入的部分才用
InMemoryStore。

**編碼是這個模組最容易出錯的地方**:同一批匯出檔裡 .txt 是 UTF-8、
CSV 是 Big5,而用錯編碼不會報錯,只會產生看起來像資料損壞的假象。
那個假象曾經被誤判成「5 筆欄位錯位」寫進規格(見 specs/v2-facts.md 第 8 條)。
"""

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


class TestImportIntoStore:
    @pytest.fixture
    def farm(self):
        store = InMemoryStore()
        return store, store.create_farm("HYD")

    def test_creates_sows_and_events(self, farm):
        store, farm_id = farm
        result = importer.parse("\n".join([MATE, FARROW, WEAN]))
        stats = importer.import_into(store, farm_id, result)

        assert stats == {"sows": 1, "events": 3, "excluded": 0, "boars": 0}
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
