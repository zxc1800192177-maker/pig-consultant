"""v2 的 HTTP 端點:母豬、事件、工作清單、提醒、匯入。

兩件事最重要,其餘都是常規的 CRUD 檢查:

1. **farm_id 一律從 session 取,不從請求內容拿。** 讓前端傳 farm_id 等於
   讓任何人換個號碼就看到別的牧場(憲法第十一條)。
2. **owner / worker 的邊界。** worker 只能記錄與看工作,花錢的動作
   (AI 建議)與經營層面的資訊(月報、設定、匯入)由牧場主控制。
"""

import json
from datetime import date, timedelta

import pytest

import config
from ai.transport import FakeTransport
from db import InMemoryStore
from server import SET_SESSION_KEY, Application


def _app():
    return Application(transport=FakeTransport(chunks=["建議"]), store=InMemoryStore())


def _post(app, path, payload, token=None):
    return app.handle_post(path, json.dumps(payload).encode("utf-8"),
                           client="test", token=token)


def _owner(app, username="farmer"):
    status, body = _post(app, "/api/auth/register",
                         {"username": username, "password": "hunter2hunter2"})
    assert status == 200, body
    return body[SET_SESSION_KEY]


def _worker(app, farm_id, username="worker"):
    """同一座牧場裡的員工。介面上還沒有邀請功能,測試直接建。"""
    token = _owner(app, username)
    user = app.auth.resolve_session(token)
    app.store.set_user_farm(user.id, farm_id, "worker")
    return token


def _farm_of(app, token):
    return app.auth.resolve_session(token).farm_id


@pytest.fixture
def farm():
    app = _app()
    token = _owner(app)
    return app, token, _farm_of(app, token)


class TestEveryUserGetsAFarm:
    """v2 的資料都掛在牧場底下,所以建立帳號時就要有一座。"""

    def test_register_creates_one(self):
        app = _app()
        token = _owner(app)
        assert _farm_of(app, token) is not None

    def test_guest_gets_one_too(self):
        app = _app()
        _, body = _post(app, "/api/auth/guest", {})
        user = app.auth.resolve_session(body[SET_SESSION_KEY])
        assert user.farm_id is not None

    def test_two_accounts_get_different_farms(self):
        app = _app()
        a, b = _owner(app, "alice"), _owner(app, "bob")
        assert _farm_of(app, a) != _farm_of(app, b)

    def test_role_defaults_to_owner(self):
        app = _app()
        assert app.auth.resolve_session(_owner(app)).is_owner


class TestSows:
    def test_requires_login(self):
        app = _app()
        assert app.handle_get("/api/sows")[0] == 401
        assert _post(app, "/api/sows", {"earTag": "1183"})[0] == 401

    def test_add_then_list(self, farm):
        app, token, _ = farm
        assert _post(app, "/api/sows", {"earTag": "1183", "breed": "LY"}, token)[0] == 200

        sows = app.handle_get("/api/sows", token)[1]["sows"]
        assert sows[0]["earTag"] == "1183"
        assert sows[0]["breed"] == "LY"

    def test_ear_tag_required(self, farm):
        app, token, _ = farm
        for bad in ({}, {"earTag": ""}, {"earTag": "   "}):
            assert _post(app, "/api/sows", bad, token)[0] == 400

    def test_duplicate_active_tag_rejected(self, farm):
        app, token, _ = farm
        _post(app, "/api/sows", {"earTag": "1183"}, token)
        assert _post(app, "/api/sows", {"earTag": "1183"}, token)[0] == 409

    def test_detail_includes_events(self, farm):
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "MT", "date": "2026-02-03"}, token)

        body = app.handle_get(f"/api/sows/{sow_id}", token)[1]
        assert body["sow"]["earTag"] == "1183"
        assert body["events"][0]["type"] == "MT"

    def test_bad_id_is_rejected(self, farm):
        app, token, _ = farm
        assert app.handle_get("/api/sows/abc", token)[0] == 400


