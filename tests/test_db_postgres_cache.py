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

import threading
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


class _SlowCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _SlowConnection:
    """假連線:execute() 會先睡一段時間,模擬一次真的很慢的整場查詢。

    假在 _connect() 這一層,而不是為了測試在 db.py 開一個
    「可以覆寫的查詢方法」—— 正式程式碼不該為了被測而長出接縫,
    list_sow_events() 的真實流程(組 SQL、_rows()、解 detail、
    決定要不要快取)在這個測試裡是原封不動跑過一遍的。
    """

    def __init__(self, rows, delay):
        self._rows = rows
        self._delay = delay

    def execute(self, sql, args=None):
        time.sleep(self._delay)
        return _SlowCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _SlowFarmStore(PostgresStore):
    """整場查詢很慢的 PostgresStore,好在測試裡用真的執行緒重現競態。

    上面 TestCacheVersioningPreventsStaleWrites 是手動把 _invalidate 插在
    讀取的兩個步驟之間來模擬;這裡是真的兩條執行緒同時跑、真的有一段
    查詢時間可以被插隊。兩種都要有:前者精確,後者真實。
    """

    # 對照 PostgresStore.EVENT_COLS 的欄位順序
    ROW = (1, 1, 5, "MT", "2026-08-18", {}, 0, None, False)

    def __init__(self, delay):
        self.delay = delay
        super().__init__("not-a-valid-connection-string")

    def _connect(self):
        return _SlowConnection([self.ROW], self.delay)

    @property
    def expected_rows(self):
        names = [c.strip() for c in self.EVENT_COLS.split(",")]
        return [dict(zip(names, self.ROW))]


class TestConcurrentReadWriteDoesNotServeStaleData:
    """真並發:一條執行緒在慢慢查全場,另一條在它查到一半時寫入。

    這是使用者實際遇到的情形 —— 手機開頁面時有 5 支 API 平行查全場
    事件,人就在這時候按下「記錄」。舊寫法會讓那筆剛記的配種消失最多
    3 秒(TTL),使用者重新整理也看不到,正是回報的症狀。
    """

    def test_slow_read_started_before_a_write_does_not_poison_the_cache(self):
        store = _SlowFarmStore(delay=0.3)

        reader_result = {}

        def slow_reader():
            reader_result["rows"] = store.list_sow_events(1)

        t = threading.Thread(target=slow_reader)
        t.start()
        # 等讀取確實已經開始(已經記下版本號、正在「查詢」中)
        time.sleep(0.1)

        # 寫入發生:新事件進資料庫,快取失效。此刻快取是空的,舊寫法的
        # dict.pop 等於什麼也沒清 —— 這正是漏洞所在。
        store._invalidate_farm_events_cache(1)

        t.join(timeout=5)
        assert reader_result["rows"] == store.expected_rows, \
            "慢讀取本來就該回它查到的那份"

        # 關鍵斷言:那份「寫入前」的舊資料絕不能留在快取裡,否則接下來
        # 3 秒內每個人(包括剛記錄完重新整理的使用者)都會讀到舊的。
        assert store._cached_farm_events(1) is None, (
            "寫入期間開始的慢讀取,把寫入前的舊資料寫回了快取 —— "
            "剛記錄的事件會消失最多一個 TTL")

    def test_a_read_with_no_concurrent_write_still_caches(self):
        """反向確認上面那條不是靠「乾脆都不快取」蒙混過關的 —— 沒有人
        在中途寫入時,快取必須照常生效,不然這個快取就白加了。
        """
        store = _SlowFarmStore(delay=0.05)
        store.list_sow_events(1)
        assert store._cached_farm_events(1) == store.expected_rows


class _CountingStore(_SlowFarmStore):
    """記錄實際打到資料庫幾次。"""

    def __init__(self, delay):
        super().__init__(delay)
        self.queries = 0
        self._count_lock = threading.Lock()

    def _connect(self):
        with self._count_lock:
            self.queries += 1
        return super()._connect()


def _run_together(fn, n):
    """讓 n 條執行緒盡量同一瞬間進入 fn() —— 用 Barrier 而不是先後啟動,
    不然它們會自然錯開,測不到「同時抵達」這個要測的情形。
    """
    barrier = threading.Barrier(n)
    results, errors = [], []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            r = fn()
        except BaseException as e:      # noqa: BLE001 - 測試要看見任何失敗
            with lock:
                errors.append(e)
        else:
            with lock:
                results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "有執行緒卡住沒結束"
    return results, errors


