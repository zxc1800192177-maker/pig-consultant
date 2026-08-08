"""HTTP 層測試。

伺服器只做路由、輸入檢查、限流,商業邏輯都在 core/ 與 ai/。
測試用假傳輸層,不消耗訂閱額度。
"""

import json

import pytest

import config
from ai.transport import FakeTransport, NotLoggedIn, QuotaExceeded
from server import Application

# monkeypatch.setattr 已在每個測試結束後自動還原 config 的修改,不需額外處理。


@pytest.fixture
def app():
    return Application(transport=FakeTransport(chunks=["建議內容"]))


def _post(app, path, payload):
    return app.handle_post(path, json.dumps(payload).encode("utf-8"), client="test")


class TestHealth:
    def test_reports_ai_availability(self, app):
        status, body = app.handle_get("/api/health")
        assert status == 200
        assert "aiAvailable" in body

    def test_grading_always_available(self, app):
        """規格 6.5:生產健檢不依賴 AI,健康檢查要能表達這件事。"""
        broken = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        _, body = broken.handle_get("/api/health")
        assert body["gradingAvailable"] is True


class TestGradeEndpoint:
    """生產健檢:純計算,不得呼叫 AI。"""

    VALUES = {"psy": 20.63, "preweaning_mortality": 20.21, "weaning_age": 21.97}

    def test_returns_grades(self, app):
        status, body = _post(app, "/api/grade", {"values": self.VALUES})
        assert status == 200
        assert body["grades"]["psy"]["grade"] == "D"

    def test_returns_ranked_weaknesses(self, app):
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        assert body["weaknesses"][0]["key"] == "weaning_age"

    def test_includes_source_label(self, app):
        """憲法第三條:顯示常模數字必須標註來源。"""
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        assert "2025" in body["source"]
        assert "110" in body["source"]

    def test_works_when_ai_is_down(self):
        """規格 6.5 驗收條件:切斷 AI 後健檢仍要跑得完。"""
        broken = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        status, body = _post(broken, "/api/grade", {"values": self.VALUES})
        assert status == 200
        assert body["weaknesses"]

    def test_never_calls_ai(self):
        transport = FakeTransport(chunks=["不該被呼叫"])
        app = Application(transport=transport)
        _post(app, "/api/grade", {"values": self.VALUES})
        assert transport.last_prompt is None

    def test_rejects_out_of_range(self, app):
        status, body = _post(app, "/api/grade", {"values": {"farrowing_rate": 300}})
        assert status == 400
        assert body["errors"]

    def test_passes_through_warnings(self, app):
        status, body = _post(app, "/api/grade", {
            "values": {"total_born_per_litter": 12.0, "live_born_per_litter": 13.0},
        })
        assert status == 200
        assert body["warnings"]

    def test_empty_values_is_ok(self, app):
        status, body = _post(app, "/api/grade", {"values": {}})
        assert status == 200
        assert body["grades"] == {}


class TestConsultEndpoint:
    def test_rejects_empty_question(self, app):
        status, _ = _post(app, "/api/consult", {"question": "  "})
        assert status == 400

    def test_rejects_overlong_question(self, app):
        status, _ = _post(app, "/api/consult", {
            "question": "痢" * (config.MAX_QUESTION_CHARS + 1),
        })
        assert status == 400

    def test_rejects_invalid_json(self, app):
        status, _ = app.handle_post("/api/consult", b"not json", client="test")
        assert status == 400

    def test_rejects_non_utf8(self, app):
        status, _ = app.handle_post("/api/consult", b"\xff\xfe", client="test")
        assert status == 400

    def test_unknown_path_is_404(self, app):
        status, _ = _post(app, "/api/nope", {})
        assert status == 404


