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
import schedule
from ai.transport import FakeTransport
from db import InMemoryStore
from server import SET_SESSION_KEY, Application, to_json_bytes


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

    def test_sire_and_dam_tags_are_stored_when_manually_added(self, farm):
        """匯入時已經會存父母耳號(見 test_importer.py),但手動用紀錄頁
        的「種豬進場」新增時,這條路徑一直沒有測試證明真的存得進去。
        """
        app, token, _ = farm
        _post(app, "/api/sows",
              {"earTag": "1183", "sireTag": "D1", "damTag": "2416"}, token)
        sows = app.handle_get("/api/sows", token)[1]["sows"]
        assert sows[0]["sireTag"] == "D1"
        assert sows[0]["damTag"] == "2416"

    def test_sire_and_dam_tags_are_optional(self, farm):
        app, token, _ = farm
        assert _post(app, "/api/sows", {"earTag": "1183"}, token)[0] == 200
        sows = app.handle_get("/api/sows", token)[1]["sows"]
        assert sows[0]["sireTag"] == ""
        assert sows[0]["damTag"] == ""

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
        """產房容量來自設定裡的總數,不再是 pens 資料表的列數。"""
        app, token, _ = farm
        _post(app, "/api/settings", {"settings": {"farrowing_pens": 12}}, token)
        body = app.handle_get("/api/alerts", token)[1]
        assert body["pens"]["configured"] is True
        assert body["pens"]["total"] == 12
        assert body["pens"]["free"] == 12

    def test_alerts_say_pens_are_unconfigured_by_default(self, farm):
        """沒設定過就不宣稱空間夠或不夠。"""
        app, token, _ = farm
        body = app.handle_get("/api/alerts", token)[1]
        assert body["pens"]["configured"] is False
        assert body["pens"]["short_by"] == 0