class TestFarmIsolationOverHttp:
    """資料層已經測過隔離,這裡測的是 HTTP 層真的有把 farm_id 帶下去。"""

    def test_cannot_see_another_farms_sows(self):
        app = _app()
        alice, bob = _owner(app, "alice"), _owner(app, "bob")
        _post(app, "/api/sows", {"earTag": "1183"}, alice)
        assert app.handle_get("/api/sows", bob)[1]["sows"] == []

    def test_cannot_open_another_farms_sow(self):
        app = _app()
        alice, bob = _owner(app, "alice"), _owner(app, "bob")
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, alice)[1]["id"]
        assert app.handle_get(f"/api/sows/{sow_id}", bob)[0] == 404

    def test_cannot_record_onto_another_farms_sow(self):
        app = _app()
        alice, bob = _owner(app, "alice"), _owner(app, "bob")
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, alice)[1]["id"]
        status, _ = _post(app, "/api/sow-events",
                          {"sowId": sow_id, "type": "MT", "date": "2026-02-03"}, bob)
        assert status == 404

    def test_farm_id_in_the_request_body_is_ignored(self):
        """前端送 farmId 不該有任何作用 —— 那是最直接的越權手法。"""
        app = _app()
        alice, bob = _owner(app, "alice"), _owner(app, "bob")
        alice_farm = _farm_of(app, alice)

        _post(app, "/api/sows", {"earTag": "1183", "farmId": alice_farm}, bob)
        assert app.handle_get("/api/sows", alice)[1]["sows"] == []


class TestSameFarmSharing:
    """牧場主與員工共用同一批資料 —— 這是 v2 改架構的理由。"""

    def test_worker_records_owner_sees(self, farm):
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)

        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, owner)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "WN", "date": "2026-02-26",
               "detail": {"weaned": 10}}, worker)

        events = app.handle_get(f"/api/sows/{sow_id}", owner)[1]["events"]
        assert events[0]["detail"]["weaned"] == 10

    def test_event_records_who_entered_it(self, farm):
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        worker_id = app.auth.resolve_session(worker).id

        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, owner)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "MT", "date": "2026-02-03"}, worker)

        events = app.handle_get(f"/api/sows/{sow_id}", owner)[1]["events"]
        assert events[0]["recordedBy"] == worker_id


class TestOwnerOnly:
    """花錢的動作與經營層面的資訊由牧場主控制(使用者決定)。"""

    def test_worker_cannot_import(self, farm):
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        status, body = _post(app, "/api/import/preview",
                             {"content": "1183|MT|20260203"}, worker)
        assert status == 403
        assert body["reason"] == "owner_only"

    def test_worker_cannot_add_pens(self, farm):
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        assert _post(app, "/api/pens", {"name": "A-01"}, worker)[0] == 403

    def test_worker_can_still_see_pens(self, farm):
        """看得到是必要的 —— 移入產房時要知道有哪些欄位。"""
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        _post(app, "/api/pens", {"name": "A-01"}, owner)
        assert len(app.handle_get("/api/pens", worker)[1]["pens"]) == 1

    def test_worker_can_see_tasks(self, farm):
        """員工要知道今天該做什麼,否則這個系統對他沒用。"""
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        assert app.handle_get("/api/tasks", worker)[0] == 200

    def test_owner_can_import(self, farm):
        app, owner, _ = farm
        assert _post(app, "/api/import/preview",
                     {"content": "1183|MT|20260203"}, owner)[0] == 200


