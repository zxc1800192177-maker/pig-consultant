"""HTTP 層測試。

伺服器只做路由、輸入檢查、限流,商業邏輯都在 core/ 與 ai/。
測試用假傳輸層,不消耗訂閱額度。
"""

import json
import re

import pytest

import config
from ai.transport import AnthropicApiTransport, FakeTransport, NotLoggedIn, QuotaExceeded
from server import CLEAR_SESSION_KEY, SET_SESSION_KEY, WEB_DIR, Application

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
    """規格 6.5:AI 不可用時要說清楚原因,不能只丟通用錯誤。

    曾實際發生的 bug:伺服器不論哪個傳輸層丟出 NotLoggedIn,一律顯示寫死的
    「Claude CLI 尚未登入」文字。API 傳輸層的 401(金鑰無效)因此被蓋成
    CLI 未登入的訊息,診斷方向整個被帶偏——這正是這裡的測試在防的事。
    """

    def test_quota_error_is_identifiable(self):
        app = Application(transport=FakeTransport(error=QuotaExceeded("額度用盡")))
        status, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert status == 503
        assert body["reason"] == "quota"

    def test_error_message_is_not_overwritten_by_server(self):
        """伺服器不得覆蓋傳輸層自己產生的錯誤文字,只能分類、不能改寫內容。"""
        app = Application(transport=FakeTransport(error=NotLoggedIn("尚未登入")))
        status, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert status == 503
        assert body["reason"] == "not_logged_in"
        assert body["error"] == "尚未登入"

    def test_cli_and_api_not_logged_in_produce_different_messages(self):
        """同樣是 NotLoggedIn,兩個傳輸層的措辭必須各自準確,不能共用一句文字。

        CLI 沒登入該講「請執行 claude auth login」,
        API 金鑰無效該講「請確認 ANTHROPIC_API_KEY」——兩者對應到完全不同的
        修復動作,講錯了會讓人去改錯地方。
        """
        cli_app = Application(transport=FakeTransport(
            error=NotLoggedIn("Claude CLI 尚未登入,請執行 claude auth login --claudeai")
        ))
        api_app = Application(transport=FakeTransport(
            error=NotLoggedIn("API key 無效或未授權,請確認 ANTHROPIC_API_KEY")
        ))

        _, cli_body = _post(cli_app, "/api/consult", {"question": "小豬下痢"})
        _, api_body = _post(api_app, "/api/consult", {"question": "小豬下痢"})

        assert cli_body["error"] != api_body["error"]
        assert "claude auth login" in cli_body["error"]
        assert "ANTHROPIC_API_KEY" in api_body["error"]
        assert "ANTHROPIC_API_KEY" not in cli_body["error"]
        assert "claude auth login" not in api_body["error"]

    def test_real_api_transport_401_message_reaches_the_response_unaltered(self):
        """整條路徑串起來測(非 fake):真的 401 錯誤要原封不動送到回應裡。"""
        import io
        import urllib.error

        transport = AnthropicApiTransport(api_key="sk-invalid-for-test")

        def raise_401(req):
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {}, fp=io.BytesIO(b"{}")
            )

        transport._open = raise_401
        app = Application(transport=transport)

        status, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert status == 503
        assert body["reason"] == "not_logged_in"
        assert "ANTHROPIC_API_KEY" in body["error"]
        assert "claude" not in body["error"].lower()


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


class TestMedicalDisclaimer:
    """醫療免責必須由程式強制附加,且在回答之前送達。

    這個系統會給出藥品劑量與休藥期。休藥期講錯 -> 藥物殘留的豬肉進入
    食物鏈 -> 受害的是第三方消費者。免責只放頁尾不夠:使用者拿到用藥
    建議時看的是畫面中間,不一定會捲到底部。
    """

    def test_consult_carries_disclaimer(self, app):
        _, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert "獸醫師確認" in body["medicalDisclaimer"]
        assert "休藥期" in body["medicalDisclaimer"]

    def test_disclaimer_arrives_before_any_answer_text(self, app):
        events = list(app.consult_events({"question": "小豬下痢"}, "test"))
        kinds = [e["type"] for e in events]
        assert events[0]["medicalDisclaimer"]
        assert kinds.index("meta") < kinds.index("delta")

    def test_disclaimer_survives_ai_failure(self):
        """AI 掛掉時免責仍要送達 —— 使用者可能只看到前面那段。"""
        app = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        _, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert body["medicalDisclaimer"]

    def test_grade_result_carries_disclaimer(self, app):
        """健檢的 AI 改善建議同樣涉及用藥,也要帶免責。"""
        _, body = _post(app, "/api/grade", {"values": {"psy": 20.63}})
        assert body["medicalDisclaimer"]

    def test_not_generated_by_ai(self):
        """同樣的請求必須得到逐字相同的免責條 —— 若交給 AI 寫就做不到。"""
        app = Application(transport=FakeTransport(chunks=["甲"]))
        texts = set()
        for i in range(3):
            _, body = _post(app, "/api/grade", {"values": {"psy": 20.63}})
            texts.add(body["medicalDisclaimer"])
        assert len(texts) == 1


class TestDosageLookup:
    """劑量查表化。結果是伺服器算出來的,不是 AI 生成的 —— 要跟 AI 文字
    分開送到前端(meta 事件 vs delta 事件),前端才能各自呈現。
    """

    def test_meta_always_carries_dosage_reference_field(self, app):
        """就算查無資料,欄位本身也要在,前端才知道『查了但沒有』跟
        『這個版本根本沒做這個功能』是兩回事。
        """
        _, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert "dosageReference" in body

    def test_returns_verified_entries_for_matching_symptom(self, app):
        """三筆官方手冊資料經 Ian review 後已授權顯示,相關症狀要查得到。"""
        _, body = _post(app, "/api/consult", {"question": "小豬下痢已經兩天"})
        assert body["dosageReference"] != []
        assert body["dosageReference"][0]["drugs"]

    def test_empty_for_unrelated_question(self, app):
        _, body = _post(app, "/api/consult", {"question": "豬隻精神沉鬱食慾不振"})
        assert body["dosageReference"] == []

    def test_survives_ai_failure(self):
        """AI 掛掉時,已經算好的比對結果仍要送達。"""
        app = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        _, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert "dosageReference" in body

    def test_dosage_reference_arrives_before_any_answer_text(self, app):
        events = list(app.consult_events({"question": "小豬下痢"}, "test"))
        kinds = [e["type"] for e in events]
        assert "dosageReference" in events[0]
        assert kinds.index("meta") < kinds.index("delta")