class TestResponsesAreSerializable:
    """回應要真的送得出去。

    這裡刻意繞一圈用 to_json_bytes,而不是只檢查回傳的 dict —— 本檔其他
    測試都停在 dict 就斷言了,序列化那一步從來沒人測到。實際的後果是
    `/api/sows` 與 `/api/alerts` 全掛:資料庫回來的 entry_date 是 date 物件,
    json 不認得,序列化拋 TypeError 讓連線直接斷開。瀏覽器那端只看得到
    「Failed to fetch」,伺服器記錄裡才有真正的原因。

    Application 與 HTTP 傳輸分離讓測試好寫,代價就是中間這道縫 ——
    這個類別就是把縫補起來。
    """

    # 鋪資料有兩個講究,少一個測試就抓不到問題。
    #
    # 一、**要經過匯入**,不能用 /api/sows 建。兩條寫入路徑存進去的型別
    #    不一樣:匯入寫的是 date 物件,POST 寫的是從 JSON 來的字串。
    # 二、**要真的養出一頭空胎母豬**。出問題的欄位是 alerts 的
    #    openSows[].since,沒有母豬逾期未配種時那份清單是空的,再怎麼測
    #    都是綠的。
    #
    # 這兩點都是踩過才知道的:第一版兩點都沒做到,測試全過,瀏覽器照樣
    # Failed to fetch。正式環境的 PostgresStore 一律回 date,不是只有匯入。

    @staticmethod
    def _rows():
        """離乳後久未配種的母豬 —— 正是 openSows 那份清單的來源。"""
        overdue = schedule.DEFAULTS["open_sow_alert_days"] + 30
        weaned = date.today() - timedelta(days=overdue)
        farrowed = weaned - timedelta(days=22)
        mated = farrowed - timedelta(days=114)
        fmt = "%Y%m%d"
        return "\n".join([
            "1183|GA|20230519|LY",
            f"1183|MT|{mated.strftime(fmt)}|D6",
            f"1183|FW|{farrowed.strftime(fmt)}|12|1|0",
            f"1183|WN|{weaned.strftime(fmt)}|11||0",
            # 之後沒有任何配種紀錄 —— 所以她一直掛在空胎名單上
        ])

    @staticmethod
    def _seeded():
        app = Application(transport=FakeTransport(chunks=["建議"]), store=InMemoryStore())
        token = _owner(app)
        _post(app, "/api/pens", {"name": "A-01"}, token)
        stats = _post(app, "/api/import",
                      {"content": TestResponsesAreSerializable._rows()}, token)[1]
        assert stats["sows"] == 1, stats
        sow_id = app.handle_get("/api/sows?all=1", token)[1]["sows"][0]["id"]
        return app, token, sow_id

    def test_seeded_data_really_reaches_the_broken_field(self):
        """先確認這組資料真的踩得到,否則下面全是空轉的綠燈。"""
        app, token, _ = self._seeded()
        body = app.handle_get("/api/alerts", token)[1]
        assert body["openSows"], "沒養出空胎母豬,這組測試抓不到序列化問題"
        assert isinstance(body["openSows"][0]["since"], date), (
            "since 已經不是 date 了 —— 若是刻意改成字串,請一併更新這個測試"
        )

    @pytest.mark.parametrize("path", [
        "/api/sows", "/api/sows?all=1", "/api/alerts", "/api/tasks",
        "/api/auth/me", "/api/health",
    ])
    def test_get_responses_survive_serialization(self, path):
        app, token, _ = self._seeded()
        status, body = app.handle_get(path, token)
        assert status == 200, body
        to_json_bytes(body)      # 不拋例外即為通過

    def test_sow_card_survives_serialization(self):
        app, token, sow_id = self._seeded()
        status, body = app.handle_get(f"/api/sows/{sow_id}", token)
        assert status == 200, body
        to_json_bytes(body)

    def test_dates_come_out_as_iso_strings(self):
        """轉出來的格式要是前端看得懂的 ISO 字串,不是 date 的 repr。"""
        payload = json.loads(to_json_bytes({"d": date(2026, 8, 13)}))
        assert payload["d"] == "2026-08-13"

    def test_unknown_types_still_raise(self):
        """只放行日期。什麼都靜靜轉成字串的話,真的組錯回應時不會有人發現。"""
        with pytest.raises(TypeError):
            to_json_bytes({"x": object()})


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


