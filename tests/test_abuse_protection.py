"""濫用防護測試。

三道防線:
  1. 真實使用者識別 —— 取得代理後方的真實 IP,否則所有人共用一個額度
  2. 每 IP 每小時 20 次 —— 伺服器端強制,不可由前端繞過
  3. 對話歷史上限 —— 由前端帶上來,但伺服器必須自己設限

背景:上線後發現伺服器看到的來源永遠是 127.0.0.1(Render 的代理),
代表原本的「每人」限制實際上是全球共用 —— 一個人送出請求會擋住所有人。
"""

import json
import threading
import time

import pytest

import config
from ai.transport import FakeTransport
from server import Application, client_ip


def _post(app, path, payload, client="1.1.1.1"):
    return app.handle_post(path, json.dumps(payload).encode("utf-8"), client=client)


class TestClientIdentification:
    """代理後方的真實 IP 判定。"""

    def test_uses_socket_address_when_no_proxy(self):
        """本機開發沒有代理,直接用連線來源。"""
        assert client_ip({}, "203.0.113.9") == "203.0.113.9"

    def test_uses_forwarded_header_behind_proxy(self):
        headers = {"X-Forwarded-For": "203.0.113.9"}
        assert client_ip(headers, "127.0.0.1") == "203.0.113.9"

    def test_skips_infrastructure_hops_from_the_end(self, monkeypatch):
        """從尾端往回跳過基礎設施層,取第一個非基礎設施的位址。

        正式環境實測的轉發鏈:
            203.204.236.67, 172.71.146.124, 10.28.196.132
              真實使用者        Cloudflare      Render 內部

        最後一段是 Render 的內部負載平衡器,而且**每次請求都不同**
        (10.25.32.132 / 10.28.196.132 / 10.28.128.130),
        導致每次都被當成新使用者,限流完全失效。

        也不能單純取第一個 —— 那段是使用者送什麼就是什麼,
        等於讓攻擊者自行指定身分、無限重置額度。
        """
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        headers = {
            "X-Forwarded-For": "203.204.236.67, 172.71.146.124, 10.28.196.132"
        }
        assert client_ip(headers, "127.0.0.1") == "203.204.236.67"

    def test_same_user_gets_same_id_despite_changing_infra_hops(self, monkeypatch):
        """同一個使用者的身分不得因為內部節點輪替而改變。

        這正是限流失效的直接原因。
        """
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        ids = {
            client_ip(
                {"X-Forwarded-For": f"203.204.236.67, 172.71.146.124, {infra}"},
                "127.0.0.1",
            )
            for infra in ("10.25.32.132", "10.28.196.132", "10.28.128.130")
        }
        assert ids == {"203.204.236.67"}

    def test_forged_leading_entries_do_not_win(self, monkeypatch):
        """偽造前導項換不到新身分。

        攻擊者送出 'X-Forwarded-For: 9.9.9.9' 時,代理會把真實來源接在後面,
        偽造值被推到最前面。砍掉尾端的基礎設施層後,取到的仍是攻擊者的
        真實 IP —— 他還是受同一份額度約束,偽造沒有好處。
        """
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        headers = {
            "X-Forwarded-For": "9.9.9.9, 203.204.236.67, 172.71.146.124, 10.28.196.132"
        }
        assert client_ip(headers, "127.0.0.1") == "203.204.236.67"
        assert client_ip(headers, "127.0.0.1") != "9.9.9.9"

    def test_short_chain_falls_back_to_first_available(self, monkeypatch):
        """鏈比預期短時取最前面的,不可回傳空值或爆掉。"""
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        assert client_ip({"X-Forwarded-For": "203.0.113.9"}, "127.0.0.1") == "203.0.113.9"

    def test_handles_spaces(self, monkeypatch):
        monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 2)
        headers = {"X-Forwarded-For": "203.0.113.9,  5.6.7.8 , 10.0.0.1"}
        assert client_ip(headers, "127.0.0.1") == "203.0.113.9"

    def test_ignores_empty_header(self):
        assert client_ip({"X-Forwarded-For": ""}, "203.0.113.9") == "203.0.113.9"

    def test_header_name_is_case_insensitive(self):
        headers = {"x-forwarded-for": "203.0.113.9"}
        assert client_ip(headers, "127.0.0.1") == "203.0.113.9"