class TestSimultaneousRequestsShareOneQuery:
    """同時抵達的整場查詢只能真的查一次。

    **這是快取本身解不掉的那一半**:前端開頁面會平行打出 5 支都要查全場
    事件的 API,它們幾乎同時抵達、同時發現快取是空的,於是各查各的 ——
    實測(加這個機制之前)5 條執行緒同時呼叫,資料庫真的被查了 5 次。
    32,000 筆的牧場同一瞬間疊出 5 份查詢結果,正是懷疑把免費方案 512MB
    衝爆、觸發 status 137 的原因,而 TTL 快取對這個情形完全沒有幫助。
    """

    def test_five_simultaneous_reads_hit_the_database_once(self):
        store = _CountingStore(delay=0.3)
        results, errors = _run_together(lambda: store.list_sow_events(1), 5)

        assert not errors, errors
        assert store.queries == 1, (
            f"5 條同時查全場,資料庫被查了 {store.queries} 次 —— "
            f"應該只有第一條真的去查,其餘等結果")
        assert len(results) == 5
        for rows in results:
            assert rows == store.expected_rows, "等結果的人也要拿到完整資料"

    def test_waiters_get_their_own_copies(self):
        """跟快取命中一樣的約定:每個呼叫端拿到的是自己的一份,改了不會
        影響別人 —— 共用查詢結果不能把這個約定弄丟。
        """
        store = _CountingStore(delay=0.2)
        results, errors = _run_together(lambda: store.list_sow_events(1), 4)
        assert not errors, errors

        results[0][0]["excluded"] = "動過了"
        for rows in results[1:]:
            assert rows[0]["excluded"] is False

    def test_a_failed_query_does_not_hang_the_waiters(self):
        """發起查詢的那條如果炸了(連線斷、資料庫掛掉),等待的人必須
        跟著收到錯誤,不能卡在那裡等一個永遠不會來的結果 —— 那會把
        整個伺服器的執行緒一條一條耗光。
        """
        class _BoomStore(_CountingStore):
            def _connect(self):
                super()._connect()
                time.sleep(self.delay)
                raise RuntimeError("資料庫連線失敗")

        store = _BoomStore(delay=0.2)
        results, errors = _run_together(lambda: store.list_sow_events(1), 5)

        assert not results
        assert len(errors) == 5, "五條都要收到錯誤,不能有人卡住"
        assert all(isinstance(e, RuntimeError) for e in errors)
        # 失敗不能把登記留在原地,否則之後的查詢會去等一個已經死掉的查詢
        assert store._inflight == {}

    def test_a_request_arriving_after_a_write_does_not_join_a_stale_query(self):
        """後到的請求不能搭上「寫入之前就開始」的那份查詢 —— 那份看不到
        剛寫入的資料。這跟 _maybe_cache_farm_events 擋的是同一件事,只是
        換成從共用查詢這個入口進來。
        """
        store = _CountingStore(delay=0.4)

        first = {}
        t = threading.Thread(
            target=lambda: first.update(rows=store.list_sow_events(1)))
        t.start()
        time.sleep(0.1)                      # 第一條確實已經在查了

        store._invalidate_farm_events_cache(1)   # 寫入發生

        # 這一條是寫入之後才進來的,必須自己查,不能搭順風車
        store.list_sow_events(1)
        t.join(timeout=5)

        assert store.queries == 2, (
            f"寫入後才抵達的請求搭上了寫入前開始的查詢(只查了 "
            f"{store.queries} 次)—— 它會拿到看不到新資料的舊結果")

    def test_the_inflight_registry_is_empty_once_everyone_is_done(self):
        store = _CountingStore(delay=0.1)
        _run_together(lambda: store.list_sow_events(1), 5)
        assert store._inflight == {}, "查完要把登記清掉,不能一直長大"

    def test_separate_farms_do_not_wait_for_each_other(self):
        """A 牧場的查詢不能擋住 B 牧場 —— 合流是「同一個牧場」才合,
        不然多牧場時大家會排成一列。
        """
        store = _CountingStore(delay=0.3)
        t0 = time.monotonic()
        results, errors = _run_together(
            lambda: store.list_sow_events(threading.get_ident() % 2 + 1), 6)
        elapsed = time.monotonic() - t0

        assert not errors, errors
        assert elapsed < 0.9, f"不同牧場被串成序列了,花了 {elapsed:.2f} 秒"