class TestSettings:
    """牧場設定。只有牧場主能改 —— 這些參數會改變全場的工作清單。"""

    def test_defaults_come_back_before_anything_is_saved(self, farm):
        app, token, _ = farm
        body = app.handle_get("/api/settings", token)[1]
        assert body["settings"]["gestation_days"] == schedule.DEFAULTS["gestation_days"]
        assert body["custom"] == []

    def test_saved_value_is_used_by_the_task_list(self, farm):
        """設定要真的影響推算,不是存起來好看的。"""
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        farrowed = date.today() - timedelta(days=15)
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "FW", "date": farrowed.isoformat()}, token)

        # 預設泌乳 22 天 → 這週還不用離乳
        kinds = {g["kind"] for g in app.handle_get("/api/tasks", token)[1]["groups"]}
        assert "wean" not in kinds

        _post(app, "/api/settings", {"settings": {"lactation_days": 15}}, token)
        kinds = {g["kind"] for g in app.handle_get("/api/tasks", token)[1]["groups"]}
        assert "wean" in kinds, "改了泌乳天數,離乳工作卻沒跟著提前"

    def test_only_non_default_values_are_stored(self, farm):
        """整份存下來的話,日後調整預設值不會生效在任何既有牧場。"""
        app, token, farm_id = farm
        _post(app, "/api/settings", {"settings": dict(schedule.DEFAULTS)}, token)
        assert app.store.get_farm_settings(farm_id) == {}

    def test_changed_values_are_marked_as_custom(self, farm):
        app, token, _ = farm
        body = _post(app, "/api/settings", {"settings": {"lactation_days": 25}}, token)[1]
        assert body["custom"] == ["lactation_days"]

    def test_out_of_range_is_rejected_not_clamped(self, farm):
        """夾到邊界的話,使用者填 999 卻被存成 130,畫面顯示 130 而他
        以為是 999 —— 那比報錯還糟。
        """
        app, token, farm_id = farm
        status, body = _post(app, "/api/settings",
                             {"settings": {"gestation_days": 999}}, token)
        assert status == 400
        assert app.store.get_farm_settings(farm_id) == {}

    def test_zero_gestation_is_rejected(self, farm):
        """0 天懷孕會讓整個工作清單瞬間爆量。"""
        app, token, _ = farm
        assert _post(app, "/api/settings",
                     {"settings": {"gestation_days": 0}}, token)[0] == 400

    def test_unknown_keys_are_ignored_not_stored(self, farm):
        app, token, farm_id = farm
        status, _ = _post(app, "/api/settings",
                          {"settings": {"lactation_days": 25, "whatever": 1}}, token)
        assert status == 200
        assert app.store.get_farm_settings(farm_id) == {"lactation_days": 25}

    def test_non_integer_is_rejected(self, farm):
        app, token, _ = farm
        for bad in ("22", 22.5, [22]):
            assert _post(app, "/api/settings",
                         {"settings": {"lactation_days": bad}}, token)[0] == 400, bad

    def test_true_is_not_accepted_as_one(self, farm):
        """bool 是 int 的子類別,不擋掉的話 True 會被當成 1 存進去。"""
        app, token, _ = farm
        assert _post(app, "/api/settings",
                     {"settings": {"review_min_litters": True}}, token)[0] == 400

    def test_worker_cannot_read_or_change_settings(self, farm):
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        assert app.handle_get("/api/settings", worker)[0] == 403
        assert _post(app, "/api/settings",
                     {"settings": {"lactation_days": 25}}, worker)[0] == 403

    def test_settings_do_not_leak_across_farms(self, farm):
        app, a_token, _ = farm
        b_token = _owner(app, "otherfarm")
        _post(app, "/api/settings", {"settings": {"lactation_days": 25}}, a_token)
        body = app.handle_get("/api/settings", b_token)[1]
        assert body["settings"]["lactation_days"] == schedule.DEFAULTS["lactation_days"]

    def test_field_list_carries_labels_and_ranges(self, farm):
        """前端不自己維護一份文字與範圍,否則兩邊會各說各話。"""
        app, token, _ = farm
        fields = {f["key"]: f
                  for f in app.handle_get("/api/settings", token)[1]["fields"]}
        assert fields["gestation_days"]["label"] == "懷孕天數"
        assert fields["gestation_days"]["min"] < fields["gestation_days"]["max"]
        assert fields["gestation_days"]["unit"] == "天"


class TestWorthReviewEndpoint:
    def test_lists_sows_with_reasons(self, farm):
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        for i, alive in enumerate([14, 12, 10]):
            _post(app, "/api/sow-events",
                  {"sowId": sow_id, "type": "FW",
                   "date": (date(2023, 1, 1) + timedelta(days=145 * i)).isoformat(),
                   "detail": {"born_alive": alive}}, token)

        body = app.handle_get("/api/review", token)[1]
        assert [s["earTag"] for s in body["sows"]] == ["1183"]
        assert body["sows"][0]["reasons"][0]["label"] == "產仔數連續下滑"

    def test_caveat_always_travels_with_the_list(self, farm):
        """名單與但書不可分開送 —— 前端漏畫但書就變成淘汰建議了。"""
        app, token, _ = farm
        assert app.handle_get("/api/review", token)[1]["caveat"]

    def test_worker_cannot_see_it(self, farm):
        app, _, farm_id = farm
        assert app.handle_get("/api/review", _worker(app, farm_id))[0] == 403