class TestHourlyLimit:
    """每 IP 每小時 20 次。"""

    @pytest.fixture
    def app(self, monkeypatch):
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        monkeypatch.setattr(config, "MAX_QUESTIONS_PER_HOUR", 3)
        return Application(transport=FakeTransport(chunks=["ok"]))

    def test_allows_up_to_the_limit(self, app):
        for i in range(3):
            status, _ = _post(app, "/api/consult", {"question": f"第{i}題"})
            assert status == 200, f"第 {i + 1} 次不該被擋"

    def test_blocks_beyond_the_limit(self, app):
        for i in range(3):
            _post(app, "/api/consult", {"question": f"第{i}題"})
        status, body = _post(app, "/api/consult", {"question": "第四題"})
        assert status == 429
        assert body["reason"] == "hourly_limit"

    def test_different_ips_have_separate_quotas(self, app):
        for i in range(3):
            _post(app, "/api/consult", {"question": f"甲{i}"}, client="1.1.1.1")
        status, _ = _post(app, "/api/consult", {"question": "乙"}, client="2.2.2.2")
        assert status == 200, "另一個 IP 不該受影響"

    def test_message_tells_user_when_to_retry(self, app):
        for i in range(3):
            _post(app, "/api/consult", {"question": f"第{i}題"})
        _, body = _post(app, "/api/consult", {"question": "超過"})
        assert "小時" in body["error"]

    def test_old_requests_fall_out_of_the_window(self, app, monkeypatch):
        """滿一小時後額度應該回復,而不是永久鎖死。"""
        for i in range(3):
            _post(app, "/api/consult", {"question": f"第{i}題"})
        assert _post(app, "/api/consult", {"question": "被擋"})[0] == 429

        # 讓時間前進超過一小時
        real = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: real + 3601)
        assert _post(app, "/api/consult", {"question": "應放行"})[0] == 200

    def test_grading_is_not_counted(self, app):
        """生產健檢是純計算、不花 AI 額度,不該佔用問答次數。"""
        for i in range(5):
            status, _ = _post(app, "/api/grade", {"values": {"psy": 20.63}})
            assert status == 200
        assert _post(app, "/api/consult", {"question": "仍應放行"})[0] == 200

    def test_rejected_requests_do_not_consume_quota(self, app):
        """被擋下的請求本身不該再記一次。

        若被擋的嘗試也計入,使用者越是重試,額度恢復時間就被推得越晚,
        等於因為「被擋」而受到額外懲罰。
        """
        for i in range(3):
            _post(app, "/api/consult", {"question": f"第{i}題"})
        for _ in range(5):
            _post(app, "/api/consult", {"question": "重複嘗試"})

        assert len(app._hourly_hits["1.1.1.1"]) == 3, (
            "只有實際放行的 3 次該被記錄"
        )


