"""PostgresStore 的整場事件快取。

跟 test_db_farms.py 不一樣:那邊全部用 InMemoryStore,因為要測的是隔離
邏輯,兩邊行為理當一致。這裡測的東西 InMemoryStore 完全沒有 ——
list_sow_events(farm_id) 不快取沒有意義(InMemoryStore 本來就在記憶體裡,
沒有資料庫來回可以省),快取只對 PostgresStore 有意義。

**不連真的資料庫**:PostgresStore.__init__ 只存 dsn 字串,不會真的連線;
真正會嘗試連線的只有 _connect()(cache miss 時才呼叫)。這裡的測試全部
只操作快取本身(_event_cache 這個 dict、_cached_farm_events、
_invalidate_farm_events_cache),或是預先塞好快取讓 list_sow_events()
直接命中、不必真的碰資料庫——跟 PostgresStore 的其他行為一樣,沒有真的
連線可測時就不測連線本身,只測連線以外的邏輯。

**這個快取存在的理由**:使用者回報過網站被 Render 判定為記憶體不足而
強制關閉(exited with status 137)。查下去發現前端一次頁面載入會平行打出
好幾支 API(工作清單、提醒、值得檢視、生產月報、已記錄……),每一支都
各自向資料庫撈「這個牧場全部事件」——32,000+ 筆的牧場等於同一瞬間疊出
好幾份幾乎一樣的查詢結果,各自佔一份記憶體,免費方案只有 512MB。快取
幾秒鐘讓同一批平行請求共用同一份資料,直接砍掉這個尖峰。
"""

import time

import pytest

from db import PostgresStore


@pytest.fixture
def store():
    # 故意寫壞的 DSN,不是隨便一個網址 —— 若哪個測試不小心真的走到
    # _connect(),psycopg 會在解析階段就以 ProgrammingError 立刻失敗
    # (毫秒級),不會嘗試真的連網路、卡在 DNS 或連線逾時拖慢測試。
    return PostgresStore("not-a-valid-connection-string")


class TestEventCacheHelpers:
    def test_nothing_cached_returns_none(self, store):
        assert store._cached_farm_events(1) is None

    def test_cached_value_is_returned_within_ttl(self, store):
        store._event_cache[1] = (time.monotonic(), [{"id": 1}])
        assert store._cached_farm_events(1) == [{"id": 1}]

    def test_expired_entry_is_not_returned(self, store):
        """TTL 短是刻意的(見 db.py 的註解)——過期的快取不能再被當成
        有效資料,否則使用者記錄完馬上重整會看到舊資料。
        """
        stale_time = time.monotonic() - store._EVENT_CACHE_TTL - 1
        store._event_cache[1] = (stale_time, [{"id": 1}])
        assert store._cached_farm_events(1) is None

    def test_invalidate_clears_the_entry(self, store):
        store._event_cache[1] = (time.monotonic(), [{"id": 1}])
        store._invalidate_farm_events_cache(1)
        assert store._cached_farm_events(1) is None

    def test_invalidate_only_clears_the_named_farm(self, store):
        """A 牧場寫入不能把 B 牧場的快取也清掉 —— 那樣等於白快取。"""
        store._event_cache[1] = (time.monotonic(), [{"id": 1}])
        store._event_cache[2] = (time.monotonic(), [{"id": 2}])
        store._invalidate_farm_events_cache(1)
        assert store._cached_farm_events(1) is None
        assert store._cached_farm_events(2) == [{"id": 2}]

    def test_invalidating_an_uncached_farm_does_not_error(self, store):
        store._invalidate_farm_events_cache(999)