class TestRecordPage:
    """紀錄頁需要的東西:公豬清單、最近記錄、離乳評分。"""

    def test_boar_can_be_added_and_listed(self, farm):
        app, token, _ = farm
        assert _post(app, "/api/boars",
                     {"earTag": "D6", "breed": "Duroc"}, token)[0] == 200
        boars = app.handle_get("/api/boars", token)[1]["boars"]
        assert [b["earTag"] for b in boars] == ["D6"]

    def test_boar_sire_and_dam_tags_are_stored_and_listed(self, farm):
        app, token, _ = farm
        _post(app, "/api/boars",
              {"earTag": "D6", "sireTag": "D1", "damTag": "2416"}, token)
        boars = app.handle_get("/api/boars", token)[1]["boars"]
        assert boars[0]["sireTag"] == "D1"
        assert boars[0]["damTag"] == "2416"

    def test_boar_parent_tags_are_optional(self, farm):
        app, token, _ = farm
        assert _post(app, "/api/boars", {"earTag": "D6"}, token)[0] == 200
        boars = app.handle_get("/api/boars", token)[1]["boars"]
        assert boars[0]["sireTag"] == ""
        assert boars[0]["damTag"] == ""

    def test_duplicate_boar_tag_is_rejected(self, farm):
        app, token, _ = farm
        _post(app, "/api/boars", {"earTag": "D6"}, token)
        assert _post(app, "/api/boars", {"earTag": "D6"}, token)[0] == 409

    def test_worker_can_add_a_boar(self, farm):
        """種豬進場是記錄動作,員工做得到(憲法第十一條第 5 款)。"""
        app, _, farm_id = farm
        assert _post(app, "/api/boars",
                     {"earTag": "D7"}, _worker(app, farm_id))[0] == 200

    def test_boars_do_not_leak_across_farms(self, farm):
        app, a_token, _ = farm
        b_token = _owner(app, "otherfarm")
        _post(app, "/api/boars", {"earTag": "D6"}, a_token)
        assert app.handle_get("/api/boars", b_token)[1]["boars"] == []

    def test_recent_events_carry_the_ear_tag(self, farm):
        """清單上要看得到是哪一頭,不能只有一個 sowId。"""
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "MT", "date": date.today().isoformat()}, token)
        events = app.handle_get("/api/recent-events", token)[1]["events"]
        assert events[0]["earTag"] == "1183"

    def test_owner_can_undo_anything(self, farm):
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        for day in (2, 1, 0):
            _post(app, "/api/sow-events",
                  {"sowId": sow_id, "type": "MT",
                   "date": (date.today() - timedelta(days=day)).isoformat()}, token)
        events = app.handle_get("/api/recent-events?days=7", token)[1]["events"]
        assert all(e["canUndo"] for e in events)

    def test_worker_can_only_undo_own_latest(self, farm):
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, owner)[1]["id"]

        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "MT",
               "date": (date.today() - timedelta(days=1)).isoformat()}, owner)
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "MT", "date": date.today().isoformat()}, worker)

        events = app.handle_get("/api/recent-events?days=7", worker)[1]["events"]
        undoable = [e for e in events if e["canUndo"]]
        assert len(undoable) == 1

    def test_can_undo_matches_what_delete_actually_allows(self, farm):
        """畫面上畫得出按鈕,按下去就必須成功 —— 兩邊判斷不一致的話,
        使用者會看到一個按了必定失敗的按鈕。
        """
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, owner)[1]["id"]
        for day, who in ((2, owner), (1, worker), (0, worker)):
            _post(app, "/api/sow-events",
                  {"sowId": sow_id, "type": "MT",
                   "date": (date.today() - timedelta(days=day)).isoformat()}, who)

        events = app.handle_get("/api/recent-events?days=7", worker)[1]["events"]
        for e in events:
            status, _ = app.handle_delete("/api/sow-events/" + str(e["id"]), worker)
            assert (status == 200) == e["canUndo"], (
                "canUndo=" + str(e["canUndo"]) + " 但實際刪除回 " + str(status))
            if status == 200:
                break      # 刪掉之後「最新一筆」就換人了,不能繼續比

    def test_wean_score_is_stored(self, farm):
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "WN", "date": date.today().isoformat(),
               "detail": {"weaned": 11, "wean_score": 4}}, token)
        detail = app.handle_get("/api/sows/" + str(sow_id), token)[1]["events"][0]["detail"]
        assert detail["wean_score"] == 4

    def test_wean_score_out_of_range_is_rejected(self, farm):
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        for bad in (0, 6, -1, "5", True):
            status, _ = _post(app, "/api/sow-events",
                              {"sowId": sow_id, "type": "WN",
                               "date": date.today().isoformat(),
                               "detail": {"wean_score": bad}}, token)
            assert status == 400, bad

    def test_missing_wean_score_is_left_empty_not_filled_in(self, farm):
        """未評分不補值 —— 補一個中間值會讓「沒人看過」與「看過覺得普通」
        變成同一件事(憲法第三條第 6 款)。
        """
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "WN", "date": date.today().isoformat(),
               "detail": {"weaned": 11, "wean_score": None}}, token)
        detail = app.handle_get("/api/sows/" + str(sow_id), token)[1]["events"][0]["detail"]
        assert "wean_score" not in detail