class TestConcurrency:
    """同時湧入的請求不得繞過限制。

    實測發現的漏洞:對正式環境同時送出兩個請求,兩個都通過了。
    原因是「檢查」與「記錄」之間有空隙 —— 兩個執行緒同時讀到舊值,
    都判定可放行,才各自寫入。攻擊者只要同時灌請求就能突破上限,
    而伺服器是多執行緒的,這在正式環境隨時會發生。
    """

    @staticmethod
    def _widen_race_window(app):
        """撐開「判定放行」與「寫入紀錄」之間的空隙,讓競爭條件穩定重現。

        這個空隙在真實環境中極窄,本機照常跑幾乎撞不到 —— 但它確實存在:
        延遲寫入後,10 個併發請求會全部通過(正確行為是只有 1 個)。
        攻擊者同時灌請求就能突破上限,而伺服器本來就是多執行緒的。

        延遲「寫入」而非「讀取」很重要:讀取延遲只是讓所有執行緒一起等,
        撐不開真正的空窗,測不出問題。
        """
        class SlowWriteDict(dict):
            def __setitem__(self, key, value):
                time.sleep(0.05)
                super().__setitem__(key, value)

        app._last_ai_request = SlowWriteDict(app._last_ai_request)
        app._hourly_hits = SlowWriteDict(app._hourly_hits)

    def _hammer(self, app, count, client="1.1.1.1"):
        """同時送出 count 個請求,回傳成功的次數。

        用 Barrier 讓所有執行緒在同一瞬間進入,模擬攻擊者的併發灌流。
        """
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(count)

        def worker(i):
            barrier.wait()
            status, _ = _post(app, "/api/consult", {"question": f"併發{i}"}, client=client)
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return sum(1 for s in results if s == 200)

    def test_interval_limit_holds_under_concurrency(self, monkeypatch):
        """3 秒間隔:同時送出 10 個,只該有 1 個通過。"""
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 3)
        monkeypatch.setattr(config, "MAX_QUESTIONS_PER_HOUR", 1000)
        app = Application(transport=FakeTransport(chunks=["ok"]))
        self._widen_race_window(app)

        passed = self._hammer(app, 10)
        assert passed == 1, f"應只有 1 個通過,實際 {passed} 個"

    def test_hourly_limit_holds_under_concurrency(self, monkeypatch):
        """每小時上限:同時送出 30 個,通過數不得超過上限。"""
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        monkeypatch.setattr(config, "MAX_QUESTIONS_PER_HOUR", 5)
        app = Application(transport=FakeTransport(chunks=["ok"]))
        self._widen_race_window(app)

        passed = self._hammer(app, 30)
        assert passed <= 5, f"上限 5 次,併發下卻放行了 {passed} 次"

    def test_recorded_hits_match_what_was_allowed(self, monkeypatch):
        """記錄的次數不能超過上限,否則額度恢復時間會被算錯。"""
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        monkeypatch.setattr(config, "MAX_QUESTIONS_PER_HOUR", 5)
        app = Application(transport=FakeTransport(chunks=["ok"]))
        self._widen_race_window(app)

        self._hammer(app, 30)
        assert len(app._hourly_hits["1.1.1.1"]) <= 5


class TestMemoryBounded:
    """記錄不能無限成長,否則防護本身變成攻擊面。"""

    def test_stale_ips_are_pruned(self, monkeypatch):
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        app = Application(transport=FakeTransport(chunks=["ok"]))

        for i in range(50):
            _post(app, "/api/consult", {"question": "問題"}, client=f"10.0.0.{i}")

        real = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: real + 7200)
        _post(app, "/api/consult", {"question": "觸發清理"}, client="10.0.1.1")

        assert len(app._hourly_hits) <= 2, (
            f"過期 IP 未被清理,累積 {len(app._hourly_hits)} 筆"
        )


class TestQuestionLength:
    """單題字數上限。"""

    @pytest.fixture
    def app(self, monkeypatch):
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        return Application(transport=FakeTransport(chunks=["ok"]))

    def test_limit_is_2000(self):
        assert config.MAX_QUESTION_CHARS == 2000

    def test_accepts_exactly_at_limit(self, app):
        status, _ = _post(app, "/api/consult", {"question": "痢" * 2000})
        assert status == 200

    def test_rejects_over_limit(self, app):
        status, body = _post(app, "/api/consult", {"question": "痢" * 2001})
        assert status == 400
        assert "2000" in body["error"]

    def test_overlong_question_does_not_reach_ai(self):
        """超長輸入必須在呼叫 AI 之前就擋下,否則白花錢。"""
        transport = FakeTransport(chunks=["不該被呼叫"])
        app = Application(transport=transport)
        _post(app, "/api/consult", {"question": "痢" * 5000})
        assert transport.last_prompt is None


class TestMalformedFieldTypes:
    """欄位型別錯誤必須回明確錯誤,不可讓執行緒崩潰。

    資安稽核實測發現:送出 {"question": {"a":"b"}} 時,伺服器回 HTTP 200
    但串流裡「零個事件」—— 連線被切斷,使用者畫面永遠卡在「顧問思考中…」,
    沒有任何說明。原因是 (question or "").strip() 對非字串會拋 AttributeError。
    網頁介面不會送出這種資料,但任何人直接呼叫 API 就會觸發。
    """

    @pytest.fixture
    def app(self, monkeypatch):
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        return Application(transport=FakeTransport(chunks=["ok"]))

    @pytest.mark.parametrize("bad", [
        {"a": "b"}, 12345, ["a", "b"], True, 3.14,
    ])
    def test_non_string_question_returns_clear_error(self, app, bad):
        status, body = _post(app, "/api/consult", {"question": bad})
        assert status == 400, f"question={bad!r} 應回 400,實際 {status}"
        assert "文字" in body["error"]

    @pytest.mark.parametrize("bad", [{"a": "b"}, 12345, ["a", "b"]])
    def test_malformed_question_never_reaches_ai(self, bad):
        """壞掉的輸入不該花錢呼叫 AI。"""
        transport = FakeTransport(chunks=["不該被呼叫"])
        app = Application(transport=transport)
        _post(app, "/api/consult", {"question": bad})
        assert transport.last_prompt is None

    def test_stream_still_emits_an_error_event(self, app):
        """串流路徑也要吐出錯誤事件,不能靜默斷線。"""
        events = list(app.consult_events({"question": {"a": "b"}}, "1.1.1.1"))
        assert events, "不可零事件 —— 前端會永遠卡在載入中"
        assert events[-1]["type"] == "error"