class _SlowSowStore(PostgresStore):
    """整群查詢很慢的 store,用來測母豬清單的合流。"""

    # 對照 PostgresStore.SOW_COLS 的欄位順序
    ROW = (1, 1, "1183", None, None, "", "", "", 0, "active", None, "", None, False)

    def __init__(self, delay):
        self.delay = delay
        self.queries = 0
        self._count_lock = threading.Lock()
        super().__init__("not-a-valid-connection-string")

    def _connect(self):
        with self._count_lock:
            self.queries += 1
        return _SlowConnection([self.ROW], self.delay)


class TestSimultaneousSowListsShareOneQuery:
    """母豬清單跟事件一樣是「開頁面時好幾支 API 同時撈」的昂貴查詢 ——
    server.py 有 7 處在撈全群。事件那一半先修了,這是剩下的另一半,也是
    當初 status 137 之後唯一還沒補的當機風險。

    **只做合流,不做快取**:母豬的狀態(胎次、耳號後綴、產房欄位)動得
    比事件頻繁,存幾秒鐘的舊清單風險遠大於省下的那次查詢。
    """

    def test_five_simultaneous_reads_hit_the_database_once(self):
        store = _SlowSowStore(delay=0.3)
        results, errors = _run_together(lambda: store.list_sows(1), 5)

        assert not errors, errors
        assert store.queries == 1, (
            f"5 支同時撈全群,資料庫被查了 {store.queries} 次")
        assert len(results) == 5
        assert all(len(r) == 1 for r in results), "等結果的人也要拿到資料"

    def test_waiters_get_their_own_copies(self):
        store = _SlowSowStore(delay=0.2)
        results, errors = _run_together(lambda: store.list_sows(1), 4)
        assert not errors, errors

        results[0][0]["ear_tag"] = "動過了"
        for rows in results[1:]:
            assert rows[0]["ear_tag"] == "1183"

    def test_a_filtered_query_does_not_join(self):
        """只要在場的那種便宜得多,而且不同 status 的結果不能互相共用。"""
        store = _SlowSowStore(delay=0.05)
        store.list_sows(1, "active")
        store.list_sows(1, "active")
        assert store.queries == 2

    def test_events_and_sows_queue_separately(self):
        """兩者共用同一套機制,但不能排進同一個隊伍 —— 母豬的查詢不該
        等在事件的查詢後面,那會讓開頁面變成一條龍。
        """
        store = _SlowSowStore(delay=0.3)
        t0 = time.monotonic()
        _run_together(lambda: store.list_sows(1), 3)
        one_kind = time.monotonic() - t0
        assert one_kind < 0.9, f"合流後不該變慢,花了 {one_kind:.2f} 秒"

    def test_a_failed_query_does_not_hang_the_waiters(self):
        class _Boom(_SlowSowStore):
            def _connect(self):
                super()._connect()
                time.sleep(self.delay)
                raise RuntimeError("資料庫連線失敗")

        store = _Boom(delay=0.2)
        results, errors = _run_together(lambda: store.list_sows(1), 5)
        assert not results
        assert len(errors) == 5, "五支都要收到錯誤,不能有人卡住"
        assert store._inflight == {}

    def test_a_write_stops_later_readers_joining_an_older_query(self):
        """寫入之後才抵達的請求,不能搭上寫入之前就開始的那次查詢 ——
        那份查詢看不到剛寫進去的那頭豬。
        """
        store = _SlowSowStore(delay=0.4)
        t = threading.Thread(target=lambda: store.list_sows(1))
        t.start()
        time.sleep(0.1)

        store._bump_sow_version(1)      # 新增/更新一頭母豬
        store.list_sows(1)              # 這支必須自己查
        t.join(timeout=5)

        assert store.queries == 2, (
            f"寫入後才抵達的請求搭上了寫入前開始的查詢(只查了 {store.queries} 次)")