class TestBoarsAreImported:
    """匯入要把公豬建起來。

    踩過的實情:預覽畫面報「公豬 154 頭、275 筆事件」,確認匯入後
    /api/boars 一頭都沒有 —— import_into 從來沒碰過 boar_rows。
    後果是匯入完資料的牧場打開配種表單,公豬選單是空的。
    """

    ROWS = "\n".join([
        "1183|GA|20230519|LY",
        "1183|MT|20260203|D6",
        "D6|BA|20200301",           # 公豬自己的事件:採精
        "D6|SC|20200302",
        "D7|BA|20210715",
    ])

    def test_boars_exist_after_import(self, farm):
        app, token, _ = farm
        _post(app, "/api/import", {"content": self.ROWS}, token)
        tags = {b["earTag"] for b in app.handle_get("/api/boars", token)[1]["boars"]}
        assert tags == {"D6", "D7"}

    def test_boars_are_not_created_as_sows(self, farm):
        """公豬與母豬是不同的實體,混在一起母豬清單會多出一堆公豬。"""
        app, token, _ = farm
        _post(app, "/api/import", {"content": self.ROWS}, token)
        tags = {s["earTag"] for s in app.handle_get("/api/sows?all=1", token)[1]["sows"]}
        assert "D6" not in tags and "D7" not in tags

    def test_entry_date_is_the_earliest_event_not_today(self, farm):
        """檔案沒有公豬的進場記錄。用今天當進場日的話,2020 年就在的
        公豬會看起來是今天剛到的。
        """
        app, token, farm_id = farm
        _post(app, "/api/import", {"content": self.ROWS}, token)
        d6 = app.store.find_boar_by_tag(farm_id, "D6")
        assert d6["entry_date"] == date(2020, 3, 1)

    def test_reimport_does_not_duplicate_boars(self, farm):
        """匯入必須冪等,公豬也一樣。"""
        app, token, _ = farm
        _post(app, "/api/import", {"content": self.ROWS}, token)
        stats = _post(app, "/api/import", {"content": self.ROWS}, token)[1]
        assert stats["boars"] == 0
        assert len(app.handle_get("/api/boars", token)[1]["boars"]) == 2

    def test_commit_reports_how_many_boars_were_added(self, farm):
        app, token, _ = farm
        assert _post(app, "/api/import", {"content": self.ROWS}, token)[1]["boars"] == 2

    def test_preview_admits_boar_events_are_not_written(self, farm):
        """報一個「275 筆」然後什麼都不寫,使用者會以為資料已經進去了。"""
        app, token, _ = farm
        body = _post(app, "/api/import/preview", {"content": self.ROWS}, token)[1]
        assert body["boarEvents"] == 3
        assert body["boarEventsImported"] is False