class TestMyDrugsInventory:
    """牧場主自己的藥品庫。來自瀏覽器 localStorage,一樣不可信,
    伺服器要能安全處理格式錯誤的輸入,並讓內容確實影響 AI 看到的提示詞。
    """

    def test_accepted_and_reaches_the_model(self):
        transport = FakeTransport(chunks=["ok"])
        app = Application(transport=transport)
        status, _ = _post(app, "/api/consult", {
            "question": "小豬下痢",
            "myDrugs": [{"name": "阿莫西林可溶性粉", "dosageNote": "每公斤10mg"}],
        })
        assert status == 200
        assert "阿莫西林可溶性粉" in transport.last_prompt

    def test_malformed_shape_does_not_crash_request(self, app):
        """跟 history 一樣:壞掉的格式直接忽略,不是回 500。"""
        status, _ = _post(app, "/api/consult", {
            "question": "小豬下痢",
            "myDrugs": "不是陣列",
        })
        assert status == 200

    def test_entries_missing_required_name_are_dropped(self):
        transport = FakeTransport(chunks=["ok"])
        app = Application(transport=transport)
        _post(app, "/api/consult", {
            "question": "小豬下痢",
            "myDrugs": [{"dosageNote": "沒有名字的藥"}],
        })
        assert "沒有名字的藥" not in transport.last_prompt

    def test_missing_my_drugs_still_works(self, app):
        status, _ = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert status == 200


class TestIsWeakComesFromBackend:
    """弱項判斷規則只存在後端(DRY)。

    前端原本自己維護一份「D 級以下算弱項」的清單,跟 core/diagnosis.py 重複。
    兩份規則改一邊漏一邊不會報錯,只會讓畫面標示與實際排序不一致。
    改由 API 直接告訴前端每一項是不是弱項。
    """

    VALUES = {
        "psy": 20.63,            # D 級且低於平均 -> 是弱項
        "wean_to_service": 7.05,  # D 級但優於平均 -> 不是弱項
        "farrowing_index": 2.42,  # B 級 -> 不是弱項
    }

    def test_grades_carry_is_weak_flag(self, app):
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        for key, grade in body["grades"].items():
            assert "isWeak" in grade, f"{key} 缺少 isWeak 欄位"

    def test_is_weak_matches_the_ranking(self, app):
        """isWeak 為 true 的項目,必須恰好等於出現在改善清單裡的項目。

        這是最重要的一條:兩者若不一致,畫面標示會跟排序自相矛盾。
        """
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        flagged = {k for k, g in body["grades"].items() if g["isWeak"]}
        ranked = {w["key"] for w in body["weaknesses"]}
        assert flagged == ranked

    def test_below_median_but_above_mean_is_not_weak(self, app):
        """離乳到第一次配種間隔 7.05 天雖為 D 級,但優於全國平均 7.38,不算弱項。"""
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        assert body["grades"]["wean_to_service"]["isWeak"] is False

    def test_good_grade_is_not_weak(self, app):
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        assert body["grades"]["farrowing_index"]["isWeak"] is False

    def test_genuinely_behind_metric_is_weak(self, app):
        _, body = _post(app, "/api/grade", {"values": self.VALUES})
        assert body["grades"]["psy"]["isWeak"] is True


class TestGradeToAdviseRoundTrip:
    """實際踩過的 bug:/api/grade 回給瀏覽器的是駝峰式鍵名(shortfallSd),
    瀏覽器原封不動把它送回 /api/advise,但 ai/prompts.py 期待底線式
    (shortfall_sd),兩邊對不上導致 KeyError,伺服器 502,前端又沒有錯誤處理,
    卡在「顧問分析中…」不動。

    之前的單元測試都是手工塞 snake_case 資料呼叫 Consultant.advise(),
    從沒真正走過「/api/grade 的輸出 -> 直接餵給 /api/advise」這條完整路徑,
    所以這個命名不一致的問題一路通過 501 個測試才在真實環境爆出來。
    """

    def test_grade_output_can_feed_advise_directly(self, app):
        """這是最貼近瀏覽器實際行為的測試:不手工構造資料,
        而是先呼叫 /api/grade,把它回傳的 weaknesses 原封不動送進 /api/advise。
        """
        example = {
            "psy": 20.63, "weaning_age": 21.97, "preweaning_mortality": 20.21,
        }
        _, grade_body = _post(app, "/api/grade", {"values": example})
        assert grade_body["weaknesses"], "前置條件:至少要有一項弱項才測得到"

        status, advise_body = _post(app, "/api/advise", {
            "weaknesses": grade_body["weaknesses"],
        })

        assert status == 200, f"應成功,實際回應:{advise_body}"
        assert "advice" in advise_body

    def test_advise_reads_camel_case_shortfall(self, app):
        """明確鎖住欄位名稱協議:/api/advise 必須看得懂 /api/grade 實際送出的
        shortfallSd(駝峰式),而不是要求呼叫端自己轉成 shortfall_sd。
        """
        status, body = _post(app, "/api/advise", {
            "weaknesses": [{
                "key": "weaning_age", "name": "平均仔豬離乳日齡", "grade": "F",
                "gradeLabel": "F 級(後 10%)", "shortfallSd": 2.96, "unit": "天",
                "improvement": "", "downstream": [], "downstreamNames": [],
            }],
        })
        assert status == 200, f"應成功,實際回應:{body}"