class TestWorkerCanFixOwnMistake:
    """手誤很常見。完全不能改的話,實務上會變成「先不記、等老闆來」——
    反而遺失資料(憲法第十一條第 5 款)。
    """

    @pytest.fixture
    def setup(self, farm):
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, owner)[1]["id"]
        return app, owner, worker, sow_id

    def test_worker_can_delete_own_latest(self, setup):
        app, owner, worker, sow_id = setup
        ev = _post(app, "/api/sow-events",
                   {"sowId": sow_id, "type": "MT", "date": "2026-02-03"}, worker)[1]["id"]
        assert app.handle_delete(f"/api/sow-events/{ev}", worker)[0] == 200

    def test_worker_cannot_delete_someone_elses(self, setup):
        app, owner, worker, sow_id = setup
        ev = _post(app, "/api/sow-events",
                   {"sowId": sow_id, "type": "MT", "date": "2026-02-03"}, owner)[1]["id"]
        assert app.handle_delete(f"/api/sow-events/{ev}", worker)[0] == 403

    def test_worker_cannot_delete_an_older_record(self, setup):
        app, owner, worker, sow_id = setup
        old = _post(app, "/api/sow-events",
                    {"sowId": sow_id, "type": "MT", "date": "2026-02-03"}, worker)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "PD", "date": "2026-03-01"}, worker)
        assert app.handle_delete(f"/api/sow-events/{old}", worker)[0] == 403

    def test_owner_can_delete_anything(self, setup):
        app, owner, worker, sow_id = setup
        old = _post(app, "/api/sow-events",
                    {"sowId": sow_id, "type": "MT", "date": "2026-02-03"}, worker)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "PD", "date": "2026-03-01"}, worker)
        assert app.handle_delete(f"/api/sow-events/{old}", owner)[0] == 200


class TestEventSideEffects:
    """記錄即完成,而且連帶效果要真的發生 —— 否則使用者得自己去改狀態。"""

    @pytest.fixture
    def sow(self, farm):
        app, token, farm_id = farm
        sow_id = _post(app, "/api/sows", {"earTag": "2580"}, token)[1]["id"]
        return app, token, farm_id, sow_id

    def test_farrowing_increments_parity(self, sow):
        app, token, _, sow_id = sow
        body = _post(app, "/api/sow-events",
                     {"sowId": sow_id, "type": "FW", "date": "2026-02-04",
                      "detail": {"born_alive": 10}}, token)[1]
        assert body["sow"]["parity"] == 1

    def test_weaning_frees_the_pen(self, sow):
        app, token, farm_id, sow_id = sow
        pen = _post(app, "/api/pens", {"name": "A-03"}, token)[1]["id"]
        app.store.update_sow(farm_id, sow_id, pen_id=pen)

        body = _post(app, "/api/sow-events",
                     {"sowId": sow_id, "type": "WN", "date": "2026-02-26"}, token)[1]
        assert body["sow"]["penId"] is None

    def test_culling_appends_the_roc_year(self, sow):
        """離群時耳號加民國年後綴,裸號釋放給新豬(牧場既有慣例)。"""
        app, token, _, sow_id = sow
        body = _post(app, "/api/sow-events",
                     {"sowId": sow_id, "type": "SAL", "date": "2026-07-01",
                      "detail": {"reason": "年齡太大"}}, token)[1]
        assert body["sow"]["earTag"] == "2580-D115"
        assert body["sow"]["status"] == "culled"

    def test_suffix_uses_the_event_year_not_today(self, sow):
        """補登去年的淘汰要標去年的年份 —— 用今天會標錯。"""
        app, token, _, sow_id = sow
        body = _post(app, "/api/sow-events",
                     {"sowId": sow_id, "type": "SAL", "date": "2024-12-20"}, token)[1]
        assert body["sow"]["earTag"] == "2580-D113"

    def test_bare_tag_is_free_again_after_culling(self, sow):
        app, token, _, sow_id = sow
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "SAL", "date": "2026-07-01"}, token)
        assert _post(app, "/api/sows", {"earTag": "2580"}, token)[0] == 200


class TestEventValidation:
    def test_unknown_type_rejected(self, farm):
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        assert _post(app, "/api/sow-events",
                     {"sowId": sow_id, "type": "ZZZ", "date": "2026-02-03"}, token)[0] == 400

    def test_bad_date_rejected(self, farm):
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        assert _post(app, "/api/sow-events",
                     {"sowId": sow_id, "type": "MT", "date": "昨天"}, token)[0] == 400

    def test_missing_sow_rejected(self, farm):
        app, token, _ = farm
        assert _post(app, "/api/sow-events",
                     {"type": "MT", "date": "2026-02-03"}, token)[0] == 400


