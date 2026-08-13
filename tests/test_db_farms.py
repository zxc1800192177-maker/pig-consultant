"""v2 資料層:以牧場為單位的隔離。

**憲法第十一條要求兩個方向都測:**
  A 牧場看不到 B 牧場的資料  ← v1 就有的方向
  同牧場的兩個使用者看得到彼此的記錄  ← v2 新增,而且是這次改架構的理由

只測前者會漏掉真正的需求:牧場主與員工共用同一批母豬資料,若沿用 v1 的
「以使用者隔離」,員工記的東西牧場主看不到,系統就沒用了。

全部用 InMemoryStore,不連真的資料庫 —— 測試要能離線跑、幾秒跑完。
InMemoryStore 的行為必須與 PostgresStore 一致,尤其是隔離:這裡漏掉隔離
會讓測試通過而正式環境外洩,比沒有測試更糟。
"""

from datetime import date

import pytest

from db import InMemoryStore


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def two_farms(store):
    """兩個牧場,各有一位牧場主。"""
    a = store.create_farm("A 牧場")
    b = store.create_farm("B 牧場")
    return a, b


class TestFarmIsolation:
    """A 牧場看不到 B 牧場的任何資料。每一種實體都要測到。"""

    def test_sows(self, store, two_farms):
        a, b = two_farms
        store.add_sow(a, "1183")
        assert store.list_sows(b) == []

    def test_boars(self, store, two_farms):
        a, b = two_farms
        store.add_boar(a, "D6")
        assert store.list_boars(b) == []

    def test_pens(self, store, two_farms):
        a, b = two_farms
        store.add_pen(a, "產房 A-01")
        assert store.list_pens(b) == []

    def test_events(self, store, two_farms):
        a, b = two_farms
        sow = store.add_sow(a, "1183")
        store.add_sow_event(a, sow, "FW", date(2026, 2, 4))
        assert store.list_sow_events(b) == []

    def test_custom_tasks(self, store, two_farms):
        a, b = two_farms
        store.add_custom_task(a, "消毒豬舍", date(2026, 8, 11), "weekly")
        assert store.list_custom_tasks(b) == []

    def test_cannot_read_another_farms_sow_by_id(self, store, two_farms):
        """知道 id 也拿不到 —— 光靠「前端只會傳自己的 id」不算數。"""
        a, b = two_farms
        sow = store.add_sow(a, "1183")
        assert store.get_sow(b, sow) is None

    def test_cannot_delete_another_farms_sow(self, store, two_farms):
        a, b = two_farms
        sow = store.add_sow(a, "1183")
        assert store.delete_sow(b, sow) is False
        assert len(store.list_sows(a)) == 1

    def test_cannot_update_another_farms_sow(self, store, two_farms):
        a, b = two_farms
        sow = store.add_sow(a, "1183")
        assert store.update_sow(b, sow, parity=99) is False
        assert store.get_sow(a, sow)["parity"] == 0

    def test_cannot_exclude_another_farms_event(self, store, two_farms):
        a, b = two_farms
        sow = store.add_sow(a, "1183")
        ev = store.add_sow_event(a, sow, "FW", date(2026, 2, 4))
        assert store.set_event_excluded(b, ev, True) is False

    def test_cannot_mark_another_farms_task(self, store, two_farms):
        a, b = two_farms
        task = store.add_custom_task(a, "消毒", date(2026, 8, 11))
        assert store.mark_task_done(b, task, date(2026, 8, 11)) is False

    def test_same_ear_tag_in_two_farms_is_fine(self, store, two_farms):
        """兩個牧場各有一頭 1183 是完全正常的,不該互相擋住。"""
        a, b = two_farms
        store.add_sow(a, "1183")
        store.add_sow(b, "1183")
        assert store.find_sow_by_tag(a, "1183")["farm_id"] == a
        assert store.find_sow_by_tag(b, "1183")["farm_id"] == b