class TestExitedSowsStayVisibleAndCountInAnalysis:
    """使用者實際反映的問題:記成死亡或淘汰後,那頭母豬從母豬資訊跟
    分析裡都消失了。這裡驗證伺服器層真的把 server.py 的兩處查詢
    (_sow_detail、_review)接到含離群母豬的清單,不是只有 schedule.py
    的純函式邏輯是對的。
    """

    def test_exited_sow_disappears_from_default_list(self, farm):
        """預設(不帶 all=1)的列表本來就只該有在場的 —— 這是既有行為,
        不是這次要改的部分,順便釘住避免以後不小心跟下面那條混在一起改。
        """
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "DTH", "date": "2026-07-01"}, token)
        assert app.handle_get("/api/sows", token)[1]["sows"] == []

    def test_exited_sow_is_still_reachable_with_all_1(self, farm):
        """但 ?all=1 要找得到她,而且耳號已經帶上民國年後綴。"""
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "DTH", "date": "2026-07-01"}, token)
        rows = app.handle_get("/api/sows?all=1", token)[1]["sows"]
        assert rows[0]["earTag"] == "1183-D115"
        assert rows[0]["status"] == "dead"

    def test_exited_sow_still_has_a_detail_card(self, farm):
        """卡片本身要開得起來,不能因為離群就 404。"""
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        _post(app, "/api/sow-events",
              {"sowId": sow_id, "type": "DTH", "date": "2026-07-01"}, token)
        status, body = app.handle_get(f"/api/sows/{sow_id}", token)
        assert status == 200
        assert body["sow"]["status"] == "dead"

    def test_exited_peer_shifts_an_active_sows_tier(self, farm):
        """伺服器層驗證:_sow_detail 傳給 performance_with_tiers 的清單
        真的含離群母豬,不是只在 schedule.py 的單元測試裡對。
        """
        app, token, _ = farm
        subject_id = _post(app, "/api/sows", {"earTag": "0001"}, token)[1]["id"]
        peer_ids = [_post(app, "/api/sows", {"earTag": f"{i:04d}"}, token)[1]["id"]
                    for i in range(2, 11)]

        def farrow(sid, values):
            for i, n in enumerate(values):
                _post(app, "/api/sow-events",
                     {"sowId": sid, "type": "FW",
                      "date": (date(2023, 1, 1) + timedelta(days=145 * i)).isoformat(),
                      "detail": {"born_alive": n}}, token)

        farrow(subject_id, [10, 10, 10])
        for pid, n in zip(peer_ids, range(11, 20)):
            farrow(pid, [n, n, n])

        without = app.handle_get(f"/api/sows/{subject_id}", token)[1]
        tier_without = next(m["tier"] for m in without["performance"]["metrics"]
                            if m["key"] == "born_alive")
        assert tier_without == "poor"

        # 5 頭表現更差的離群母豬加進來
        poor_ids = [_post(app, "/api/sows", {"earTag": f"9{i:03d}"}, token)[1]["id"]
                    for i in range(5)]
        for pid, n in zip(poor_ids, range(1, 6)):
            farrow(pid, [n, n, n])
            _post(app, "/api/sow-events",
                 {"sowId": pid, "type": "SAL", "date": "2026-07-01"}, token)

        with_exited = app.handle_get(f"/api/sows/{subject_id}", token)[1]
        tier_with = next(m["tier"] for m in with_exited["performance"]["metrics"]
                         if m["key"] == "born_alive")
        assert tier_with != "poor"