class TestAdviseChatEndpoint:
    """生產健檢改善建議後的追問對話框(/api/advise-chat)。

    跟 /api/consult 是同一種串流外殼(TestStreamingEvents 已經測過那份共用
    邏輯),這裡只測這個端點特有的規則:一定要先有健檢弱項、persona 不能
    跟疾病諮詢混淆、以及登入門檻與限流套用得到。
    """

    WEAKNESSES = [
        {"key": "psy", "name": "PSY", "grade": "F", "shortfallSd": 1.0,
         "improvement": "", "downstreamNames": []},
    ]

    def _events(self, app, payload, client="test"):
        return list(app.advise_events(payload, client))

    def test_rejects_without_weaknesses(self, app):
        status, body = _post(app, "/api/advise-chat", {"question": "先做哪個比較好"})
        assert status == 400
        assert "健檢" in body["error"]

    def test_rejects_empty_question(self, app):
        status, _ = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "   ",
        })
        assert status == 400

    def test_rejects_overlong_question(self, app):
        status, _ = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES,
            "question": "問" * (config.MAX_QUESTION_CHARS + 1),
        })
        assert status == 400

    def test_succeeds_with_weaknesses_and_question(self, app):
        status, body = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        })
        assert status == 200
        assert body["answer"]

    def test_uses_advice_persona_not_disease_persona(self):
        """憲法設計:追問改善建議不能被誤送成疾病諮詢的語氣。"""
        from ai.prompts import ADVICE_SYSTEM_PROMPT, DISEASE_SYSTEM_PROMPT

        transport = FakeTransport(chunks=["建議內容"])
        app = Application(transport=transport)
        _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        })
        assert transport.last_system == ADVICE_SYSTEM_PROMPT
        assert transport.last_system != DISEASE_SYSTEM_PROMPT

    def test_threads_reference_factors_into_the_prompt(self):
        transport = FakeTransport(chunks=["建議內容"])
        app = Application(transport=transport)
        _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
            "referenceFactors": [{"name": "豬舍類型", "value": "開放式豬舍"}],
        })
        assert "豬舍類型" in transport.last_prompt
        assert "開放式豬舍" in transport.last_prompt

    def test_threads_history_into_the_prompt(self):
        transport = FakeTransport(chunks=["建議內容"])
        app = Application(transport=transport)
        _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "那第二個呢",
            "history": [
                {"role": "user", "content": "先做哪個比較好"},
                {"role": "assistant", "content": "先處理離乳前死亡率"},
            ],
        })
        assert "先處理離乳前死亡率" in transport.last_prompt

    def test_error_event_carries_status(self):
        app = Application(transport=FakeTransport(error=QuotaExceeded("用盡")))
        events = self._events(app, {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        })
        error = next(e for e in events if e["type"] == "error")
        assert error["status"] == 503
        assert error["reason"] == "quota"

    def test_deltas_then_done(self, app):
        events = self._events(app, {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        })
        assert events[-1]["type"] == "done"
        assert any(e["type"] == "delta" for e in events)

    def test_blocked_without_login_when_accounts_enabled(self):
        app = _account_app()
        status, body = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        })
        assert status == 401
        assert body.get("reason") == "login_required"

    def test_works_after_guest_login(self):
        app = _account_app()
        token = _post(app, "/api/auth/guest", {})[1][SET_SESSION_KEY]
        status, body = _post_as(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "先做哪個比較好",
        }, token)
        assert status == 200
        assert body["answer"]

    def test_second_immediate_request_is_throttled(self, app):
        _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "第一問",
        })
        status, body = _post(app, "/api/advise-chat", {
            "weaknesses": self.WEAKNESSES, "question": "第二問",
        })
        assert status == 429
        assert "秒" in body["error"]


class TestExampleEndpoint:
    """demo 用的範例資料(範例牧場,已取得授權)。"""

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


class TestAuthBarVisibilityBug:
    """實際回報過的 bug:登出後右上角的使用者名稱沒有消失。

    根因是 .authbar 這個 class 設了 display: flex,跟 [hidden] 屬性
    預設的 display: none 特異度相同 —— 作者的規則會贏。用
    `bar.hidden = true` 設定隱藏屬性,DOM 上的屬性有加上去,但畫面上
    毫無效果,舊的 innerHTML(登出前的使用者名稱)留在原地。

    修法是跟其餘所有隱藏邏輯一樣改用 .is-hidden(帶 !important,才真的
    蓋得過 display: flex)。這裡鎖住修法本身,不讓它被無意中改回去。
    """

    def test_authbar_code_uses_is_hidden_not_hidden_property(self):
        js = (WEB_DIR / "app.js").read_text("utf-8")
        section = js.split("function renderAuthBar()")[1].split("\nfunction ")[0]
        # 找的是「賦值」(bar.hidden = ...),不是任何提到這個字的地方 ——
        # 函式裡的說明註解本身就會提到 bar.hidden 這個寫法(解釋不要用它)。
        assert not re.search(r"bar\.hidden\s*=", section), (
            "authBar 改回用 .hidden 屬性設定可見度 —— .authbar 的 "
            "display: flex 特異度跟 [hidden] 相同,作者規則會贏,"
            "畫面不會真的隱藏(這正是登出後使用者名稱不消失的成因)"
        )

    def test_authbar_initial_html_state_uses_is_hidden(self):
        html = (WEB_DIR / "index.html").read_text("utf-8")
        tag = re.search(r'<div[^>]*id="authBar"[^>]*>', html)
        assert tag, "找不到 #authBar"
        assert "is-hidden" in tag.group(0), (
            "#authBar 初始狀態應該用 is-hidden class,不是 hidden 屬性"
        )
        assert " hidden" not in tag.group(0), (
            "#authBar 不該用 hidden 屬性 —— .authbar 的 display:flex 會蓋掉它"
        )

    def test_authbar_css_still_conflicts_with_hidden_attribute(self):
        """如果這條測試哪天失敗(代表 .authbar 不再設固定 display 了),
        以上兩條測試就可以拿掉 —— 但那之前,前兩條測試存在的理由都還在。
        """
        css = (WEB_DIR / "style.css").read_text("utf-8")
        rule = css.split(".authbar {")[1].split("}")[0]
        assert "display:" in rule.replace(" ", "")