class TestSameFarmSharing:
    """**這一組才是 v2 改架構的理由。**

    牧場主與員工共用同一批資料。若沿用 v1 的「以使用者隔離」,員工記的
    東西牧場主看不到,系統就沒用了。
    """

    @pytest.fixture
    def farm_with_two_users(self, store):
        farm = store.create_farm("HYD 牧場")
        owner = store.create_user("farmer", "hash")
        worker = store.create_user("worker", "hash")
        store.set_user_farm(owner, farm, "owner")
        store.set_user_farm(worker, farm, "worker")
        return farm, owner, worker

    def test_worker_records_owner_sees(self, store, farm_with_two_users):
        farm, owner, worker = farm_with_two_users
        sow = store.add_sow(farm, "1183")
        store.add_sow_event(farm, sow, "WN", date(2026, 2, 26),
                            {"weaned": 10}, recorded_by=worker)

        events = store.list_sow_events(farm, sow)
        assert len(events) == 1
        assert events[0]["detail"]["weaned"] == 10

    def test_event_records_who_entered_it(self, store, farm_with_two_users):
        """數字對不上時要查得到是誰記的(憲法第十一條第 3 款)。"""
        farm, owner, worker = farm_with_two_users
        sow = store.add_sow(farm, "1183")
        store.add_sow_event(farm, sow, "WN", date(2026, 2, 26), recorded_by=worker)
        assert store.list_sow_events(farm, sow)[0]["recorded_by"] == worker

    def test_both_users_belong_to_the_same_farm(self, store, farm_with_two_users):
        farm, owner, worker = farm_with_two_users
        assert store.get_user_by_id(owner)["farm_id"] == farm
        assert store.get_user_by_id(worker)["farm_id"] == farm

    def test_roles_are_recorded(self, store, farm_with_two_users):
        farm, owner, worker = farm_with_two_users
        assert store.get_user_by_id(owner)["role"] == "owner"
        assert store.get_user_by_id(worker)["role"] == "worker"


class TestEarTagIdentity:
    """耳號會在離群時加上民國年後綴,裸號釋放給新豬
    (specs/v2-facts.md 第 6 條)。
    """

    def test_same_tag_different_entry_date_is_allowed(self, store):
        """2580 淘汰改名成 2580-D115 之後,新的 2580 進場必須進得來。"""
        farm = store.create_farm("HYD")
        store.add_sow(farm, "2580-D115", entry_date=date(2023, 3, 17))
        store.add_sow(farm, "2580", entry_date=date(2026, 8, 1))
        assert len(store.list_sows(farm)) == 2

    def test_exact_duplicate_is_rejected(self, store):
        farm = store.create_farm("HYD")
        store.add_sow(farm, "2580", entry_date=date(2023, 3, 17))
        with pytest.raises(ValueError):
            store.add_sow(farm, "2580", entry_date=date(2023, 3, 17))

    def test_find_by_tag_ignores_culled(self, store):
        """離群的豬不該被耳號查出來 —— 否則新的 2580 會對到舊的那頭。"""
        farm = store.create_farm("HYD")
        old = store.add_sow(farm, "2580", entry_date=date(2023, 3, 17))
        store.update_sow(farm, old, status="culled", ear_tag="2580-D115")
        new = store.add_sow(farm, "2580", entry_date=date(2026, 8, 1))
        assert store.find_sow_by_tag(farm, "2580")["id"] == new


class TestEventDedupe:
    """匯入必須可重複執行 —— 同一份檔案匯兩次不該產生兩倍事件。"""

    def test_same_event_twice_is_one_row(self, store):
        farm = store.create_farm("HYD")
        sow = store.add_sow(farm, "1183")
        first = store.add_sow_event(farm, sow, "FW", date(2026, 2, 4))
        again = store.add_sow_event(farm, sow, "FW", date(2026, 2, 4))
        assert first == again
        assert len(store.list_sow_events(farm, sow)) == 1

    def test_different_dates_are_separate(self, store):
        farm = store.create_farm("HYD")
        sow = store.add_sow(farm, "1183")
        store.add_sow_event(farm, sow, "MT", date(2026, 2, 3))
        store.add_sow_event(farm, sow, "MT", date(2026, 2, 4))
        assert len(store.list_sow_events(farm, sow)) == 2


class TestExcludedEvents:
    """匯入時的離群值把關:標記為不納入統計,但**不刪資料**。"""

    def test_excluded_row_still_exists(self, store):
        farm = store.create_farm("HYD")
        sow = store.add_sow(farm, "1585")
        ev = store.add_sow_event(farm, sow, "FW", date(2025, 10, 15),
                                 {"born_alive": 56})
        store.set_event_excluded(farm, ev, True)

        events = store.list_sow_events(farm, sow)
        assert len(events) == 1, "排除不等於刪除,母豬卡的時間軸仍要看得到"
        assert events[0]["excluded"] is True

    def test_can_be_undone(self, store):
        farm = store.create_farm("HYD")
        sow = store.add_sow(farm, "1585")
        ev = store.add_sow_event(farm, sow, "FW", date(2025, 10, 15))
        store.set_event_excluded(farm, ev, True)
        store.set_event_excluded(farm, ev, False)
        assert store.list_sow_events(farm, sow)[0]["excluded"] is False

    def test_events_default_to_included(self, store):
        farm = store.create_farm("HYD")
        sow = store.add_sow(farm, "1183")
        store.add_sow_event(farm, sow, "FW", date(2026, 2, 4))
        assert store.list_sow_events(farm, sow)[0]["excluded"] is False


class TestPenRelease:
    """母豬離群或離乳時要釋放產房欄位,否則產房永遠顯示滿的。"""

    def test_deleting_a_pen_clears_the_reference(self, store):
        farm = store.create_farm("HYD")
        pen = store.add_pen(farm, "A-03")
        sow = store.add_sow(farm, "1183")
        store.update_sow(farm, sow, pen_id=pen)

        store.delete_pen(farm, pen)
        assert store.get_sow(farm, sow)["pen_id"] is None