class TestExitedSowsInReview:
    def test_exited_sow_never_appears_in_the_flagged_list(self, farm):
        app, token, _ = farm
        sow_id = _post(app, "/api/sows", {"earTag": "1183"}, token)[1]["id"]
        for i, n in enumerate([14, 12, 10]):
            _post(app, "/api/sow-events",
                 {"sowId": sow_id, "type": "FW",
                  "date": (date(2023, 1, 1) + timedelta(days=145 * i)).isoformat(),
                  "detail": {"born_alive": n}}, token)
        _post(app, "/api/sow-events",
             {"sowId": sow_id, "type": "SAL", "date": "2026-07-01"}, token)

        body = app.handle_get("/api/review", token)[1]
        assert body["sows"] == []


class TestCustomTasks:
    """自訂工作:牧場自己排的例行事項(消毒、疫苗、設備檢查)。

    與系統推算的工作**分開回傳** —— 混在一起使用者分不出哪些是系統
    依生產週期算的、哪些是自己設的。
    """

    def test_add_then_list(self, farm):
        app, token, _ = farm
        assert _post(app, "/api/custom-tasks",
                     {"name": "產房消毒", "startDate": "2026-08-19",
                      "repeat": "weekly"}, token)[0] == 200

        tasks = app.handle_get("/api/custom-tasks", token)[1]["tasks"]
        assert tasks[0]["name"] == "產房消毒"
        assert tasks[0]["repeat"] == "weekly"
        assert tasks[0]["repeatLabel"] == "每週"

    def test_name_is_required(self, farm):
        app, token, _ = farm
        for bad in ({}, {"name": ""}, {"name": "   "}):
            payload = {"startDate": "2026-08-19", **bad}
            assert _post(app, "/api/custom-tasks", payload, token)[0] == 400

    def test_start_date_is_required(self, farm):
        app, token, _ = farm
        assert _post(app, "/api/custom-tasks", {"name": "消毒"}, token)[0] == 400
        assert _post(app, "/api/custom-tasks",
                     {"name": "消毒", "startDate": "壞掉"}, token)[0] == 400

    def test_unknown_repeat_rule_is_rejected(self, farm):
        """不認得的規則不猜 —— 存進去之後展開不出日期,工作會安靜消失。"""
        app, token, _ = farm
        assert _post(app, "/api/custom-tasks",
                     {"name": "消毒", "startDate": "2026-08-19",
                      "repeat": "每三天"}, token)[0] == 400

    def test_repeat_defaults_to_once(self, farm):
        app, token, _ = farm
        _post(app, "/api/custom-tasks",
              {"name": "消毒", "startDate": "2026-08-19"}, token)
        assert app.handle_get("/api/custom-tasks", token)[1]["tasks"][0]["repeat"] == "once"

    def test_delete(self, farm):
        app, token, _ = farm
        task_id = _post(app, "/api/custom-tasks",
                        {"name": "消毒", "startDate": "2026-08-19"}, token)[1]["id"]
        assert app.handle_delete(f"/api/custom-tasks/{task_id}", token)[0] == 200
        assert app.handle_get("/api/custom-tasks", token)[1]["tasks"] == []

    def test_delete_missing_is_404(self, farm):
        app, token, _ = farm
        assert app.handle_delete("/api/custom-tasks/999", token)[0] == 404

    def test_worker_can_see_but_not_change(self, farm):
        """員工要知道自己被排了什麼,但排班是牧場主的事。"""
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        task_id = _post(app, "/api/custom-tasks",
                        {"name": "消毒", "startDate": "2026-08-19"}, owner)[1]["id"]

        assert app.handle_get("/api/custom-tasks", worker)[0] == 200
        assert _post(app, "/api/custom-tasks",
                     {"name": "別的", "startDate": "2026-08-19"}, worker)[0] == 403
        assert app.handle_delete(f"/api/custom-tasks/{task_id}", worker)[0] == 403

    def test_does_not_leak_across_farms(self, farm):
        app, a_token, _ = farm
        b_token = _owner(app, "otherfarm")
        _post(app, "/api/custom-tasks",
              {"name": "消毒", "startDate": "2026-08-19"}, a_token)
        assert app.handle_get("/api/custom-tasks", b_token)[1]["tasks"] == []