class TestPwaAssets:
    """manifest / service worker 的檔案沒有動態產生,不會被一般測試碰到,
    改版時很容易漏改而沒人發現(圖示改名、家目錄挪動)。這裡鎖住兩件事:
    manifest 裡引用的每個檔案都真的存在,以及 service worker 的預快取清單
    不會誤吞 /api/* —— 那會讓串流回應被快取攔截。
    """

    def test_manifest_is_valid_json_with_required_fields(self):
        manifest = json.loads((WEB_DIR / "manifest.webmanifest").read_text("utf-8"))
        assert manifest["start_url"] == "/"
        assert manifest["display"] == "standalone"
        assert len(manifest["icons"]) >= 2

    def test_manifest_icons_exist_on_disk(self):
        manifest = json.loads((WEB_DIR / "manifest.webmanifest").read_text("utf-8"))
        for icon in manifest["icons"]:
            assert (WEB_DIR / icon["src"]).is_file(), f"manifest 引用但不存在:{icon['src']}"

    def test_apple_touch_icon_exists(self):
        assert (WEB_DIR / "icons" / "apple-touch-icon.png").is_file()

    @staticmethod
    def _sw_url_list(name):
        sw = (WEB_DIR / "sw.js").read_text("utf-8")
        block = sw.split(f"{name} = [")[1].split("]")[0]
        return re.findall(r'"(/[^"]*)"', block)

    def test_service_worker_cache_lists_have_no_api_paths(self):
        """一旦 /api/* 混進快取清單,SSE 串流會被攔截而整個斷掉。"""
        for name in ("CODE_URLS", "ASSET_URLS"):
            urls = self._sw_url_list(name)
            assert urls, f"沒解析到 {name},測試本身可能失效"
            assert not any(u.startswith("/api/") for u in urls)

    def test_service_worker_cached_files_exist_on_disk(self):
        for name in ("CODE_URLS", "ASSET_URLS"):
            for url in self._sw_url_list(name):
                path = WEB_DIR / "index.html" if url == "/" else WEB_DIR / url.lstrip("/")
                assert path.is_file(), f"sw.js 快取但不存在:{url}"

    def test_all_code_files_are_network_first(self):
        """HTML/CSS/JS 必須走網路優先。

        走快取優先時真實發生過兩件事:部署後第一次載入必定是舊版;
        以及各檔案獨立更新造成「舊 HTML + 新 JS」,新 JS 找不到元素而
        讓整頁按鈕失效。這條測試確保不會有人把程式碼檔案挪回素材清單。
        """
        code_urls = set(self._sw_url_list("CODE_URLS"))
        for path in WEB_DIR.rglob("*"):
            if path.suffix not in (".js", ".css", ".html"):
                continue
            if path.name == "sw.js":      # service worker 由瀏覽器自己管理更新
                continue
            url = "/" + path.relative_to(WEB_DIR).as_posix()
            assert url in code_urls or (url == "/index.html" and "/" in code_urls), (
                f"{url} 是程式碼卻不在 CODE_URLS,會走快取優先而可能與其他檔案版本錯配"
            )

    def test_code_and_asset_lists_do_not_overlap(self):
        """同一個路徑落在兩份清單會讓行為取決於程式碼順序,不該存在。"""
        overlap = set(self._sw_url_list("CODE_URLS")) & set(self._sw_url_list("ASSET_URLS"))
        assert not overlap, f"重複出現在兩份清單:{overlap}"

    def test_index_html_links_manifest_and_service_worker_registration(self):
        html = (WEB_DIR / "index.html").read_text("utf-8")
        assert 'rel="manifest"' in html
        js = (WEB_DIR / "app.js").read_text("utf-8")
        assert "serviceWorker" in js and "register(" in js


# --- 帳號系統 ---
#
# 全部用 InMemoryStore 注入,不連真的資料庫:測試要能離線跑、幾秒跑完,
# 而且不會因為外部服務不穩就變成紅燈。真的接資料庫的驗證另外手動做。

def _account_app():
    from db import InMemoryStore
    return Application(transport=FakeTransport(chunks=["建議內容"]), store=InMemoryStore())


def _register(app, username="farmer", password="hunter2hunter2"):
    """註冊並回傳 session token,供後續請求使用。"""
    status, body = _post(app, "/api/auth/register",
                         {"username": username, "password": password})
    assert status == 200, body
    return body[SET_SESSION_KEY]


def _post_as(app, path, payload, token):
    """帶著 session 的 POST。"""
    return app.handle_post(
        path, json.dumps(payload).encode("utf-8"), client="test", token=token
    )


class TestAccountsDisabledWithoutDatabase:
    """沒設定 DATABASE_URL 時帳號功能關閉,其餘功能完全不受影響 ——
    這是這個站的核心賣點:免帳號就能用,帳號只是加值。
    """

    def test_health_reports_accounts_unavailable(self, app):
        _, body = app.handle_get("/api/health")
        assert body["accountsAvailable"] is False

    def test_auth_endpoints_report_unavailable_not_crash(self, app):
        status, body = _post(app, "/api/auth/login",
                             {"username": "farmer", "password": "hunter2hunter2"})
        assert status == 503
        assert "error" in body

    def test_consult_still_works(self, app):
        assert _post(app, "/api/consult", {"question": "小豬下痢"})[0] == 200

    def test_grade_still_works(self, app):
        assert _post(app, "/api/grade", {"values": {"psy": 20.63}})[0] == 200

    def test_me_reports_logged_out(self, app):
        status, body = app.handle_get("/api/auth/me")
        assert status == 200
        assert body["loggedIn"] is False