class TestRequestSizeLimit:
    """請求體大小上限。

    資安稽核實測:送出 19.7 MB 請求體,伺服器照單全收並正常處理。
    更關鍵的是順序 —— do_POST 先把整包讀進記憶體,之後才輪到限流,
    所以「每小時 20 次」完全擋不住這件事。Render 免費方案只有 512MB 記憶體。
    """

    def test_limit_is_configured(self):
        assert config.MAX_REQUEST_BYTES > 0
        # 正常請求:2000 字問題 + 20 則歷史 × 500 字 ≈ 30KB,64KB 綽綽有餘
        assert config.MAX_REQUEST_BYTES <= 128 * 1024

    def test_oversized_body_is_rejected(self):
        from server import too_large
        assert too_large(config.MAX_REQUEST_BYTES + 1) is True

    def test_normal_body_is_allowed(self):
        from server import too_large
        assert too_large(30 * 1024) is False

    def test_missing_length_is_allowed(self):
        from server import too_large
        assert too_large(0) is False

    def test_real_payload_fits_comfortably(self):
        """實際最大合法請求要能通過,不能把正常使用者擋掉。"""
        from server import too_large
        payload = json.dumps({
            "question": "痢" * config.MAX_QUESTION_CHARS,
            "history": [{"role": "user", "content": "痢" * config.MAX_HISTORY_CHARS}]
                       * config.MAX_HISTORY_TURNS,
        }, ensure_ascii=False).encode("utf-8")
        assert too_large(len(payload)) is False, (
            f"合法請求 {len(payload)} bytes 被擋,上限設太小"
        )


class TestHistoryLimit:
    """對話歷史上限。歷史由前端帶上來,伺服器不能照單全收。"""

    @pytest.fixture
    def app(self, monkeypatch):
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        return Application(transport=FakeTransport(chunks=["ok"]))

    def test_limit_is_20(self):
        assert config.MAX_HISTORY_TURNS == 20

    def test_keeps_only_the_most_recent_turns(self):
        transport = FakeTransport(chunks=["ok"])
        app = Application(transport=transport)
        history = [
            {"role": "user", "content": f"第{i}題"} for i in range(50)
        ]
        _post(app, "/api/consult", {"question": "現在這題", "history": history})

        assert "第49題" in transport.last_prompt, "最近的對話應保留"
        assert "第0題" not in transport.last_prompt, "過舊的對話應丟棄"

    def test_oversized_history_is_truncated_not_rejected(self, app):
        """歷史過長就截斷,不要整個請求失敗 —— 使用者沒做錯事。"""
        history = [{"role": "user", "content": "舊問題"} for _ in range(200)]
        status, _ = _post(app, "/api/consult", {"question": "新問題", "history": history})
        assert status == 200

    def test_history_entries_are_length_capped(self):
        """單則歷史也要限長,否則 20 則各塞 10 萬字一樣能灌爆成本。"""
        transport = FakeTransport(chunks=["ok"])
        app = Application(transport=transport)
        _post(app, "/api/consult", {
            "question": "新問題",
            "history": [{"role": "user", "content": "痢" * 50000}],
        })
        assert len(transport.last_prompt) < 50000

    def test_works_without_history(self, app):
        status, _ = _post(app, "/api/consult", {"question": "第一次提問"})
        assert status == 200

    def test_malformed_history_is_ignored(self, app):
        """前端送來的東西不可信,壞掉的歷史不該讓伺服器崩潰。"""
        for bad in ("字串", 123, [{"沒有": "role"}], [None], {"不是": "陣列"}):
            status, _ = _post(app, "/api/consult", {"question": "問題", "history": bad})
            assert status == 200, f"history={bad!r} 應被忽略而非報錯"