class TestCustomTasksInTheWeek:
    """自訂工作要出現在 /api/tasks 的這一週裡,而且跟推算的工作分開。"""

    def _add(self, app, token, **over):
        payload = {"name": "產房消毒", "startDate": "2026-08-19",
                   "repeat": "weekly", **over}
        return _post(app, "/api/custom-tasks", payload, token)[1]["id"]

    def test_appears_in_the_week(self, farm):
        app, token, _ = farm
        self._add(app, token)
        body = app.handle_get("/api/tasks?start=2026-08-17", token)[1]
        assert [t["name"] for t in body["custom"]] == ["產房消毒"]
        assert body["custom"][0]["due"] == "2026-08-19"

    def test_kept_separate_from_computed_groups(self, farm):
        """不可以混進 groups —— 那是系統依生產週期推算的。"""
        app, token, _ = farm
        self._add(app, token)
        body = app.handle_get("/api/tasks?start=2026-08-17", token)[1]
        assert body["custom"]
        assert all("產房消毒" not in str(g) for g in body["groups"])

    def test_absent_from_other_weeks(self, farm):
        app, token, _ = farm
        self._add(app, token, repeat="once")
        body = app.handle_get("/api/tasks?start=2026-09-07", token)[1]
        assert body["custom"] == []

    def test_marking_done_sticks_to_that_occurrence(self, farm):
        """這週標了完成,下週同一項工作仍是未完成。"""
        app, token, _ = farm
        task_id = self._add(app, token)

        assert _post(app, "/api/custom-tasks/done",
                     {"taskId": task_id, "due": "2026-08-19", "done": True},
                     token)[0] == 200

        this_week = app.handle_get("/api/tasks?start=2026-08-17", token)[1]["custom"]
        next_week = app.handle_get("/api/tasks?start=2026-08-24", token)[1]["custom"]
        assert this_week[0]["done"] is True
        assert next_week[0]["done"] is False, "下週不該跟著被標成完成"

    def test_unmarking_works(self, farm):
        app, token, _ = farm
        task_id = self._add(app, token)
        _post(app, "/api/custom-tasks/done",
              {"taskId": task_id, "due": "2026-08-19", "done": True}, token)
        _post(app, "/api/custom-tasks/done",
              {"taskId": task_id, "due": "2026-08-19", "done": False}, token)

        body = app.handle_get("/api/tasks?start=2026-08-17", token)[1]
        assert body["custom"][0]["done"] is False

    def test_marking_twice_is_not_an_error(self, farm):
        """網路不穩重送一次不該炸 —— 標記是冪等的。"""
        app, token, _ = farm
        task_id = self._add(app, token)
        for _ in range(2):
            assert _post(app, "/api/custom-tasks/done",
                         {"taskId": task_id, "due": "2026-08-19", "done": True},
                         token)[0] == 200

    def test_worker_can_mark_done(self, farm):
        """工作就是員工在做的,標完成是記錄不是經營決策。"""
        app, owner, farm_id = farm
        worker = _worker(app, farm_id)
        task_id = self._add(app, owner)
        assert _post(app, "/api/custom-tasks/done",
                     {"taskId": task_id, "due": "2026-08-19", "done": True},
                     worker)[0] == 200

    def test_cannot_mark_another_farms_task(self, farm):
        app, a_token, _ = farm
        b_token = _owner(app, "otherfarm")
        task_id = self._add(app, a_token)
        assert _post(app, "/api/custom-tasks/done",
                     {"taskId": task_id, "due": "2026-08-19", "done": True},
                     b_token)[0] == 404

    def test_bad_payload_is_rejected(self, farm):
        app, token, _ = farm
        task_id = self._add(app, token)
        assert _post(app, "/api/custom-tasks/done",
                     {"due": "2026-08-19", "done": True}, token)[0] == 400
        assert _post(app, "/api/custom-tasks/done",
                     {"taskId": task_id, "due": "壞掉", "done": True}, token)[0] == 400