class TestAuthEndpoints:
    def test_health_reports_accounts_available(self):
        _, body = _account_app().handle_get("/api/health")
        assert body["accountsAvailable"] is True

    def test_register_then_me(self):
        app = _account_app()
        token = _register(app)
        status, body = app.handle_get("/api/auth/me", token)
        assert status == 200
        assert body["loggedIn"] is True
        assert body["username"] == "farmer"
        assert body["isGuest"] is False

    def test_duplicate_username_is_409(self):
        app = _account_app()
        _register(app)
        status, _ = _post(app, "/api/auth/register",
                          {"username": "farmer", "password": "another-password"})
        assert status == 409

    def test_weak_password_is_400(self):
        app = _account_app()
        status, _ = _post(app, "/api/auth/register",
                          {"username": "farmer", "password": "short"})
        assert status == 400

    def test_wrong_password_is_401(self):
        app = _account_app()
        _register(app)
        status, _ = _post(app, "/api/auth/login",
                          {"username": "farmer", "password": "wrong-password"})
        assert status == 401

    def test_login_returns_a_session(self):
        app = _account_app()
        _register(app)
        status, body = _post(app, "/api/auth/login",
                             {"username": "farmer", "password": "hunter2hunter2"})
        assert status == 200
        assert body[SET_SESSION_KEY]

    def test_logout_clears_the_session(self):
        app = _account_app()
        token = _register(app)
        status, body = _post_as(app, "/api/auth/logout", {}, token)
        assert status == 200
        assert body[CLEAR_SESSION_KEY] is True
        assert app.handle_get("/api/auth/me", token)[1]["loggedIn"] is False

    def test_invalid_token_is_treated_as_logged_out(self):
        app = _account_app()
        _, body = app.handle_get("/api/auth/me", "not-a-real-token")
        assert body["loggedIn"] is False

    def test_session_token_never_appears_in_normal_responses(self):
        """token 只能經由 HttpOnly cookie 傳遞。若混進一般回應內容,
        JavaScript 就讀得到,HttpOnly 等於白設。
        """
        app = _account_app()
        token = _register(app)
        for path in ("/api/auth/me", "/api/my-drugs", "/api/health-checks"):
            _, body = app.handle_get(path, token)
            assert SET_SESSION_KEY not in body
            assert token not in json.dumps(body, ensure_ascii=False)


class TestGuestAccounts:
    def test_guest_login_creates_a_usable_identity(self):
        app = _account_app()
        status, body = _post(app, "/api/auth/guest", {})
        assert status == 200
        assert body["isGuest"] is True
        assert body["username"] is None
        assert app.handle_get("/api/auth/me", body[SET_SESSION_KEY])[1]["loggedIn"] is True

    def test_guest_can_save_and_read_own_data(self):
        app = _account_app()
        token = _post(app, "/api/auth/guest", {})[1][SET_SESSION_KEY]

        status, _ = _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, token)
        assert status == 200
        assert len(app.handle_get("/api/health-checks", token)[1]["records"]) == 1

    def test_claim_keeps_the_data(self):
        app = _account_app()
        token = _post(app, "/api/auth/guest", {})[1][SET_SESSION_KEY]
        _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, token)

        status, body = _post_as(
            app, "/api/auth/claim",
            {"username": "farmer", "password": "hunter2hunter2"}, token,
        )
        assert status == 200
        assert body["isGuest"] is False
        assert len(app.handle_get("/api/health-checks", token)[1]["records"]) == 1

    def test_registered_account_cannot_be_reclaimed(self):
        app = _account_app()
        token = _register(app)
        status, _ = _post_as(
            app, "/api/auth/claim",
            {"username": "other", "password": "hunter2hunter2"}, token,
        )
        assert status == 409

    def test_claim_without_session_is_rejected(self):
        app = _account_app()
        status, _ = _post(app, "/api/auth/claim",
                          {"username": "farmer", "password": "hunter2hunter2"})
        assert status == 401


class TestHealthCheckHistory:
    def test_requires_login(self):
        app = _account_app()
        assert app.handle_get("/api/health-checks")[0] == 401
        assert _post(app, "/api/health-checks", {"values": {"psy": 20.63}})[0] == 401

    def test_saved_record_comes_back_with_computed_grades(self):
        app = _account_app()
        token = _register(app)
        _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, token)

        record = app.handle_get("/api/health-checks", token)[1]["records"][0]
        # 級距是讀取時即時算的,不是存起來的(單一事實來源)
        assert record["grades"]["psy"] == "D"
        assert record["values"]["psy"] == 20.63
        assert record["createdAt"]

    def test_invalid_values_are_rejected_before_saving(self):
        """壞資料一旦存進去,之後每次讀歷史都會再壞一次。"""
        app = _account_app()
        token = _register(app)
        status, _ = _post_as(app, "/api/health-checks", {"values": {"psy": "不是數字"}}, token)
        assert status == 400
        assert app.handle_get("/api/health-checks", token)[1]["records"] == []

    def test_empty_values_rejected(self):
        app = _account_app()
        token = _register(app)
        assert _post_as(app, "/api/health-checks", {"values": {}}, token)[0] == 400

    def test_newest_first(self):
        app = _account_app()
        token = _register(app)
        for psy in (20.0, 21.0, 22.0):
            _post_as(app, "/api/health-checks", {"values": {"psy": psy}}, token)

        records = app.handle_get("/api/health-checks", token)[1]["records"]
        assert [r["values"]["psy"] for r in records] == [22.0, 21.0, 20.0]

    def test_one_user_cannot_see_anothers_records(self):
        app = _account_app()
        alice = _register(app, "alice")
        bob = _register(app, "bob")
        _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, alice)

        assert app.handle_get("/api/health-checks", bob)[1]["records"] == []

    def test_one_user_cannot_delete_anothers_record(self):
        app = _account_app()
        alice = _register(app, "alice")
        bob = _register(app, "bob")
        _, created = _post_as(app, "/api/health-checks", {"values": {"psy": 20.63}}, alice)

        assert app.handle_delete(f"/api/health-checks/{created['id']}", bob)[0] == 404
        assert len(app.handle_get("/api/health-checks", alice)[1]["records"]) == 1