class TestCacheVersioningPreventsStaleWrites:
    """實際發生過的 bug:使用者記錄配種後,那筆沒出現在「已記錄」欄位裡。

    根因是競態條件——舊寫法的 _invalidate_farm_events_cache 只是單純
    dict.pop。如果一次「查全場事件」的查詢開始時快取剛好是空的(還沒有
    人查過),pop 等於沒清;查詢還在跑的期間如果有另一個請求寫入新事件
    並想清快取,清的也是「空」,清了等於沒清。等這次慢查詢終於跑完,
    它手上的(不包含新事件的)舊結果會被原封不動寫進快取,把新事件
    「蓋」到看不見,直到 TTL 過期為止。

    修法是版本號:查詢開始前記下版本號,寫入快取前比對版本號有沒有變。
    這裡不需要真的開執行緒模擬平行——「查詢開始時記下版本號」跟
    「查詢結束時準備寫入」之間插入一次 _invalidate,就是在模擬「查詢在
    跑的這段期間,另一個請求寫入並清了快取」,效果跟真的平行請求一樣。
    """

    def test_maybe_cache_writes_when_nothing_changed_during_the_query(self, store):
        version_before = store._farm_events_cache_version(5)
        store._maybe_cache_farm_events(5, version_before, [{"id": 1}])
        assert store._cached_farm_events(5) == [{"id": 1}]

    def test_maybe_cache_skips_a_stale_write_after_a_concurrent_invalidate(self, store):
        version_before = store._farm_events_cache_version(5)
        # 模擬:查詢還在跑的時候,另一個請求寫入新事件並清了快取。
        store._invalidate_farm_events_cache(5)
        # 慢查詢終於跑完,想把(不包含那筆新事件的)舊結果寫回快取——
        # 這次寫入必須被拒絕,否則使用者剛記的那筆就會被蓋成看不見。
        store._maybe_cache_farm_events(5, version_before, [{"id": 1, "stale": True}])
        assert store._cached_farm_events(5) is None

    def test_stale_write_does_not_resurrect_after_repeated_invalidates(self, store):
        version_before = store._farm_events_cache_version(5)
        store._invalidate_farm_events_cache(5)
        store._invalidate_farm_events_cache(5)
        store._maybe_cache_farm_events(5, version_before, [{"id": 1, "stale": True}])
        assert store._cached_farm_events(5) is None

    def test_invalidate_only_bumps_the_named_farm(self, store):
        v1 = store._farm_events_cache_version(1)
        v2 = store._farm_events_cache_version(2)
        store._invalidate_farm_events_cache(1)
        assert store._farm_events_cache_version(1) == v1 + 1
        assert store._farm_events_cache_version(2) == v2


class TestListSowEventsUsesTheCache:
    """直接測 list_sow_events() 本身,不繞過公開介面 —— 但只測快取命中
    的路徑(不必連資料庫),快取沒命中時才會走到 _connect()。
    """

    def test_whole_farm_query_hits_the_cache(self, store):
        store._event_cache[7] = (time.monotonic(), [{"id": 1, "sow_id": 5}])
        assert store.list_sow_events(7) == [{"id": 1, "sow_id": 5}]

    def test_returned_rows_are_independent_copies(self, store):
        """呼叫端拿到的字典是自己的一份,改了不會影響快取本身,也不會
        影響下一個呼叫端拿到的資料 —— 這是 list_sow_events() 原本就有
        的約定(每次呼叫都回一份新的 dict),加了快取不能破壞它。
        """
        store._event_cache[7] = (time.monotonic(), [{"id": 1, "excluded": False}])
        first = store.list_sow_events(7)
        first[0]["excluded"] = True

        second = store.list_sow_events(7)
        assert second[0]["excluded"] is False

    def test_filtered_query_does_not_read_the_whole_farm_cache(self, store):
        """帶 sow_id 就不是「查全場」,不能被快取命中路徑攔下來直接回傳
        全場資料給某一頭豬的查詢 —— 這裡用一個必定連不上的假 DSN 確認它
        真的想去連資料庫(NOT 命中快取),而不是誤判成命中。
        """
        store._event_cache[7] = (time.monotonic(), [{"id": 1, "sow_id": 999}])
        with pytest.raises(Exception):
            store.list_sow_events(7, sow_id=5)

    def test_since_until_filters_also_bypass_the_cache(self, store):
        from datetime import date
        store._event_cache[7] = (time.monotonic(), [{"id": 1}])
        with pytest.raises(Exception):
            store.list_sow_events(7, since=date(2026, 1, 1))