class TestTasksAndAlerts:
    def test_tasks_group_by_kind(self, farm):
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        farrowed = date.today() - timedelta(days=22)
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "FW", "date": farrowed.isoformat()}, token)

        body = app.handle_get("/api/tasks", token)[1]
        kinds = {g["kind"] for g in body["groups"]}
        assert "wean" in kinds

    def test_tasks_carry_a_chinese_label(self, farm):
        """畫面上的文字只該有一份定義(core/labels.py)。"""
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        farrowed = date.today() - timedelta(days=22)
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "FW", "date": farrowed.isoformat()}, token)

        group = app.handle_get("/api/tasks", token)[1]["groups"][0]
        assert group["label"] == "離乳"

    def test_week_can_be_chosen(self, farm):
        app, token, _ = farm
        body = app.handle_get("/api/tasks?start=2026-08-10", token)[1]
        assert body["weekStart"] == "2026-08-10"
        assert body["weekEnd"] == "2026-08-16"

    def test_alerts_report_pen_pressure(self, farm):
        app, token, _ = farm
        _post(app, "/api/pens", {"name": "A-01"}, token)
        body = app.handle_get("/api/alerts", token)[1]
        assert body["pens"]["total"] == 1
        assert body["pens"]["free"][0]["name"] == "A-01"


class TestImport:
    ROWS = "\n".join([
        "1183|GA|20230519|LY",
        "1183|MT|20260203|D6",
        "1585|FW|20251015|56|0|0",      # 離群值:單窩 56 隻
    ])

    def test_preview_does_not_write(self, farm):
        app, token, _ = farm
        body = _post(app, "/api/import/preview", {"content": self.ROWS}, token)[1]
        assert body["sows"] == 2
        assert app.handle_get("/api/sows", token)[1]["sows"] == [], "預覽不該寫入"

    def test_preview_reports_anomalies(self, farm):
        app, token, _ = farm
        body = _post(app, "/api/import/preview", {"content": self.ROWS}, token)[1]
        assert len(body["anomalies"]) == 1
        assert "56" in body["anomalies"][0]["reason"]

    def test_commit_writes(self, farm):
        app, token, _ = farm
        stats = _post(app, "/api/import", {"content": self.ROWS}, token)[1]
        assert stats["sows"] == 2
        assert len(app.handle_get("/api/sows?all=1", token)[1]["sows"]) == 2

    def test_excluded_lines_are_flagged_not_dropped(self, farm):
        app, token, _ = farm
        preview = _post(app, "/api/import/preview", {"content": self.ROWS}, token)[1]
        bad_line = preview["anomalies"][0]["line"]

        _post(app, "/api/import",
              {"content": self.ROWS, "excludeLines": [bad_line]}, token)

        sows = app.handle_get("/api/sows?all=1", token)[1]["sows"]
        sow_id = next(s["id"] for s in sows if s["earTag"] == "1585")
        events = app.handle_get(f"/api/sows/{sow_id}", token)[1]["events"]
        assert events[0]["excluded"] is True, "排除不等於刪除"

    def test_empty_content_rejected(self, farm):
        app, token, _ = farm
        assert _post(app, "/api/import", {}, token)[0] == 400

    def test_import_goes_to_the_right_farm(self):
        app = _app()
        alice, bob = _owner(app, "alice"), _owner(app, "bob")
        _post(app, "/api/import", {"content": self.ROWS}, alice)
        assert app.handle_get("/api/sows?all=1", bob)[1]["sows"] == []


class TestRequestLimits:
    def test_import_allows_bigger_bodies(self):
        """32,814 筆的實際檔案是 1.45 MB,一般端點的 64KB 上限擋不住。"""
        from server import too_large
        assert too_large(2_000_000, "/api/import") is False
        assert too_large(2_000_000, "/api/sows") is True

    def test_import_still_has_a_ceiling(self):
        from server import too_large
        assert too_large(config.MAX_IMPORT_BYTES + 1, "/api/import") is True