class TestMyDrugsServerSide:
    def test_requires_login(self):
        app = _account_app()
        assert app.handle_get("/api/my-drugs")[0] == 401
        assert _post(app, "/api/my-drugs", {"name": "阿莫西林"})[0] == 401

    def test_add_then_list(self):
        app = _account_app()
        token = _register(app)
        status, _ = _post_as(app, "/api/my-drugs", {
            "name": "阿莫西林", "dosageNote": "每公斤10mg", "withdrawalDays": 7,
        }, token)
        assert status == 200

        drugs = app.handle_get("/api/my-drugs", token)[1]["drugs"]
        assert drugs[0]["name"] == "阿莫西林"
        assert drugs[0]["withdrawalDays"] == 7

    def test_name_is_required(self):
        app = _account_app()
        token = _register(app)
        for bad in ({"name": ""}, {"name": "   "}, {"dosageNote": "沒有名字"}):
            assert _post_as(app, "/api/my-drugs", bad, token)[0] == 400

    def test_overlong_fields_are_truncated_not_rejected(self):
        app = _account_app()
        token = _register(app)
        _post_as(app, "/api/my-drugs",
                 {"name": "藥" * 200, "dosageNote": "說" * 500}, token)

        drug = app.handle_get("/api/my-drugs", token)[1]["drugs"][0]
        assert len(drug["name"]) <= config.MAX_DRUG_NAME_CHARS
        assert len(drug["dosageNote"]) <= config.MAX_DRUG_NOTE_CHARS

    def test_count_is_capped(self):
        app = _account_app()
        token = _register(app)
        for i in range(config.MAX_MY_DRUGS):
            _post_as(app, "/api/my-drugs", {"name": f"藥{i}"}, token)

        assert _post_as(app, "/api/my-drugs", {"name": "多的"}, token)[0] == 400

    def test_delete_removes_it(self):
        app = _account_app()
        token = _register(app)
        _, created = _post_as(app, "/api/my-drugs", {"name": "阿莫西林"}, token)

        assert app.handle_delete(f"/api/my-drugs/{created['id']}", token)[0] == 200
        assert app.handle_get("/api/my-drugs", token)[1]["drugs"] == []

    def test_one_user_cannot_see_anothers_drugs(self):
        app = _account_app()
        alice = _register(app, "alice")
        bob = _register(app, "bob")
        _post_as(app, "/api/my-drugs", {"name": "阿莫西林"}, alice)

        assert app.handle_get("/api/my-drugs", bob)[1]["drugs"] == []

    def test_one_user_cannot_delete_anothers_drug(self):
        app = _account_app()
        alice = _register(app, "alice")
        bob = _register(app, "bob")
        _, created = _post_as(app, "/api/my-drugs", {"name": "阿莫西林"}, alice)

        assert app.handle_delete(f"/api/my-drugs/{created['id']}", bob)[0] == 404
        assert len(app.handle_get("/api/my-drugs", alice)[1]["drugs"]) == 1

    def test_malformed_id_is_rejected(self):
        app = _account_app()
        token = _register(app)
        assert app.handle_delete("/api/my-drugs/abc", token)[0] == 400


class TestConsultUsesServerSideDrugs:
    """已登入時藥品庫一律以資料庫為準。

    信任請求裡的 myDrugs 等於任何人都能塞一組假劑量進去,而畫面上會
    顯示成「你自己藥品庫的資料」—— 正是劑量查表化要防的事。
    """

    def test_logged_in_uses_database_not_request_body(self):
        from db import InMemoryStore
        transport = FakeTransport(chunks=["ok"])
        app = Application(transport=transport, store=InMemoryStore())
        token = _register(app)
        _post_as(app, "/api/my-drugs", {"name": "資料庫裡的藥"}, token)

        _post_as(app, "/api/consult",
                 {"question": "小豬下痢", "myDrugs": [{"name": "偽造的藥"}]}, token)

        assert "資料庫裡的藥" in transport.last_prompt
        assert "偽造的藥" not in transport.last_prompt

    def test_logged_out_still_uses_request_body(self):
        """未登入使用者的藥品庫存在自己的瀏覽器,行為完全不變。"""
        transport = FakeTransport(chunks=["ok"])
        app = Application(transport=transport)
        _post(app, "/api/consult",
              {"question": "小豬下痢", "myDrugs": [{"name": "瀏覽器裡的藥"}]})
        assert "瀏覽器裡的藥" in transport.last_prompt


class TestLoginGate:
    """兩項核心功能要先登入(含訪客)才能用。

    前端會把功能畫面藏起來,但那只是介面 —— 真正的限制必須在後端,
    否則任何人直接呼叫 API 就繞過去了,而疾病諮詢每次呼叫都在花錢。
    """

    def test_consult_blocked_without_login(self):
        app = _account_app()
        status, body = _post(app, "/api/consult", {"question": "小豬下痢"})
        assert status == 401
        assert body.get("reason") == "login_required"

    def test_grade_blocked_without_login(self):
        app = _account_app()
        status, body = _post(app, "/api/grade", {"values": {"psy": 20.63}})
        assert status == 401
        assert body.get("reason") == "login_required"

    def test_advise_blocked_without_login(self):
        app = _account_app()
        status, _ = _post(app, "/api/advise", {"weaknesses": [
            {"key": "psy", "name": "PSY", "grade": "F", "shortfallSd": 1.0,
             "improvement": "", "downstreamNames": []},
        ]})
        assert status == 401

    def test_no_ai_call_happens_when_blocked(self):
        """擋下來的請求不能已經先花掉一次 API 額度。"""
        from db import InMemoryStore
        transport = FakeTransport(chunks=["不該被呼叫"])
        app = Application(transport=transport, store=InMemoryStore())
        _post(app, "/api/consult", {"question": "小豬下痢"})
        assert transport.last_prompt is None

    def test_error_arrives_as_a_stream_event_not_a_dead_connection(self):
        """串流路徑要送出 error 事件。直接斷線的話畫面會永遠卡在載入中。"""
        app = _account_app()
        events = list(app.consult_events({"question": "小豬下痢"}, "test"))
        assert events, "沒有送出任何事件,前端會一直等下去"
        assert events[0]["type"] == "error"
        assert events[0]["status"] == 401

    def test_everything_works_after_guest_login(self):
        """訪客也算登入 —— 門檻是「點一下」,不是「先註冊」。"""
        app = _account_app()
        token = _post(app, "/api/auth/guest", {})[1][SET_SESSION_KEY]

        assert _post_as(app, "/api/consult", {"question": "小豬下痢"}, token)[0] == 200
        assert _post_as(app, "/api/grade", {"values": {"psy": 20.63}}, token)[0] == 200

    def test_works_after_registering(self):
        app = _account_app()
        token = _register(app)
        assert _post_as(app, "/api/grade", {"values": {"psy": 20.63}}, token)[0] == 200

    def test_expired_or_forged_token_is_still_blocked(self):
        app = _account_app()
        assert _post_as(app, "/api/grade", {"values": {"psy": 20.63}}, "forged")[0] == 401

    def test_health_endpoint_announces_the_requirement(self):
        _, body = _account_app().handle_get("/api/health")
        assert body["loginRequired"] is True

    def test_auth_endpoints_stay_open(self):
        """登入相關的端點本身不能被門檻擋住,否則沒有人進得來。"""
        app = _account_app()
        assert app.handle_get("/api/auth/me")[0] == 200
        assert _post(app, "/api/auth/guest", {})[0] == 200
        assert app.handle_get("/api/health")[0] == 200