class TestDailyBudgetGuard:
    """對外上線走 API 計費,失控會直接扣款(憲法第九條)。

    這是製程內的安全氣囊,不取代 console.anthropic.com 的計費上限。
    """

    def test_blocks_after_daily_limit(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_AI_REQUESTS_PER_DAY", 2)
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        app = Application(transport=FakeTransport(chunks=["ok"]))

        _post(app, "/api/consult", {"question": "第一題"})
        _post(app, "/api/consult", {"question": "第二題"})
        status, body = _post(app, "/api/consult", {"question": "第三題"})

        assert status == 503
        assert body["reason"] == "daily_limit"

    def test_grading_not_affected_by_daily_limit(self, monkeypatch):
        """健檢是純計算,不花 API 額度,不該被這個上限擋住。"""
        monkeypatch.setattr(config, "MAX_AI_REQUESTS_PER_DAY", 1)
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        app = Application(transport=FakeTransport(chunks=["ok"]))

        _post(app, "/api/consult", {"question": "第一題"})
        status, _ = _post(app, "/api/grade", {"values": {"psy": 20.63}})
        assert status == 200

    def test_counts_shared_across_all_clients(self, monkeypatch):
        """這是保護帳單,不是保護單一使用者,額度是全站共用。"""
        monkeypatch.setattr(config, "MAX_AI_REQUESTS_PER_DAY", 1)
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        app = Application(transport=FakeTransport(chunks=["ok"]))

        app.handle_post(
            "/api/consult", json.dumps({"question": "甲"}).encode(), client="a"
        )
        status, body = app.handle_post(
            "/api/consult", json.dumps({"question": "乙"}).encode(), client="b"
        )
        assert status == 503
        assert body["reason"] == "daily_limit"


class TestRateLimit:
    """憲法第九條:連續請求要有最短間隔,避免誤觸把額度燒光。"""

    def test_second_immediate_request_is_throttled(self, app):
        _post(app, "/api/consult", {"question": "第一題"})
        status, body = _post(app, "/api/consult", {"question": "第二題"})
        assert status == 429
        assert "秒" in body["error"]

    def test_grading_is_not_rate_limited(self, app):
        """健檢是純計算,不花額度,不該被限流擋住。"""
        _post(app, "/api/grade", {"values": {"psy": 20.63}})
        status, _ = _post(app, "/api/grade", {"values": {"psy": 20.63}})
        assert status == 200

    def test_different_clients_tracked_separately(self, app):
        app.handle_post("/api/consult", json.dumps({"question": "甲"}).encode(), client="a")
        status, _ = app.handle_post(
            "/api/consult", json.dumps({"question": "乙"}).encode(), client="b"
        )
        assert status == 200


class TestErrorMessages:
    """規格 6.5:AI 不可用時要說清楚原因,不能只丟通用錯誤。"""

    def test_quota_error_is_identifiable(self):
        app = Application(transport=FakeTransport(error=QuotaExceeded("額度用盡")))
        status, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert status == 503
        assert body["reason"] == "quota"

    def test_login_error_is_identifiable(self):
        app = Application(transport=FakeTransport(error=NotLoggedIn("尚未登入")))
        status, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert status == 503
        assert body["reason"] == "not_logged_in"
        assert "claude auth login" in body["error"]


class TestReportableAlwaysDelivered:
    """憲法第一條:通報須知不因 AI 失敗而消失。"""

    def test_baseline_notice_present_on_success(self, app):
        status, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert status == 200
        assert "無法判斷" in body["baselineNotice"]

    def test_escalation_present_on_keyword_hit(self, app):
        _, body = _post(app, "/api/consult", {"question": "懷疑非洲豬瘟"})
        assert body["escalation"]["disease"] == "非洲豬瘟"

    def test_notice_survives_ai_failure(self):
        """AI 掛掉時,防疫提示仍要送到使用者眼前。"""
        app = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        status, body = _post(app, "/api/consult", {"question": "懷疑非洲豬瘟"})
        assert status == 503
        assert body["escalation"]["disease"] == "非洲豬瘟"
        assert body["baselineNotice"]

    def test_disclaimer_included(self, app):
        _, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert "不代表安全" in body["disclaimer"]


class TestMetricsEndpoint:
    """前端要靠這個端點動態產生輸入欄位,不該把指標清單寫死在畫面裡。"""

    def test_returns_only_gradable_metrics(self, app):
        status, body = app.handle_get("/api/metrics")
        assert status == 200
        assert len(body["metrics"]) == 18

    def test_each_metric_has_field_info(self, app):
        _, body = app.handle_get("/api/metrics")
        for metric in body["metrics"]:
            assert metric["key"] and metric["name"]
            assert "unit" in metric
            assert metric["definition"]

    def test_includes_reportable_disclaimer(self, app):
        """畫面上必須永遠帶著這句,沒跳提示不等於安全。"""
        _, body = app.handle_get("/api/metrics")
        assert "不代表安全" in body["disclaimer"]

    def test_scale_metrics_not_offered_for_input(self, app):
        """規模型指標不評級,不該出現在輸入表單造成誤會。"""
        _, body = app.handle_get("/api/metrics")
        keys = {m["key"] for m in body["metrics"]}
        assert "total_services" not in keys


class TestStreamingEvents:
    """串流路徑與收攏路徑必須共用同一份邏輯,行為不得分歧。"""

    def _events(self, app, payload, client="test"):
        return list(app.consult_events(payload, client))

    def test_meta_arrives_before_any_text(self, app):
        """防疫提示要在 AI 開口之前就送出,使用者中途離開也已經看到。"""
        events = self._events(app, {"question": "懷疑非洲豬瘟"})
        kinds = [e["type"] for e in events]
        assert kinds[0] == "meta"
        assert kinds.index("meta") < kinds.index("delta")

    def test_escalation_in_meta_event(self, app):
        events = self._events(app, {"question": "懷疑非洲豬瘟"})
        assert events[0]["escalation"]["disease"] == "非洲豬瘟"

    def test_deltas_then_done(self, app):
        events = self._events(app, {"question": "小豬下痢"})
        assert events[-1]["type"] == "done"
        assert any(e["type"] == "delta" for e in events)

    def test_error_event_carries_status(self):
        app = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        events = list(app.consult_events({"question": "小豬下痢"}, "test"))
        error = next(e for e in events if e["type"] == "error")
        assert error["status"] == 503
        assert error["reason"] == "quota"

    def test_meta_still_sent_when_ai_fails(self):
        app = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        events = list(app.consult_events({"question": "懷疑非洲豬瘟"}, "test"))
        assert events[0]["type"] == "meta"
        assert events[0]["escalation"]

    def test_validation_error_has_no_meta(self, app):
        """問題根本不合法時,不該產生任何諮詢內容。"""
        events = self._events(app, {"question": "  "})
        assert [e["type"] for e in events] == ["error"]
        assert events[0]["status"] == 400


class TestExampleEndpoint:
    """demo 用的範例資料(合億畜牧場,已取得授權)。"""

    def test_returns_full_farm_values(self, app):
        status, body = app.handle_get("/api/example")
        assert status == 200
        assert len(body["values"]) == 18
        assert body["values"]["psy"] == 20.63

    def test_example_grades_match_official_report(self, app):
        """範例跑完的結果必須與官方報告一致,這是 demo 的說服點。"""
        _, example = app.handle_get("/api/example")
        _, body = _post(app, "/api/grade", {"values": example["values"]})
        assert body["grades"]["psy"]["grade"] == "D"
        assert body["grades"]["preweaning_mortality"]["grade"] == "E"
        assert body["grades"]["weaning_age"]["grade"] == "F"