class TestBothImplementationsAgree:
    """Store 有兩個實作,漏掉一個方法只會在正式環境炸掉 —— InMemoryStore
    有的測試會過,PostgresStore 缺的那個要等真的部署才發現。
    """

    def test_postgres_implements_everything(self):
        from db import PostgresStore, Store
        missing = [
            name for name in vars(Store)
            if not name.startswith("_") and callable(getattr(Store, name))
            and name not in vars(PostgresStore)
        ]
        assert missing == [], f"PostgresStore 缺少:{missing}"

    def test_in_memory_implements_everything(self):
        from db import InMemoryStore, Store
        missing = [
            name for name in vars(Store)
            if not name.startswith("_") and callable(getattr(Store, name))
            and name not in vars(InMemoryStore)
        ]
        assert missing == [], f"InMemoryStore 缺少:{missing}"

    def test_signatures_match(self):
        """參數名稱與順序也要一致 —— 呼叫端用關鍵字參數時才不會一邊通一邊爆。"""
        import inspect
        from db import InMemoryStore, PostgresStore, Store
        bad = []
        for name in vars(Store):
            if name.startswith("_") or not callable(getattr(Store, name)):
                continue
            want = list(inspect.signature(getattr(Store, name)).parameters)
            for impl in (InMemoryStore, PostgresStore):
                if name in vars(impl):
                    got = list(inspect.signature(getattr(impl, name)).parameters)
                    if got != want:
                        bad.append(f"{impl.__name__}.{name}: {got} != {want}")
        assert bad == [], "\n".join(bad)


class TestDevMemoryStore:
    """本機開發用的記憶體 store。

    方便性的功能最怕的是溜到正式環境:資料看起來寫得進去,伺服器一重啟
    就全部消失,而且完全沒有錯誤訊息。所以「不得蓋掉真資料庫」這件事
    要有測試釘住,不能只靠註解提醒。
    """

    def test_off_by_default(self):
        from config import memory_db_enabled
        assert memory_db_enabled("", "") is False

    def test_opt_in_without_database_url(self):
        from config import memory_db_enabled
        assert memory_db_enabled("1", "") is True

    def test_real_database_always_wins(self):
        """設了 DATABASE_URL 就一定連真的,旗標留著也不生效。"""
        from config import memory_db_enabled
        assert memory_db_enabled("1", "postgresql://example/db") is False

    def test_unrecognised_values_are_off(self):
        """只認得幾個明確的開啟值。'0'、'no'、拼錯的字都算沒開。"""
        from config import memory_db_enabled
        for flag in ("0", "no", "yes", "ture", " 1"):
            assert memory_db_enabled(flag, "") is False, flag

    def test_select_store_returns_none_when_off(self, monkeypatch):
        import config as cfg
        import db
        monkeypatch.setattr(cfg, "DATABASE_URL", "")
        monkeypatch.setattr(cfg, "DEV_MEMORY_DB", False)
        assert db.select_store() is None

    def test_select_store_returns_memory_when_on(self, monkeypatch):
        import config as cfg
        import db
        monkeypatch.setattr(cfg, "DATABASE_URL", "")
        monkeypatch.setattr(cfg, "DEV_MEMORY_DB", True)
        assert isinstance(db.select_store(), InMemoryStore)


class TestDedupeIndexStaysCorrect:
    """InMemoryStore 用索引做判重(對應 PostgresStore 的唯一索引)。
    刪除後若沒清索引,重新新增會拿回一個已經不存在的 id。
    """

    def test_reinsert_after_delete_gets_a_fresh_id(self, store):
        farm = store.create_farm("HYD")
        sow = store.add_sow(farm, "1183")
        first = store.add_sow_event(farm, sow, "FW", date(2026, 2, 4))
        store.delete_sow_event(farm, first)

        again = store.add_sow_event(farm, sow, "FW", date(2026, 2, 4))
        assert store.set_event_excluded(farm, again, True) is True, (
            "拿回來的 id 必須真的存在,否則後續操作會靜默失敗")
        assert len(store.list_sow_events(farm, sow)) == 1

    def test_deleting_a_sow_frees_her_event_keys(self, store):
        farm = store.create_farm("HYD")
        sow = store.add_sow(farm, "1183", entry_date=date(2023, 1, 1))
        store.add_sow_event(farm, sow, "FW", date(2026, 2, 4))
        store.delete_sow(farm, sow)

        again = store.add_sow(farm, "1183", entry_date=date(2026, 1, 1))
        ev_id = store.add_sow_event(farm, again, "FW", date(2026, 2, 4))
        assert store.set_event_excluded(farm, ev_id, True) is True