class TestLoginGateDisabledWithoutDatabase:
    """沒有資料庫時不得把所有人鎖在門外。

    資料庫故障或本機開發沒設 DATABASE_URL 時,網站要降級成免帳號可用,
    而不是整個不能用 —— 否則一個外部服務出問題就等於全站停擺。
    """

    def test_consult_still_open(self, app):
        assert _post(app, "/api/consult", {"question": "小豬下痢"})[0] == 200

    def test_grade_still_open(self, app):
        assert _post(app, "/api/grade", {"values": {"psy": 20.63}})[0] == 200

    def test_health_reports_no_requirement(self, app):
        _, body = app.handle_get("/api/health")
        assert body["loginRequired"] is False


class TestLoginGateCanBeTurnedOff:
    """REQUIRE_LOGIN=0 時退回「帳號是選填」的行為。"""

    def test_features_open_when_requirement_disabled(self, monkeypatch):
        monkeypatch.setattr(config, "REQUIRE_LOGIN", False)
        app = _account_app()
        assert _post(app, "/api/grade", {"values": {"psy": 20.63}})[0] == 200
        assert _post(app, "/api/consult", {"question": "小豬下痢"})[0] == 200

    def test_health_reflects_the_setting(self, monkeypatch):
        monkeypatch.setattr(config, "REQUIRE_LOGIN", False)
        _, body = _account_app().handle_get("/api/health")
        assert body["loginRequired"] is False


class TestLoginThrottle:
    """密碼可以被暴力猜,訪客建立會寫入資料庫 —— 兩者都要設限。"""

    def test_repeated_attempts_are_throttled(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_LOGIN_ATTEMPTS_PER_WINDOW", 3)
        app = _account_app()
        for _ in range(3):
            _post(app, "/api/auth/login", {"username": "farmer", "password": "guess"})

        assert _post(app, "/api/auth/login",
                     {"username": "farmer", "password": "guess"})[0] == 429

    def test_guest_creation_is_throttled_too(self, monkeypatch):
        """不設限等於開放任何人把免費方案的資料庫容量灌爆。"""
        monkeypatch.setattr(config, "MAX_LOGIN_ATTEMPTS_PER_WINDOW", 3)
        app = _account_app()
        for _ in range(3):
            _post(app, "/api/auth/guest", {})

        assert _post(app, "/api/auth/guest", {})[0] == 429

    def test_logout_is_not_throttled(self, monkeypatch):
        """登出被擋住會讓使用者卡在登入狀態出不去。"""
        monkeypatch.setattr(config, "MAX_LOGIN_ATTEMPTS_PER_WINDOW", 1)
        app = _account_app()
        token = _register(app)
        for _ in range(5):
            assert _post_as(app, "/api/auth/logout", {}, token)[0] == 200


# --- 藥品標示拍照辨識 ---

_JPEG = b"\xff\xd8\xff" + b"\x00" * 40      # 檔頭正確的最小假 JPEG
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def _image_payload(raw=_JPEG, media_type="image/jpeg"):
    import base64
    return {"image": {"mediaType": media_type, "data": base64.b64encode(raw).decode()}}


def _label_app(chunks=None, error=None):
    from db import InMemoryStore
    return Application(
        transport=FakeTransport(chunks=chunks or ['{"name": "阿莫西林", "withdrawalDays": 7}'],
                                error=error),
        store=InMemoryStore(),
    )


class TestScanEntryPoints:
    """兩個入口:相機一鍵直開,相簿另一顆。

    差別只在 capture 屬性,而那個屬性是瀏覽器在 click 當下讀的 ——
    所以是兩個各自固定的 input,不是一個用 JS 切換屬性的 input。
    這裡鎖住這個區分,避免日後有人「順手合併成一個」而讓相簿入口
    悄悄變成又開相機。
    """

    @staticmethod
    def _tag(html, element_id):
        match = re.search(r'<input[^>]*id="%s"[^>]*>' % element_id, html)
        assert match, f"找不到 #{element_id}"
        return match.group(0)

    def test_camera_input_opens_the_camera(self):
        html = (WEB_DIR / "index.html").read_text("utf-8")
        assert 'capture="environment"' in self._tag(html, "scanLabelInput")

    def test_gallery_input_does_not_force_the_camera(self):
        """有 capture 的話手機會直接開相機,使用者永遠選不到既有照片 ——
        這正是這個入口要解決的問題。
        """
        html = (WEB_DIR / "index.html").read_text("utf-8")
        assert "capture" not in self._tag(html, "pickLabelInput")

    def test_both_accept_images_only(self):
        html = (WEB_DIR / "index.html").read_text("utf-8")
        for element_id in ("scanLabelInput", "pickLabelInput"):
            assert 'accept="image/*"' in self._tag(html, element_id)

    def test_both_buttons_are_wired(self):
        js = (WEB_DIR / "app.js").read_text("utf-8")
        assert 'wireScanInput("scanLabelBtn", "scanLabelInput")' in js
        assert 'wireScanInput("pickLabelBtn", "pickLabelInput")' in js


class TestDrugLabelDoesNotWrite:
    """**這一組是整個功能最重要的測試。**

    辨識結果只能填進表單,由牧場主核對後自己按新增才入庫(憲法第三條)。
    藥品庫的內容會被 build_my_drugs_context() 當成可引用的劑量依據送進
    疾病諮詢 —— 若 AI 讀出來的數字能自動入庫,等於 AI 的輸出繞一圈變成
    「使用者提供的事實」,違反第二條的單向流動。
    """

    def test_successful_scan_stores_nothing(self):
        app = _label_app()
        token = _register(app)
        status, body = _post_as(app, "/api/drug-label", _image_payload(), token)
        assert status == 200
        assert body["draft"]["name"] == "阿莫西林"
        # 讀完之後藥品庫必須仍然是空的
        assert app.handle_get("/api/my-drugs", token)[1]["drugs"] == []

    def test_response_is_labelled_a_draft_not_a_drug(self):
        """回傳的鍵是 draft,不是 drug —— 名字本身就在說「這還沒算數」。"""
        app = _label_app()
        token = _register(app)
        body = _post_as(app, "/api/drug-label", _image_payload(), token)[1]
        assert "draft" in body
        assert "id" not in body


class TestDrugLabelEndpoint:
    def test_requires_login(self):
        """每次呼叫都在花錢,前端擋不住直接呼叫 API 的人。"""
        app = _label_app()
        assert _post(app, "/api/drug-label", _image_payload())[0] == 401

    def test_returns_all_four_fields(self):
        app = _label_app(chunks=['{"name": "藥", "activeIngredient": "Amoxicillin",'
                                 ' "dosageNote": "每公斤10mg", "withdrawalDays": 7}'])
        token = _register(app)
        draft = _post_as(app, "/api/drug-label", _image_payload(), token)[1]["draft"]
        assert draft["activeIngredient"] == "Amoxicillin"
        assert draft["withdrawalDays"] == 7

    def test_accepts_png(self):
        app = _label_app()
        token = _register(app)
        payload = _image_payload(_PNG, "image/png")
        assert _post_as(app, "/api/drug-label", payload, token)[0] == 200

    def test_unreadable_photo_reports_rather_than_inventing(self):
        """最危險的失敗模式是編一組數字出來。讀不出名稱就明講重拍。"""
        app = _label_app(chunks=["這張照片太模糊了"])
        token = _register(app)
        status, body = _post_as(app, "/api/drug-label", _image_payload(), token)
        assert status == 422
        assert body["reason"] == "unreadable"

    def test_ai_failure_degrades_cleanly(self):
        app = _label_app(error=QuotaExceeded("額度用盡"))
        token = _register(app)
        status, body = _post_as(app, "/api/drug-label", _image_payload(), token)
        assert status == 503
        assert body["reason"] == "quota"


class TestDrugLabelImageValidation:
    """圖片一樣是不可信輸入(憲法第四條)。"""

    def test_missing_image_rejected(self):
        app = _label_app()
        token = _register(app)
        assert _post_as(app, "/api/drug-label", {}, token)[0] == 400

    def test_disallowed_media_type_rejected(self):
        app = _label_app()
        token = _register(app)
        payload = _image_payload(_JPEG, "image/svg+xml")
        assert _post_as(app, "/api/drug-label", payload, token)[0] == 400

    def test_non_base64_rejected(self):
        app = _label_app()
        token = _register(app)
        payload = {"image": {"mediaType": "image/jpeg", "data": "這不是 base64!!"}}
        assert _post_as(app, "/api/drug-label", payload, token)[0] == 400

    def test_magic_bytes_must_match_declared_type(self):
        """宣告成 JPEG 卻塞別的東西進來,等於讓我們把未知內容轉手送進 AI。"""
        app = _label_app()
        token = _register(app)
        payload = _image_payload(_PNG, "image/jpeg")   # PNG 內容謊稱是 JPEG
        status, body = _post_as(app, "/api/drug-label", payload, token)
        assert status == 400
        assert "不符" in body["error"]

    def test_oversized_image_rejected(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_IMAGE_BYTES", 100)
        app = _label_app()
        token = _register(app)
        payload = _image_payload(_JPEG + b"\x00" * 500)
        assert _post_as(app, "/api/drug-label", payload, token)[0] == 400

    def test_rejected_image_costs_no_ai_call(self):
        """驗證要排在呼叫 AI 之前,否則壞圖片一樣會花掉額度。"""
        app = _label_app()
        token = _register(app)
        _post_as(app, "/api/drug-label", {}, token)
        assert app.transport.last_image is None


class TestDrugLabelThrottle:
    def test_scan_limit_is_separate_from_questions(self, monkeypatch):
        """建置藥品庫是一次性的,問診是持續性的 —— 拍完十張照片不該
        就不能問問題了。
        """
        monkeypatch.setattr(config, "MAX_LABEL_SCANS_PER_HOUR", 2)
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        app = _label_app()
        token = _register(app)
        for _ in range(2):
            assert _post_as(app, "/api/drug-label", _image_payload(), token)[0] == 200

        assert _post_as(app, "/api/drug-label", _image_payload(), token)[0] == 429
        # 拍照額度用完,問診照樣可用
        assert _post_as(app, "/api/consult", {"question": "小豬下痢"}, token)[0] == 200

    def test_questions_do_not_consume_scan_quota(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_QUESTIONS_PER_HOUR", 2)
        monkeypatch.setattr(config, "MIN_REQUEST_INTERVAL_SEC", 0)
        app = _label_app()
        token = _register(app)
        for _ in range(2):
            _post_as(app, "/api/consult", {"question": "小豬下痢"}, token)

        assert _post_as(app, "/api/drug-label", _image_payload(), token)[0] == 200
