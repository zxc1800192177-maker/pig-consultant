"""工作推算。

「今天」一律由測試指定,模組內不呼叫 date.today() —— 這是 schedule.py
放頂層(而非 core/)之後仍要維持確定性的方式。

預設間隔量自這個牧場的真實資料(見 schedule.DEFAULTS 的註解),所以測試
用的日期也照那些間隔算,不是隨便挑的數字。
"""

from datetime import date, timedelta

import pytest

import config
import schedule
from schedule import (
    CHECK_DUE, FARROW_DUE, INDUCE, MATE_DUE, MOVE_IN, WEAN_DUE,
    build_week_tasks, current_cycle, overdue_sows, pen_pressure, tasks_for_sow,
)

D = schedule.DEFAULTS


def sow(sow_id=1, tag="1183", **kw):
    return {"id": sow_id, "ear_tag": tag, "status": "active", "pen_id": None, **kw}


def ev(sow_id, code, when, detail=None, excluded=False, eid=None):
    return {"id": eid or 0, "sow_id": sow_id, "event_type": code,
            "event_date": when, "detail": detail or {}, "excluded": excluded}


def kinds(tasks):
    return {t.kind for t in tasks}


def due_of(tasks, kind):
    return next(t.due for t in tasks if t.kind == kind)


class TestCycleTracking:
    """只看最後一次離乳之後的事件 —— 上一胎的配種與這一胎無關。"""

    def test_mating_starts_a_cycle(self):
        c = current_cycle([ev(1, "MT", date(2026, 2, 3))])
        assert c["mate"] == date(2026, 2, 3)

    def test_batch_mating_takes_the_first_day(self):
        """這個場一次配種連續 2–3 天。用最後一天算預產期會系統性短算。"""
        c = current_cycle([ev(1, "MT", date(2026, 2, 3)),
                           ev(1, "MT", date(2026, 2, 4))])
        assert c["mate"] == date(2026, 2, 3)

    def test_weaning_resets_the_cycle(self):
        c = current_cycle([
            ev(1, "MT", date(2025, 10, 13)),
            ev(1, "FW", date(2026, 2, 4)),
            ev(1, "WN", date(2026, 2, 26)),
        ])
        assert c["wean"] == date(2026, 2, 26)
        assert c["mate"] is None, "上一胎的配種不該留到下一個週期"
        assert c["farrow"] is None

    def test_abortion_returns_to_open(self):
        c = current_cycle([ev(1, "MT", date(2026, 2, 3)),
                           ev(1, "AB", date(2026, 3, 1))])
        assert c["mate"] is None

    def test_exit_is_recorded(self):
        c = current_cycle([ev(1, "SAL", date(2026, 5, 1))])
        assert c["exited"] == date(2026, 5, 1)


class TestTasksAfterMating:
    """配種之後會排出五件事:驗孕、移入產房、催產、分娩、移至待產區
    (沒登記驗孕陰性就當懷孕,使用者決定)。
    """

    MATED = date(2026, 2, 3)

    @pytest.fixture
    def tasks(self):
        return tasks_for_sow(sow(), [ev(1, "MT", self.MATED)], D)

    def test_all_five(self, tasks):
        assert kinds(tasks) == {CHECK_DUE, MOVE_IN, INDUCE, FARROW_DUE,
                                schedule.MOVE_TO_GESTATION}

    def test_farrow_is_gestation_days_later(self, tasks):
        assert due_of(tasks, FARROW_DUE) == self.MATED + timedelta(days=114)

    def test_move_in_is_two_weeks_before_farrowing(self, tasks):
        assert due_of(tasks, MOVE_IN) == self.MATED + timedelta(days=114 - 14)

    def test_induction_is_day_113(self, tasks):
        assert due_of(tasks, INDUCE) == self.MATED + timedelta(days=113)

    def test_preg_check_uses_the_farms_actual_interval(self, tasks):
        """26 天量自這個場 1,252 筆實際驗孕記錄,不是教科書的 28 天。"""
        assert due_of(tasks, CHECK_DUE) == self.MATED + timedelta(days=26)

    def test_preg_check_disappears_once_done(self):
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", self.MATED),
            ev(1, "PD", self.MATED + timedelta(days=26), {"positive": True}),
        ], D)
        assert CHECK_DUE not in kinds(tasks)
        assert FARROW_DUE in kinds(tasks), "驗孕完仍要等分娩"


class TestMoveToGestationZone:
    """配種區 → 待產區。使用者明確決定:配種了、沒登記驗孕陰性就當懷孕,
    從配種日起算,不必等驗孕陽性 —— 這個場很多母豬從沒驗孕過。
    """

    MATED = date(2026, 2, 3)
    CHECKED = MATED + timedelta(days=26)

    def test_fires_from_the_mating_date_after_a_positive_check(self):
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", self.MATED),
            ev(1, "PD", self.CHECKED, {"positive": True}),
        ], D)
        assert schedule.MOVE_TO_GESTATION in kinds(tasks)
        assert due_of(tasks, schedule.MOVE_TO_GESTATION) == self.MATED + timedelta(days=60)

    def test_fires_without_any_check(self):
        """配種後從沒驗孕,沒登記「沒懷孕」就當懷孕,一樣要排到待產區。"""
        tasks = tasks_for_sow(sow(), [ev(1, "MT", self.MATED)], D)
        assert schedule.MOVE_TO_GESTATION in kinds(tasks)
        assert due_of(tasks, schedule.MOVE_TO_GESTATION) == self.MATED + timedelta(days=60)

    def test_does_not_fire_after_a_negative_check(self):
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", self.MATED),
            ev(1, "PD", self.CHECKED, {"positive": False}),
        ], D)
        assert schedule.MOVE_TO_GESTATION not in kinds(tasks)

    def test_fires_when_the_result_is_unrecorded(self):
        """有驗但結果沒填,跟根本沒驗孕是不同的問題,但一樣不是「沒懷孕」——
        照樣當懷孕看待。"""
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", self.MATED),
            ev(1, "PD", self.CHECKED, {}),
        ], D)
        assert schedule.MOVE_TO_GESTATION in kinds(tasks)

    def test_interval_is_configurable(self):
        cfg = schedule.settings_with_defaults({"to_gestation_zone_days": 45})
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", self.MATED),
            ev(1, "PD", self.CHECKED, {"positive": True}),
        ], cfg)
        assert due_of(tasks, schedule.MOVE_TO_GESTATION) == self.MATED + timedelta(days=45)


class TestTasksAfterFarrowing:
    def test_weaning_is_due(self):
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", date(2025, 10, 13)),
            ev(1, "FW", date(2026, 2, 4)),
        ], D)
        assert kinds(tasks) == {WEAN_DUE}
        assert due_of(tasks, WEAN_DUE) == date(2026, 2, 4) + timedelta(days=22)

    def test_nothing_left_after_weaning_except_mating(self):
        """離乳後只剩「該配種」跟「該移至配種區」兩件事 —— 移入產房、
        催產、分娩那些都是上一胎的,不該留到這一胎。
        """
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", date(2025, 10, 13)),
            ev(1, "FW", date(2026, 2, 4)),
            ev(1, "WN", date(2026, 2, 26)),
        ], D)
        assert kinds(tasks) == {MATE_DUE, schedule.MOVE_TO_MATING}
        assert due_of(tasks, MATE_DUE) == date(2026, 2, 26) + timedelta(days=5)
        assert due_of(tasks, schedule.MOVE_TO_MATING) == date(2026, 2, 26)


class TestNoTasksWhenGone:
    def test_culled_sow_has_none(self):
        assert tasks_for_sow(sow(status="culled"), [ev(1, "MT", date(2026, 2, 3))], D) == []

    def test_exit_event_stops_tasks(self):
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", date(2026, 2, 3)),
            ev(1, "DTH", date(2026, 3, 1)),
        ], D)
        assert tasks == []


class TestExcludedEventsAreIgnored:
    """匯入時被判定為離群值的記錄不納入統計,也不該拿來推算下一步。"""

    def test_excluded_farrowing_does_not_schedule_weaning(self):
        groups = build_week_tasks(
            [sow()],
            [ev(1, "MT", date(2025, 10, 13)),
             ev(1, "FW", date(2026, 2, 4), excluded=True)],
            date(2026, 2, 23), date(2026, 3, 1))
        assert all(g["kind"] != WEAN_DUE for g in groups)


class TestWeekGrouping:
    """依工作類型分組,不按日期 —— 這個場一週一批,整批同步。"""

    def test_groups_by_kind(self):
        sows = [sow(1, "1183"), sow(2, "2580")]
        events = [ev(1, "FW", date(2026, 2, 4)), ev(2, "FW", date(2026, 2, 5))]
        groups = build_week_tasks(sows, events, date(2026, 2, 23), date(2026, 3, 1))

        assert len(groups) == 1
        assert groups[0]["kind"] == WEAN_DUE
        assert [t.ear_tag for t in groups[0]["tasks"]] == ["1183", "2580"]

    def test_tasks_outside_the_week_are_excluded(self):
        groups = build_week_tasks([sow()], [ev(1, "FW", date(2026, 2, 4))],
                                  date(2026, 1, 1), date(2026, 1, 7))
        assert groups == []

    def test_every_task_says_why(self):
        groups = build_week_tasks([sow()], [ev(1, "MT", date(2026, 2, 3))],
                                  date(2026, 5, 1), date(2026, 6, 30))
        for g in groups:
            for t in g["tasks"]:
                assert t.why, "每件工作都要說明為什麼是這天"


class TestOverdue:
    """離乳後太久沒配種 —— 這是提醒,不是工作,兩者刻意分開。"""

    def test_flags_long_open_sow(self):
        rows = overdue_sows([sow()], [ev(1, "WN", date(2026, 1, 1))], date(2026, 3, 1))
        assert rows[0]["ear_tag"] == "1183"
        assert rows[0]["days"] == 59

    def test_within_the_window_is_fine(self):
        rows = overdue_sows([sow()], [ev(1, "WN", date(2026, 2, 20))], date(2026, 3, 1))
        assert rows == []

    def test_mated_sow_is_not_overdue(self):
        rows = overdue_sows([sow()], [
            ev(1, "WN", date(2026, 1, 1)),
            ev(1, "MT", date(2026, 1, 6)),
        ], date(2026, 3, 1))
        assert rows == []

    def test_sorted_worst_first(self):
        rows = overdue_sows(
            [sow(1, "A"), sow(2, "B")],
            [ev(1, "WN", date(2026, 1, 20)), ev(2, "WN", date(2025, 6, 1))],
            date(2026, 3, 1))
        assert [r["ear_tag"] for r in rows] == ["B", "A"]


class TestPenPressure:
    """產房空間夠不夠。

    **總欄數是使用者在設定裡自己填的「總產房數」**(使用者決定),不是
    算出來的 —— pens 資料表只會累積「曾經被移欄記錄提到的欄位名稱」,
    牧場實際的總欄數在還沒被記錄過之前不會出現在那份清單裡,拿清單
    長度當總數會系統性低估。

    **佔用**仍然來自真實的欄位指派(sows.pen_id → pens.zone,由
    MOVE_PEN 事件維護),不是猜的。
    """

    @staticmethod
    def pen(pen_id=1, name="1", zone="farrowing"):
        return {"id": pen_id, "farm_id": 1, "name": name, "zone": zone}

    TWO = [{"id": 1, "farm_id": 1, "name": "1", "zone": "farrowing"},
           {"id": 2, "farm_id": 1, "name": "2", "zone": "farrowing"}]

    def test_counts_sows_due_to_move_in(self):
        mated = date(2026, 3, 1) - timedelta(days=114 - 14)
        r = pen_pressure([sow(1, "1183"), sow(2, "2580")],
                         [ev(1, "MT", mated), ev(2, "MT", mated)],
                         self.TWO, date(2026, 3, 1),
                         settings={"farrowing_pens": 2})
        assert r["incoming"] == 2
        assert r["short_by"] == 0

    def test_short_when_more_coming_than_free(self):
        mated = date(2026, 3, 1) - timedelta(days=114 - 14)
        sows = [sow(i, str(i)) for i in range(1, 4)]
        events = [ev(i, "MT", mated) for i in range(1, 4)]
        r = pen_pressure(sows, events, self.TWO, date(2026, 3, 1),
                         settings={"farrowing_pens": 2})
        assert r["incoming"] == 3
        assert r["short_by"] == 1

    def test_total_comes_from_settings_not_from_the_pens_list(self):
        """這正是這次改版要修的問題:拿 pens 清單長度當總數,牧場真正
        的產房總數在還沒被移欄記錄提到之前不會出現在清單裡,會低估。
        """
        r = pen_pressure([], [], self.TWO, date(2026, 3, 1),
                         settings={"farrowing_pens": 50})
        assert r["total"] == 50

    def test_occupied_counts_real_pen_assignments(self):
        """佔用來自真實的欄位指派,不是從生產週期猜的。"""
        assigned = sow(1, "1183", pen_id=1)
        r = pen_pressure([assigned], [ev(1, "FW", date(2026, 2, 25))],
                         self.TWO, date(2026, 3, 1),
                         settings={"farrowing_pens": 2})
        assert r["occupied"] == 1
        assert r["free"] == 1

    def test_lactating_without_a_real_assignment_is_not_counted(self):
        """舊版靠「已分娩未離乳」猜佔用,沒有真的指派欄位也算佔用;
        現在只認真的指派。
        """
        r = pen_pressure([sow(1, "1183")], [ev(1, "FW", date(2026, 2, 25))],
                         [self.pen(1)], date(2026, 3, 1),
                         settings={"farrowing_pens": 1})
        assert r["occupied"] == 0
        assert r["free"] == 1

    def test_assignment_to_a_non_farrowing_pen_does_not_occupy_farrowing_capacity(self):
        """配種區、待產區的欄位指派不算進產房佔用。"""
        pens = [self.pen(1, zone="mating"), self.pen(2, zone="farrowing")]
        assigned = sow(1, "1183", pen_id=1)      # 指派到配種區,不是產房
        r = pen_pressure([assigned], [], pens, date(2026, 3, 1),
                         settings={"farrowing_pens": 1})
        assert r["occupied"] == 0
        assert r["free"] == 1

    def test_no_farrowing_pens_means_unconfigured(self):
        """總產房數沒填(預設 0)時不可以宣稱空間不足 —— 那是憑空捏造的
        警示(憲法第三條)。
        """
        mated = date(2026, 3, 1) - timedelta(days=114 - 14)
        sows = [sow(i, str(i)) for i in range(1, 4)]
        events = [ev(i, "MT", mated) for i in range(1, 4)]
        r = pen_pressure(sows, events, [], date(2026, 3, 1))
        assert r["configured"] is False
        assert r["incoming"] == 3      # 進來幾頭照算,那不需要知道容量
        assert r["short_by"] == 0      # 但不宣稱缺幾欄

    def test_configured_flag_is_true_once_farrowing_pens_is_set(self):
        r = pen_pressure([], [], [], date(2026, 3, 1),
                         settings={"farrowing_pens": 2})
        assert r["configured"] is True
        assert r["total"] == 2


class TestSettingsOverride:
    def test_farm_can_change_the_intervals(self):
        tasks = tasks_for_sow(sow(), [ev(1, "MT", date(2026, 2, 3))],
                              schedule.settings_with_defaults({"gestation_days": 115}))
        assert due_of(tasks, FARROW_DUE) == date(2026, 2, 3) + timedelta(days=115)

    def test_none_falls_back_to_default(self):
        cfg = schedule.settings_with_defaults({"gestation_days": None})
        assert cfg["gestation_days"] == 114


class TestDeterminism:
    """schedule.py 不得呼叫 date.today() —— 那正是 core/ 禁 datetime 想
    防止的事(同樣輸入不同輸出)。這裡靠「把今天當參數」達成同樣效果。
    """

    def test_module_never_calls_today(self):
        """用 AST 找真正的呼叫,不是字串比對 —— 說明文字裡本來就會提到
        `date.today()`(解釋為什麼不用它),字串比對會誤判。
        `test_core_purity.py` 也是走 AST 的同一種做法。
        """
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path(schedule.__file__).read_text(encoding="utf-8"))
        banned = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("today", "now", "utcnow"):
                    banned.append(f"第 {node.lineno} 行:{node.func.attr}()")
        assert banned == [], (
            "schedule.py 不得自己取得當下時間,「今天」必須由呼叫端傳入 —— "
            "否則同樣的輸入會在不同日子得到不同結果。" + str(banned)
        )


class TestNegativePregnancyCheck:
    """驗孕陰性 = 沒懷孕,必須回到待配種。

    這是用真實資料驗證時抓到的 bug:少了這個處理,母豬會一直被排出永遠
    不會發生的分娩與催產工作。實測影響很大 —— 分娩預測的平均誤差從
    +8.9 天降到 +3.65 天,誤差在 3 天內的比例從 81.4% 升到 90.2%。
    這個場目前就有 50 頭處於驗孕陰性狀態。
    """

    MATED = date(2026, 2, 3)
    NEGATIVE = [
        ev(1, "MT", MATED),
        ev(1, "PD", MATED + timedelta(days=26), {"positive": False}),
    ]

    def test_no_farrowing_task_after_negative_check(self):
        assert FARROW_DUE not in kinds(tasks_for_sow(sow(), self.NEGATIVE, D))

    def test_no_induction_task_after_negative_check(self):
        assert INDUCE not in kinds(tasks_for_sow(sow(), self.NEGATIVE, D))

    def test_no_pen_move_after_negative_check(self):
        assert MOVE_IN not in kinds(tasks_for_sow(sow(), self.NEGATIVE, D))

    def test_she_needs_mating_again(self):
        assert MATE_DUE in kinds(tasks_for_sow(sow(), self.NEGATIVE, D))

    def test_positive_check_keeps_the_pregnancy(self):
        positive = [
            ev(1, "MT", self.MATED),
            ev(1, "PD", self.MATED + timedelta(days=26), {"positive": True}),
        ]
        assert FARROW_DUE in kinds(tasks_for_sow(sow(), positive, D))

    def test_negative_check_counts_toward_overdue(self):
        """驗孕陰性後沒有再配種,拖久了要進提醒名單。"""
        rows = overdue_sows([sow()], self.NEGATIVE, date(2026, 6, 1))
        assert rows and rows[0]["ear_tag"] == "1183"

    def test_negative_sow_does_not_occupy_farrowing_pen_forecast(self):
        """她不會分娩,就不該被算進產房需求 —— 否則產房永遠顯示不夠。"""
        mated = date(2026, 3, 1) - timedelta(days=114 - 14)
        events = [ev(1, "MT", mated),
                  ev(1, "PD", mated + timedelta(days=26), {"positive": False})]
        pens = [{"id": 1, "farm_id": 1, "name": "1", "zone": "farrowing"}]
        r = pen_pressure([sow()], events, pens, date(2026, 3, 1))
        assert r["incoming"] == 0


class TestWorthReview:
    """「值得檢視」名單。

    門檻量自這個牧場 451 頭在場母豬的實際分布(見 schedule.DEFAULTS),
    不是憑感覺挑的:連續下滑 2 胎有 20 頭、3 胎只有 2 頭,取 3 等於這份
    名單永遠是空的。

    最重要的一條在 TestReviewWordingIsNotACullRecommendation ——
    措辭不得變成淘汰建議。
    """

    @staticmethod
    def farrows(sow_id, alive, start=date(2023, 1, 1), gap=145):
        """依序幾胎,每胎活仔數如 alive 所列。"""
        return [ev(sow_id, "FW", start + timedelta(days=gap * i), {"born_alive": n})
                for i, n in enumerate(alive)]

    def test_steady_sow_is_not_listed(self):
        rows = schedule.sows_worth_review(
            [sow()], self.farrows(1, [12, 12, 13]), date(2026, 1, 1))
        assert rows == []

    def test_consecutive_decline_is_listed(self):
        rows = schedule.sows_worth_review(
            [sow()], self.farrows(1, [14, 12, 10]), date(2026, 1, 1))
        assert [r["ear_tag"] for r in rows] == ["1183"]
        assert {x["code"] for x in rows[0]["reasons"]} >= {"decline"}

    def test_one_bad_litter_is_not_a_trend(self):
        """單胎掉下來不算,下滑要連續 —— 否則名單會塞滿正常波動。"""
        rows = schedule.sows_worth_review(
            [sow()], self.farrows(1, [12, 13, 11]), date(2026, 1, 1))
        assert not any(x["code"] == "decline"
                       for r in rows for x in r["reasons"])

    def test_too_few_litters_is_never_judged(self):
        """一兩胎看不出趨勢。新母豬不該因為第二胎少一隻就被列出來。"""
        rows = schedule.sows_worth_review(
            [sow()], self.farrows(1, [14, 10]), date(2026, 1, 1))
        assert rows == []

    def test_reason_states_the_actual_numbers(self):
        """只說「連續下滑」而不給數字,使用者無從判斷該不該採信。"""
        rows = schedule.sows_worth_review(
            [sow()], self.farrows(1, [14, 12, 10]), date(2026, 1, 1))
        detail = next(x["detail"] for x in rows[0]["reasons"] if x["code"] == "decline")
        assert "14" in detail and "12" in detail and "10" in detail

    def test_long_non_productive_days_is_listed(self):
        """胎間隔拉長 = 非生產天數多。114+22=136 天是正常間隔。"""
        rows = schedule.sows_worth_review(
            [sow()], self.farrows(1, [12, 12, 12], gap=200), date(2026, 1, 1))
        assert any(x["code"] == "npd" for x in rows[0]["reasons"])

    def test_normal_interval_is_not_flagged_for_npd(self):
        rows = schedule.sows_worth_review(
            [sow()], self.farrows(1, [12, 12, 12], gap=140), date(2026, 1, 1))
        assert not any(x["code"] == "npd" for r in rows for x in r["reasons"])

    def test_low_alive_is_measured_against_the_same_farm(self):
        """與同場其他母豬比,不與全國常模比(已確認的設計決定)。

        同樣是平均 8 隻,在一個平均 12 隻的場裡是墊底,在平均 8 隻的場裡
        很普通 —— 門檻寫死就只為某一場服務。
        """
        weak = sow(1, "0001")
        peers = [sow(i, f"{i:04d}") for i in range(2, 12)]
        events = self.farrows(1, [8, 8, 8])
        for p in peers:
            events += self.farrows(p["id"], [14, 14, 14])

        rows = schedule.sows_worth_review([weak] + peers, events, date(2026, 1, 1))
        listed = {r["ear_tag"] for r in rows}
        assert "0001" in listed
        assert any(x["code"] == "low_alive"
                   for r in rows if r["ear_tag"] == "0001" for x in r["reasons"])

    def test_same_numbers_are_fine_in_a_weaker_herd(self):
        """整場都是 8 隻時,8 隻的那頭不該因為「低」被列出來。"""
        herd = [sow(i, f"{i:04d}") for i in range(1, 12)]
        events = []
        for s in herd:
            events += self.farrows(s["id"], [8, 8, 8])
        rows = schedule.sows_worth_review(herd, events, date(2026, 1, 1))
        assert not any(x["code"] == "low_alive"
                       for r in rows for x in r["reasons"])

    def test_missing_litter_size_is_not_counted_as_zero(self):
        """PigCHAMP 的分娩記錄偶爾沒有仔數欄位。當成 0 會憑空造出一次
        「活仔 0」的慘況,讓她被誤判成表現崩壞。
        """
        events = [ev(1, "FW", date(2023, 1, 1), {"born_alive": 12}),
                  ev(1, "FW", date(2023, 6, 1), {}),          # 沒填
                  ev(1, "FW", date(2023, 11, 1), {"born_alive": 12}),
                  ev(1, "FW", date(2024, 4, 1), {"born_alive": 12})]
        rows = schedule.sows_worth_review([sow()], events, date(2026, 1, 1))
        assert not any(x["code"] == "decline" for r in rows for x in r["reasons"])

    def test_culled_sows_are_not_listed(self):
        """已經離群的不必再檢視。"""
        rows = schedule.sows_worth_review(
            [sow(status="culled")], self.farrows(1, [14, 12, 10]), date(2026, 1, 1))
        assert rows == []

    def test_excluded_events_are_ignored(self):
        """匯入時判定為離群值的記錄不納入統計,也不該影響這份名單。"""
        events = self.farrows(1, [14, 12, 10])
        events.append(ev(1, "FW", date(2024, 6, 1), {"born_alive": 1}, excluded=True))
        rows = schedule.sows_worth_review([sow()], events, date(2026, 1, 1))
        assert not any("1" == x["detail"][-1] for x in rows[0]["reasons"])

    def test_more_reasons_sort_first(self):
        """理由多的先看。"""
        many = sow(1, "0001")
        one = sow(2, "0002")
        events = (self.farrows(1, [14, 12, 10], gap=250)
                  + self.farrows(2, [14, 12, 10], gap=140))
        rows = schedule.sows_worth_review([many, one], events, date(2026, 1, 1))
        assert rows[0]["ear_tag"] == "0001"
        assert len(rows[0]["reasons"]) > len(rows[-1]["reasons"])

    def test_thresholds_are_configurable(self):
        """別的牧場的分布不會一樣,寫死等於只為這一場服務。"""
        events = self.farrows(1, [12, 13, 11])
        loose = schedule.sows_worth_review(
            [sow()], events, date(2026, 1, 1), {"review_decline_litters": 1})
        assert any(x["code"] == "decline" for r in loose for x in r["reasons"])

    def test_no_sows_no_crash(self):
        assert schedule.sows_worth_review([], [], date(2026, 1, 1)) == []


class TestReviewWordingIsNotACullRecommendation:
    """措辭把關。

    這個場實際的淘汰原因裡「年齡太大」佔 48.0%,「生產性能差」只佔 2.9%
    (specs/v2-facts.md 第 10 條)—— 系統算得出來的正好是最少被拿來當決策
    依據的那一項。名單只能陳述事實,決定權在牧場主(憲法第三條第 6 款)。
    """

    def test_labels_never_say_cull(self):
        from core.labels import REVIEW_LABELS
        for text in REVIEW_LABELS.values():
            assert "淘汰" not in text, f"「值得檢視」的理由不可寫成淘汰建議:{text}"
            assert "建議" not in text, text

    def test_reason_details_never_say_cull(self):
        rows = schedule.sows_worth_review(
            [sow()], TestWorthReview.farrows(1, [14, 12, 10], gap=250),
            date(2026, 1, 1))
        for reason in rows[0]["reasons"]:
            assert "淘汰" not in reason["detail"]

    def test_caveat_states_the_gap(self):
        """但書要講明白系統看得到的不是主要決策依據。"""
        from core.labels import review_caveat
        text = review_caveat()
        assert "48" in text and "2.9" in text, "但書要帶上實際的淘汰原因佔比"
        assert "不是淘汰建議" in text


class TestSowStatus:
    """母豬目前狀態與預產期。"""

    MATED = date(2026, 3, 1)
    TODAY = date(2026, 5, 1)

    def test_mated_but_unchecked_counts_as_pregnant(self):
        """配種了、沒登記驗孕陰性,就當懷孕(使用者決定)—— 這個場很少
        逐頭驗孕,只認陽性驗孕的話,大多數其實已經懷孕的母豬會一直卡在
        模糊狀態。
        """
        s = schedule.sow_status(sow(), [ev(1, "MT", self.MATED)], self.TODAY)
        assert s["state"] == "pregnant"

    def test_positive_check_makes_her_pregnant(self):
        events = [ev(1, "MT", self.MATED),
                  ev(1, "PD", self.MATED + timedelta(days=26), {"positive": True})]
        assert schedule.sow_status(sow(), events, self.TODAY)["state"] == "pregnant"

    def test_negative_check_puts_her_back_to_open(self):
        events = [ev(1, "MT", self.MATED),
                  ev(1, "PD", self.MATED + timedelta(days=26), {"positive": False})]
        assert schedule.sow_status(sow(), events, self.TODAY)["state"] == "open"

    def test_due_date_is_mating_plus_gestation(self):
        s = schedule.sow_status(sow(), [ev(1, "MT", self.MATED)], self.TODAY)
        assert s["due"] == self.MATED + timedelta(days=D["gestation_days"])

    def test_due_date_is_given_even_before_the_check(self):
        """還沒驗孕的那幾天照樣要準備產房,所以預產期兩種狀態都要給。"""
        assert schedule.sow_status(sow(), [ev(1, "MT", self.MATED)], self.TODAY)["due"]

    def test_due_date_uses_the_first_day_of_a_batch(self):
        """這個場一次配種連續 2~3 天。用最後一天算預產期會一路短算。"""
        events = [ev(1, "MT", self.MATED), ev(1, "MT", self.MATED + timedelta(days=1))]
        s = schedule.sow_status(sow(), events, self.TODAY)
        assert s["due"] == self.MATED + timedelta(days=D["gestation_days"])

    def test_day_counts_from_mating(self):
        s = schedule.sow_status(sow(), [ev(1, "MT", self.MATED)], self.TODAY)
        assert s["day"] == 61

    def test_lactating_after_farrowing(self):
        farrowed = date(2026, 4, 20)
        s = schedule.sow_status(sow(), [ev(1, "FW", farrowed)], self.TODAY)
        assert s["state"] == "lactating"
        assert s["day"] == 11
        assert s["wean_due"] == farrowed + timedelta(days=D["lactation_days"])

    def test_no_due_date_once_she_has_farrowed(self):
        """已經生了就沒有預產期。留著會讓畫面顯示一個過去的日期。"""
        events = [ev(1, "MT", self.MATED), ev(1, "FW", date(2026, 4, 20))]
        assert schedule.sow_status(sow(), events, self.TODAY)["due"] is None

    def test_open_after_weaning_counts_empty_days(self):
        weaned = date(2026, 4, 1)
        s = schedule.sow_status(sow(), [ev(1, "FW", date(2026, 3, 10)),
                                        ev(1, "WN", weaned)], self.TODAY)
        assert s["state"] == "open"
        assert s["day"] == 30

    def test_culled_sow_reports_exited(self):
        s = schedule.sow_status(sow(), [ev(1, "SAL", date(2026, 4, 1))], self.TODAY)
        assert s["state"] == "exited"

    def test_status_field_on_the_sow_is_respected(self):
        """事件還沒補登、但母豬已標成淘汰時,不該顯示成待配種。"""
        s = schedule.sow_status(sow(status="culled"), [], self.TODAY)
        assert s["state"] == "exited"

    def test_no_events_does_not_crash(self):
        s = schedule.sow_status(sow(), [], self.TODAY)
        assert s["state"] == "open"
        assert s["day"] is None


class TestSowPerformance:
    """母豬卡的生產表現。"""

    @staticmethod
    def litters(sow_id, specs, start=date(2023, 1, 1), gap=145):
        """specs: [(活仔, 死胎, 離乳)] 依序幾胎。"""
        out = []
        for i, (alive, still, weaned) in enumerate(specs):
            day = start + timedelta(days=gap * i)
            out.append(ev(sow_id, "FW", day,
                          {"born_alive": alive, "stillborn": still, "mummified": 0}))
            out.append(ev(sow_id, "WN", day + timedelta(days=22), {"weaned": weaned}))
        return out

    def test_no_farrowings_returns_none(self):
        """沒生過就沒有表現可談。補一組 0 會把場內比較一起拉歪。"""
        assert schedule.sow_performance([ev(1, "MT", date(2026, 1, 1))]) is None

    def test_averages(self):
        p = schedule.sow_performance(self.litters(1, [(12, 1, 11), (10, 1, 9)]))
        assert p["born_alive"] == 11
        assert p["weaned"] == 10
        assert p["total_born"] == 12          # (12+1 + 10+1) / 2

    def test_stillborn_rate_is_a_percentage_of_total_born(self):
        p = schedule.sow_performance(self.litters(1, [(9, 1, 9)]))
        assert p["stillborn_rate"] == 10.0

    def test_missing_counts_are_not_treated_as_zero(self):
        """缺欄位當 0 會少算,而少算的窩看起來只是「比較小窩」,
        沒有人會發現數字是壞的。
        """
        events = [ev(1, "FW", date(2023, 1, 1), {"born_alive": 12}),
                  ev(1, "FW", date(2023, 6, 1), {})]
        p = schedule.sow_performance(events)
        assert p["born_alive"] == 12          # 只有一窩有數字
        assert p["total_born"] is None        # 沒有死胎欄位就不宣稱總仔數

    def test_litters_per_year_uses_the_interval_not_time_on_farm(self):
        """用「胎數 ÷ 在場年數」會把進場後還沒配種的那段也算進去,
        新母豬因此永遠很難看。
        """
        p = schedule.sow_performance(self.litters(1, [(12, 0, 11)] * 3, gap=146))
        assert 2.4 < p["litters_per_year"] < 2.6

    def test_single_litter_has_no_annual_rate(self):
        p = schedule.sow_performance(self.litters(1, [(12, 0, 11)]))
        assert p["litters_per_year"] is None

    def test_lactation_days_pair_each_farrowing_with_its_wean(self):
        p = schedule.sow_performance(self.litters(1, [(12, 0, 11), (11, 0, 10)]))
        assert p["lactation_days"] == 22

    def test_there_is_no_per_sow_pre_weaning_mortality(self):
        """仔豬會在窩間流動(25.3% 的配對離乳數大於活仔數),
        (活仔−離乳)/活仔 對個別母豬無意義。這個坑踩過一次。
        """
        p = schedule.sow_performance(self.litters(1, [(12, 0, 11)]))
        assert not any("mortal" in k or "死亡" in k for k in p)


class TestBoarPerformance:
    """公豬卡的配種績效。從母豬那邊的配種記錄比對公豬耳號算,不是猜的
    —— MT 事件本來就記了 boar_tag。
    """

    MATED = date(2026, 2, 3)
    CHECKED = MATED + timedelta(days=26)
    FARROWED = MATED + timedelta(days=114)

    def test_no_matings_returns_none(self):
        events = [ev(1, "MT", self.MATED, {"boar_tag": "D6"})]
        assert schedule.boar_performance("D9", events) is None

    def test_blank_boar_tag_returns_none(self):
        """不能拿「沒填公豬耳號」的配種記錄去湊出一頭公豬的績效。"""
        events = [ev(1, "MT", self.MATED, {"boar_tag": ""})]
        assert schedule.boar_performance("", events) is None

    def test_counts_a_bare_mating_with_no_result_yet(self):
        """剛配種、還沒驗孕 —— 算進配種次數,但不能算進任何比率的分母。"""
        events = [ev(1, "MT", self.MATED, {"boar_tag": "D6"})]
        p = schedule.boar_performance("D6", events)
        assert p["matings"] == 1
        assert p["sowsMated"] == 1
        assert p["checked"] == 0
        assert p["positiveRate"] is None
        assert p["litters"] == 0
        assert p["avgBornAlive"] is None

    def test_positive_check_counts_toward_the_rate(self):
        events = [
            ev(1, "MT", self.MATED, {"boar_tag": "D6"}),
            ev(1, "PD", self.CHECKED, {"positive": True}),
        ]
        p = schedule.boar_performance("D6", events)
        assert p["checked"] == 1
        assert p["positiveRate"] == 100.0

    def test_negative_check_counts_toward_the_rate_as_a_failure(self):
        events = [
            ev(1, "MT", self.MATED, {"boar_tag": "D6"}),
            ev(1, "PD", self.CHECKED, {"positive": False}),
        ]
        p = schedule.boar_performance("D6", events)
        assert p["checked"] == 1
        assert p["positiveRate"] == 0.0

    def test_farrow_counts_a_litter_even_without_a_born_alive_count(self):
        """有分娩就算一窩,活仔數缺記錄時窩數仍然算,只是不進平均活仔數。"""
        events = [
            ev(1, "MT", self.MATED, {"boar_tag": "D6"}),
            ev(1, "FW", self.FARROWED, {}),
        ]
        p = schedule.boar_performance("D6", events)
        assert p["litters"] == 1
        assert p["avgBornAlive"] is None

    def test_average_born_alive_across_his_litters(self):
        events = [
            ev(1, "MT", self.MATED, {"boar_tag": "D6"}),
            ev(1, "FW", self.FARROWED, {"born_alive": 12}),
            ev(1, "WN", self.FARROWED + timedelta(days=22), {"weaned": 11}),
            ev(1, "MT", self.FARROWED + timedelta(days=27), {"boar_tag": "D6"}),
            ev(1, "FW", self.FARROWED + timedelta(days=27 + 114), {"born_alive": 10}),
        ]
        p = schedule.boar_performance("D6", events)
        assert p["matings"] == 2
        assert p["litters"] == 2
        assert p["avgBornAlive"] == 11

    def test_only_matings_by_this_boar_are_counted(self):
        events = [
            ev(1, "MT", self.MATED, {"boar_tag": "D6"}),
            ev(1, "FW", self.FARROWED, {"born_alive": 12}),
            ev(1, "WN", self.FARROWED + timedelta(days=22), {"weaned": 11}),
            ev(1, "MT", self.FARROWED + timedelta(days=27), {"boar_tag": "D9"}),
        ]
        p = schedule.boar_performance("D6", events)
        assert p["matings"] == 1
        assert p["litters"] == 1

    def test_sows_mated_counts_distinct_sows_not_matings(self):
        """同一頭母豬重配(例如驗孕陰性後再配一次)不能讓配過的母豬數
        灌水。
        """
        events = [
            ev(1, "MT", self.MATED, {"boar_tag": "D6"}),
            ev(1, "PD", self.CHECKED, {"positive": False}),
            ev(1, "MT", self.CHECKED + timedelta(days=5), {"boar_tag": "D6"}),
        ]
        p = schedule.boar_performance("D6", events)
        assert p["matings"] == 2
        assert p["sowsMated"] == 1

    def test_matings_across_different_sows_all_count(self):
        events = [
            ev(1, "MT", self.MATED, {"boar_tag": "D6"}),
            ev(2, "MT", self.MATED, {"boar_tag": "D6"}),
        ]
        p = schedule.boar_performance("D6", events)
        assert p["matings"] == 2
        assert p["sowsMated"] == 2

    def test_excluded_events_are_ignored(self):
        """匯入時判定為離群值的記錄不該算進公豬的績效。"""
        events = [ev(1, "MT", self.MATED, {"boar_tag": "D6"}, excluded=True)]
        assert schedule.boar_performance("D6", events) is None

    def test_a_later_mating_ends_the_previous_attempts_result_window(self):
        """下一次配種之後才發生的驗孕不該算進上一次配種的結果 ——
        那筆驗孕是針對新的這次配種。
        """
        events = [
            ev(1, "MT", self.MATED, {"boar_tag": "D6"}),
            ev(1, "MT", self.MATED + timedelta(days=1), {"boar_tag": "D9"}),
            ev(1, "PD", self.CHECKED, {"positive": True}),
        ]
        p6 = schedule.boar_performance("D6", events)
        p9 = schedule.boar_performance("D9", events)
        assert p6["checked"] == 0
        assert p9["checked"] == 1


class TestTierWithinFarm:
    """場內三級。與同場其他母豬比,不與全國常模比(已確認的設計決定)。"""

    HERD = [8, 9, 10, 11, 12, 13, 14, 15, 16, 17]

    def test_top_third_is_good(self):
        assert schedule.tier_within_farm(17, self.HERD, True) == "good"

    def test_bottom_third_is_poor(self):
        assert schedule.tier_within_farm(8, self.HERD, True) == "poor"

    def test_middle_is_mid(self):
        assert schedule.tier_within_farm(12, self.HERD, True) == "mid"

    def test_direction_flips_for_lower_is_better(self):
        """死胎率跟其他項相反 —— 漏掉的話最會生的那幾頭會被標成待改善。"""
        assert schedule.tier_within_farm(8, self.HERD, False) == "good"
        assert schedule.tier_within_farm(17, self.HERD, False) == "poor"

    def test_small_herd_is_not_graded(self):
        """頭數太少時三分位只是把母豬排名,那不是評價。"""
        assert schedule.tier_within_farm(12, [10, 12, 14], True) is None

    def test_identical_herd_is_not_graded(self):
        """全場一模一樣就分不出高下,不該硬給一級。"""
        assert schedule.tier_within_farm(12, [12] * 20, True) is None

    def test_missing_value_is_not_graded(self):
        assert schedule.tier_within_farm(None, self.HERD, True) is None


class TestStillbornConcentratedInFirstLitter:
    """死胎全部集中在最早那一胎。

    實測 2580:整體 17.1%,排除最早那胎只有 3.2% —— 14 隻裡 11 隻死胎都在
    那一窩,之後六胎再無死胎。不講的話那會被當成長期問題,但它是一次性的。
    """

    # 2580 的真實七胎(活仔, 死胎)
    REAL = [(3, 11), (3, 2), (14, 0), (15, 0), (16, 0), (7, 0), (5, 0)]

    @staticmethod
    def farrows(specs, start=date(2023, 1, 1), gap=145):
        return [ev(1, "FW", start + timedelta(days=gap * i),
                   {"born_alive": a, "stillborn": s, "mummified": 0})
                for i, (a, s) in enumerate(specs)]

    def test_matches_the_real_numbers(self):
        p = schedule.sow_performance(self.farrows(self.REAL))
        assert round(p["stillborn_rate"], 1) == 17.1
        note = p["stillborn_note"]
        assert note is not None
        assert round(note["without_first"], 1) == 3.2

    def test_evenly_spread_stillbirths_get_no_note(self):
        """每一胎都有死胎就是長期問題,不該說成「集中在最早那一胎」。"""
        p = schedule.sow_performance(self.farrows([(10, 2), (10, 2), (10, 2), (10, 2)]))
        assert p["stillborn_note"] is None

    def test_low_overall_rate_gets_no_note(self):
        """整體本來就不高時再拆解只是雜訊。"""
        p = schedule.sow_performance(self.farrows([(14, 1), (15, 0), (16, 0), (15, 0)]))
        assert p["stillborn_note"] is None

    def test_too_few_litters_gets_no_note(self):
        """兩胎時「排除第一胎」只剩一胎,那不是趨勢。"""
        p = schedule.sow_performance(self.farrows([(3, 11), (14, 0)]))
        assert p["stillborn_note"] is None

    def test_wording_does_not_claim_it_was_parity_one(self):
        """匯入的歷史不保證從她的頭胎開始 —— 宣稱是第 1 胎會是編的。"""
        from core.labels import stillborn_note
        text = stillborn_note(17.1, 3.2)
        assert "最早記錄" in text
        assert "第 1 胎" not in text


class TestOverdueFarrowing:
    """預產日過了卻沒有分娩記錄。

    她要嘛生了沒登記,要嘛沒保住 —— 兩種都要有人去看。實測 200 頭裡有
    88 頭的預產日已過,最久的過了 611 天,所以這不是邊緣案例。
    """

    MATED = date(2026, 1, 1)

    def _status(self, today):
        return schedule.sow_status(sow(), [ev(1, "MT", self.MATED)], today)

    def test_before_the_due_date_there_is_no_overdue(self):
        due = self.MATED + timedelta(days=D["gestation_days"])
        assert self._status(due - timedelta(days=1))["overdue_days"] is None

    def test_on_the_due_date_it_is_not_yet_overdue(self):
        due = self.MATED + timedelta(days=D["gestation_days"])
        assert self._status(due)["overdue_days"] is None

    def test_past_the_due_date_counts_the_days(self):
        due = self.MATED + timedelta(days=D["gestation_days"])
        assert self._status(due + timedelta(days=30))["overdue_days"] == 30

    def test_farrowed_sows_are_never_overdue(self):
        """生完就沒有預產期,自然也不會逾期。"""
        events = [ev(1, "MT", self.MATED),
                  ev(1, "FW", self.MATED + timedelta(days=114))]
        s = schedule.sow_status(sow(), events, date(2026, 8, 14))
        assert s["due"] is None
        assert s.get("overdue_days") is None

    def test_label_states_both_the_days_and_the_reason(self):
        from core.labels import overdue_farrow_label
        text = overdue_farrow_label(611)
        assert "611" in text
        assert "尚無分娩記錄" in text


class TestPendingPregnancyCheck:
    """時間軸裡「還沒驗孕」的提示。

    這是原本缺席的資訊:2580 配種 143 天、從沒驗孕過,時間軸裡完全看不
    出這件事 —— 使用者得自己數有沒有一列「驗孕」才會發現。
    """

    MATED = date(2026, 1, 1)

    def test_no_check_yet_is_flagged(self):
        s = schedule.sow_status(sow(), [ev(1, "MT", self.MATED)],
                                self.MATED + timedelta(days=10))
        assert s["preg_checked"] is False

    def test_check_recorded_is_not_flagged_as_missing(self):
        events = [ev(1, "MT", self.MATED),
                  ev(1, "PD", self.MATED + timedelta(days=26), {"positive": None})]
        s = schedule.sow_status(sow(), events, self.MATED + timedelta(days=30))
        # 有記錄但結果不明,跟「根本沒驗」是不同的問題
        assert s["preg_checked"] is True
        assert s["state"] == "pregnant"    # 結果不明,不是「沒懷孕」,照樣當懷孕

    def test_no_overdue_before_the_check_window(self):
        s = schedule.sow_status(sow(), [ev(1, "MT", self.MATED)],
                                self.MATED + timedelta(days=10))
        assert s["preg_check_overdue_days"] is None

    def test_overdue_after_the_check_window(self):
        due = self.MATED + timedelta(days=D["preg_check_days"])
        s = schedule.sow_status(sow(), [ev(1, "MT", self.MATED)],
                                due + timedelta(days=15))
        assert s["preg_check_overdue_days"] == 15

    def test_confirmed_pregnant_has_no_pending_fields(self):
        """已經確認懷孕就沒有「還沒驗孕」這件事,不該出現這些欄位。"""
        events = [ev(1, "MT", self.MATED),
                  ev(1, "PD", self.MATED + timedelta(days=26), {"positive": True})]
        s = schedule.sow_status(sow(), events, self.MATED + timedelta(days=40))
        assert "preg_checked" not in s

    def test_open_state_has_no_pending_fields(self):
        """驗孕陰性後回到待配種,不該再顯示「還沒驗孕」——她已經驗過了。"""
        events = [ev(1, "MT", self.MATED),
                  ev(1, "PD", self.MATED + timedelta(days=26), {"positive": False})]
        s = schedule.sow_status(sow(), events, self.MATED + timedelta(days=40))
        assert s["state"] == "open"
        assert "preg_checked" not in s

    def test_note_names_the_real_gap_when_never_checked(self):
        from core.labels import pending_check_note
        text = pending_check_note(False, 10, 26, None)
        assert "尚未驗孕" in text
        assert "26" in text and "10" in text

    def test_note_flags_overdue_with_the_day_count(self):
        from core.labels import pending_check_note
        text = pending_check_note(False, 143, 26, 117)
        assert "117" in text

    def test_note_distinguishes_recorded_but_blank_from_never_checked(self):
        from core.labels import pending_check_note
        recorded = pending_check_note(True, 30, 26, 4)
        never = pending_check_note(False, 30, 26, 4)
        assert recorded != never
        assert "結果未填" in recorded
        assert "尚未驗孕" in never


class TestRepeatEstrusCount:
    """重發情次數。使用者明確定義:驗孕結果陰性一次就算一次重發情,
    不用推論配種批次後面有沒有接著分娩。
    """

    @staticmethod
    def litters(sow_id, alive_list, start=date(2023, 1, 1), gap=145):
        return [ev(sow_id, "FW", start + timedelta(days=gap * i), {"born_alive": a})
                for i, a in enumerate(alive_list)]

    def test_no_checks_is_zero(self):
        p = schedule.sow_performance(self.litters(1, [12, 12, 12]))
        assert p["repeat_estrus"] == 0

    def test_one_negative_check_counts_once(self):
        events = self.litters(1, [12, 12, 12]) + [
            ev(1, "PD", date(2024, 1, 1), {"positive": False})]
        assert schedule.sow_performance(events)["repeat_estrus"] == 1

    def test_positive_checks_do_not_count(self):
        events = self.litters(1, [12, 12, 12]) + [
            ev(1, "PD", date(2024, 1, 1), {"positive": True})]
        assert schedule.sow_performance(events)["repeat_estrus"] == 0

    def test_unknown_result_does_not_count(self):
        """結果沒填不等於陰性,不可以算成重發情。"""
        events = self.litters(1, [12, 12, 12]) + [
            ev(1, "PD", date(2024, 1, 1), {"positive": None})]
        assert schedule.sow_performance(events)["repeat_estrus"] == 0

    def test_multiple_negatives_across_her_life_all_count(self):
        """實測 1183:47 筆事件裡有 5 次驗孕陰性,終身總次數才有意義。"""
        events = self.litters(1, [12, 12, 12])
        for i in range(5):
            events.append(ev(1, "PD", date(2020, 1, 1) + timedelta(days=30 * i),
                             {"positive": False}))
        assert schedule.sow_performance(events)["repeat_estrus"] == 5

    def test_tiers_fewer_is_better(self):
        """越少越好 —— 跟死胎率同一個方向,不是跟總仔數那幾項一樣。"""
        assert schedule.tier_within_farm(0, list(range(10)), False) == "good"
        assert schedule.tier_within_farm(9, list(range(10)), False) == "poor"

    def test_label_and_unit(self):
        from core.labels import performance_label, performance_unit, performance_digits
        assert performance_label("repeat_estrus") == "重發情次數"
        assert performance_unit("repeat_estrus") == "次"
        assert performance_digits("repeat_estrus") == 0      # 次數是整數,不該有小數


class TestExitedSowsCountTowardTheComparisonBaseline:
    """已離群(死亡/淘汰)的母豬要算進場內比較的分母裡。

    只拿在場的當比較基準,表現最差、正是離群原因的那批一離群就從
    分母消失,活下來的人級距會愈算愈寬鬆 —— 這是使用者實際反映的
    問題:記錄死亡或淘汰之後,那頭豬就從「母豬資訊」與分析裡完全
    消失了,兩邊都要修。
    """

    @staticmethod
    def farrows(sow_id, alive_list, start=date(2023, 1, 1), gap=145):
        return [ev(sow_id, "FW", start + timedelta(days=gap * i), {"born_alive": a})
                for i, a in enumerate(alive_list)]

    def test_performance_tiers_use_exited_peers_too(self):
        """一頭表現中等的在場母豬,拿掉離群的差母豬當比較基準會被錯誤地
        評為「待改善」;含入離群母豬之後,墊底的是那些離群的,她才回到
        中段。

        數值刻意各不相同(不是一堆母豬共用同一個數字)—— 樣本裡九成
        都是同一個值時,三分位的上下界會疊在一起變成分不出高下(回
        None),不是這條測試想驗證的東西。
        """
        subject = sow(1, "0001", status="active")
        # 在場 9 頭,活仔數 11~19 各不相同;subject 是 10,墊底
        good_peers = [sow(i, f"{i:04d}", status="active") for i in range(2, 11)]
        # 離群 5 頭,活仔數 1~5,比 subject 差很多
        poor_exited = [sow(90 + i, f"9{i:03d}", status="culled") for i in range(5)]

        events = self.farrows(1, [10, 10, 10])
        for p, n in zip(good_peers, range(11, 20)):
            events += self.farrows(p["id"], [n, n, n])
        for p, n in zip(poor_exited, range(1, 6)):
            events += self.farrows(p["id"], [n, n, n])

        grouped = schedule._by_sow(events)

        without_exited = schedule.performance_with_tiers(1, [subject] + good_peers, grouped)
        with_exited = schedule.performance_with_tiers(
            1, [subject] + good_peers + poor_exited, grouped)

        tier_without = next(m["tier"] for m in without_exited["metrics"]
                            if m["key"] == "born_alive")
        tier_with = next(m["tier"] for m in with_exited["metrics"] if m["key"] == "born_alive")
        assert tier_without == "poor"      # 只跟 9 頭好母豬比,10 隻墊底
        assert tier_with != "poor"         # 加入離群的 5 頭之後,10 隻不再墊底

    def test_review_cutoff_uses_exited_peers_too(self):
        """值得檢視的百分位門檻含離群母豬,不是只看在場的。"""
        subject = sow(1, "0001", status="active")
        good_peers = [sow(i, f"{i:04d}", status="active") for i in range(2, 11)]
        poor_exited = sow(99, "9999", status="culled")

        events = self.farrows(1, [10, 10, 10])
        for p in good_peers:
            events += self.farrows(p["id"], [14, 14, 14])
        events += self.farrows(99, [4, 4, 4])

        without_exited = schedule.sows_worth_review(
            [subject] + good_peers, events, date(2026, 1, 1))
        with_exited = schedule.sows_worth_review(
            [subject] + good_peers + [poor_exited], events, date(2026, 1, 1))

        flagged_without = any(x["code"] == "low_alive"
                              for r in without_exited for x in r["reasons"])
        flagged_with = any(x["code"] == "low_alive"
                           for r in with_exited for x in r["reasons"])
        assert flagged_without           # 只跟在場比,10 隻墊底被標
        assert not flagged_with          # 加入離群的 4 隻之後,10 隻不再墊底

    def test_exited_sows_never_appear_in_the_flagged_list(self):
        """離群母豬本身不該出現在值得檢視名單裡 —— 她已經沒有「要不要
        繼續留」這個決定可做,列出來沒有意義。
        """
        exited = sow(1, "0001", status="culled")
        events = self.farrows(1, [14, 12, 10])       # 這組數字在場的話會被標
        rows = schedule.sows_worth_review([exited], events, date(2026, 1, 1))
        assert rows == []

    def test_dead_status_also_counts_as_exited(self):
        """死亡跟淘汰是同一類 —— 都不進最終名單,都算進比較基準。"""
        dead = sow(1, "0001", status="dead")
        events = self.farrows(1, [14, 12, 10])
        assert schedule.sows_worth_review([dead], events, date(2026, 1, 1)) == []


class TestTodayUsesFarmTimezone:
    """「今天」要用牧場當地日期,不是 UTC。

    正式站跑在 UTC 的機器上。取 UTC 日期的話,台灣時間半夜 12 點到早上
    8 點之間系統會以為還是昨天 —— 清晨看工作清單會看到上一週的工作,
    而豬場的班表正好從清晨開始。

    這個 bug 是在台灣時間 00:05 跑測試時才浮現的:三個用 date.today()
    寫的測試同時紅了,因為它們算的是本地日期、伺服器算的是 UTC 日期。
    """

    def test_today_matches_the_configured_timezone(self, monkeypatch):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import server

        monkeypatch.setattr(config, "FARM_TIMEZONE", "Asia/Taipei")
        assert server._today() == datetime.now(ZoneInfo("Asia/Taipei")).date()

    def test_a_different_timezone_really_changes_the_answer(self, monkeypatch):
        """證明它真的有讀設定,不是剛好跟 UTC 一樣。

        挑 UTC+14 與 UTC-11 這兩端:任何時刻這兩個時區的日期都不同,
        所以這條測試不會因為執行時間而時好時壞。
        """
        import server

        monkeypatch.setattr(config, "FARM_TIMEZONE", "Pacific/Kiritimati")   # UTC+14
        east = server._today()
        monkeypatch.setattr(config, "FARM_TIMEZONE", "Pacific/Niue")         # UTC-11
        west = server._today()
        assert east != west

    def test_broken_timezone_falls_back_instead_of_crashing(self, monkeypatch):
        """時區設錯不該讓整個服務起不來 —— 寧可日期偏一點。"""
        from datetime import datetime, timezone
        import server

        monkeypatch.setattr(config, "FARM_TIMEZONE", "Not/AZone")
        assert server._today() == datetime.now(timezone.utc).date()


class TestCustomTaskDates:
    """自訂工作的重複規則展開成這一週的日期。"""

    MON = date(2026, 8, 17)          # 週一
    SUN = date(2026, 8, 23)          # 週日

    @staticmethod
    def task(start, rule="once", name="消毒", tid=1):
        return {"id": tid, "name": name, "start_date": start, "repeat_rule": rule}

    def _dates(self, task):
        return schedule.custom_task_dates(task, self.MON, self.SUN)

    # --- once ---
    def test_once_inside_the_week(self):
        assert self._dates(self.task(date(2026, 8, 19))) == [date(2026, 8, 19)]

    def test_once_outside_the_week(self):
        assert self._dates(self.task(date(2026, 8, 10))) == []
        assert self._dates(self.task(date(2026, 9, 1))) == []

    def test_once_on_the_week_boundaries_counts(self):
        assert self._dates(self.task(self.MON)) == [self.MON]
        assert self._dates(self.task(self.SUN)) == [self.SUN]

    # --- weekly ---
    def test_weekly_repeats_on_the_same_weekday(self):
        # 起始日是 8/5(週三),這一週的週三是 8/19
        assert self._dates(self.task(date(2026, 8, 5), "weekly")) == [date(2026, 8, 19)]

    def test_weekly_starting_this_week(self):
        assert self._dates(self.task(date(2026, 8, 19), "weekly")) == [date(2026, 8, 19)]

    def test_weekly_before_the_start_date_does_not_appear(self):
        """設定「從下個月開始每週消毒」時,這個月不該冒出來。"""
        assert self._dates(self.task(date(2026, 9, 2), "weekly")) == []

    def test_weekly_appears_every_week_not_just_the_first(self):
        """跨好幾週之後仍然要出現 —— 不是只有起始那一週。"""
        task = self.task(date(2026, 1, 7), "weekly")     # 很久以前的週三
        assert schedule.custom_task_dates(task, self.MON, self.SUN) == [date(2026, 8, 19)]

    # --- monthly ---
    def test_monthly_repeats_on_the_same_day_of_month(self):
        task = self.task(date(2026, 3, 20), "monthly")
        assert schedule.custom_task_dates(task, self.MON, self.SUN) == [date(2026, 8, 20)]

    def test_monthly_outside_the_week(self):
        task = self.task(date(2026, 3, 5), "monthly")    # 每月 5 號
        assert schedule.custom_task_dates(task, self.MON, self.SUN) == []

    def test_monthly_on_the_31st_falls_back_in_short_months(self):
        """「每月 31 號」在 2 月、4 月這種月份要退到當月最後一天,
        不能因為 2 月 31 日不存在就整個炸掉。
        """
        task = self.task(date(2026, 1, 31), "monthly")
        # 2026-02 只有 28 天
        got = schedule.custom_task_dates(task, date(2026, 2, 23), date(2026, 3, 1))
        assert got == [date(2026, 2, 28)]

    def test_monthly_does_not_crash_on_any_month(self):
        task = self.task(date(2026, 1, 31), "monthly")
        for month in range(1, 13):
            start = date(2026, month, 1)
            schedule.custom_task_dates(task, start, start + timedelta(days=6))

    # --- 防呆 ---
    def test_unknown_rule_shows_nothing_rather_than_guessing(self):
        assert self._dates(self.task(self.MON, "每三天")) == []

    def test_missing_rule_defaults_to_once(self):
        task = {"id": 1, "name": "消毒", "start_date": date(2026, 8, 19)}
        assert schedule.custom_task_dates(task, self.MON, self.SUN) == [date(2026, 8, 19)]


class TestBuildCustomTasks:
    MON = date(2026, 8, 17)
    SUN = date(2026, 8, 23)

    def test_marks_which_occurrences_are_done(self):
        """重複性工作每一次發生各自標記 —— 這週消毒了、上週沒有。"""
        tasks = [{"id": 1, "name": "消毒", "start_date": date(2026, 8, 5),
                  "repeat_rule": "weekly"}]
        done = [{"task_id": 1, "due_date": date(2026, 8, 19)}]

        rows = schedule.build_custom_tasks(tasks, done, self.MON, self.SUN)
        assert len(rows) == 1
        assert rows[0]["done"] is True

    def test_a_different_weeks_completion_does_not_leak(self):
        """上週標了完成,不該讓這週看起來也完成了。"""
        tasks = [{"id": 1, "name": "消毒", "start_date": date(2026, 8, 5),
                  "repeat_rule": "weekly"}]
        done = [{"task_id": 1, "due_date": date(2026, 8, 12)}]     # 上週那次

        rows = schedule.build_custom_tasks(tasks, done, self.MON, self.SUN)
        assert rows[0]["done"] is False

    def test_sorted_by_date(self):
        tasks = [
            {"id": 1, "name": "B 工作", "start_date": date(2026, 8, 21), "repeat_rule": "once"},
            {"id": 2, "name": "A 工作", "start_date": date(2026, 8, 18), "repeat_rule": "once"},
        ]
        rows = schedule.build_custom_tasks(tasks, [], self.MON, self.SUN)
        assert [r["name"] for r in rows] == ["A 工作", "B 工作"]

    def test_no_tasks_no_crash(self):
        assert schedule.build_custom_tasks([], [], self.MON, self.SUN) == []


class TestMonthBounds:
    def test_ordinary_month(self):
        assert schedule.month_bounds(2026, 8) == (date(2026, 8, 1), date(2026, 8, 31))

    def test_december_rolls_into_next_year(self):
        assert schedule.month_bounds(2026, 12) == (date(2026, 12, 1), date(2026, 12, 31))

    def test_february_leap_year(self):
        assert schedule.month_bounds(2028, 2) == (date(2028, 2, 1), date(2028, 2, 29))

    def test_february_non_leap_year(self):
        assert schedule.month_bounds(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))


class TestMonthlyReport:
    """生產月報,12 項指標,即時重算不存快照。"""

    START = date(2026, 8, 1)
    END = date(2026, 8, 31)

    def test_no_data_returns_none_for_everything(self):
        r = schedule.monthly_report([], [], self.START, self.END)
        for key in schedule.MONTH_REPORT_METRICS:
            assert r["metrics"][key]["value"] is None, key

    def test_herd_size_is_the_average_of_start_and_end_counts(self):
        sows = [sow(1, "1183", entry_date=date(2026, 1, 1)),
               sow(2, "2580", entry_date=date(2026, 8, 20))]   # 月中才進場
        r = schedule.monthly_report(sows, [], self.START, self.END)
        # 月初 1 頭在場,月底 2 頭在場 → 平均 1.5
        assert r["herdSize"] == 1.5

    def test_entry_date_falls_back_to_earliest_event(self):
        """沒有進場記錄的母豬,用她最早一筆事件的日期當進場日 ——
        跟 importer.py 對公豬進場日的處理是同一個理由。
        """
        sows = [sow(1, "1183")]      # 沒有 entry_date
        events = [ev(1, "MT", date(2026, 1, 5))]
        r = schedule.monthly_report(sows, events, self.START, self.END)
        assert r["herdSize"] == 1.0

    def test_exited_sow_does_not_count_after_her_exit(self):
        sows = [sow(1, "1183", entry_date=date(2026, 1, 1))]
        events = [ev(1, "SAL", date(2026, 8, 5))]
        r = schedule.monthly_report(sows, events, self.START, self.END)
        # 月初(8/1)還在場,月底(8/31)已經離群 → 平均 0.5
        assert r["herdSize"] == 0.5

    def test_mating_rate(self):
        sows = [sow(i, str(i), entry_date=date(2026, 1, 1)) for i in range(1, 5)]  # 4 頭
        events = [ev(1, "MT", date(2026, 8, 5)), ev(2, "MT", date(2026, 8, 10))]
        r = schedule.monthly_report(sows, events, self.START, self.END)
        assert r["metrics"]["mating_rate"]["value"] == 50.0    # 2/4

    def test_conception_rate_only_counts_this_months_checks(self):
        events = [
            ev(1, "PD", date(2026, 8, 5), {"positive": True}),
            ev(2, "PD", date(2026, 8, 10), {"positive": False}),
            ev(3, "PD", date(2026, 7, 20), {"positive": True}),    # 上個月,不算
        ]
        r = schedule.monthly_report([], events, self.START, self.END)
        assert r["metrics"]["conception_rate"]["n"] == 2
        assert r["metrics"]["conception_rate"]["value"] == 50.0

    def test_conception_rate_ignores_unrecorded_results(self):
        events = [ev(1, "PD", date(2026, 8, 5), {})]
        r = schedule.monthly_report([], events, self.START, self.END)
        assert r["metrics"]["conception_rate"]["n"] == 0

    def test_farrowing_rate_denominator_is_matings_from_gestation_days_earlier(self):
        """這正是這個指標最容易算錯的地方:分母是回推 gestation_days 天
        的配種,不是當月配種 —— 配種量一波動,拿當月配種當分母會讓
        分娩率出現跟真實表現無關的假跳動。
        """
        mated_for_this_month = self.START - timedelta(days=114)
        events = [
            ev(1, "MT", mated_for_this_month),
            ev(2, "MT", mated_for_this_month + timedelta(days=5)),
            ev(1, "FW", date(2026, 8, 10)),
            # 當月配種,不該算進分娩率的分母 —— 她要到年底才可能分娩
            ev(3, "MT", date(2026, 8, 15)),
        ]
        r = schedule.monthly_report([], events, self.START, self.END)
        assert r["metrics"]["farrowing_rate"]["n"] == 2        # 只有回推那兩筆
        assert r["metrics"]["farrowing_rate"]["value"] == 50.0  # 1 窩 / 2 筆配種

    def test_litter_metrics_ignore_farrows_missing_alive_or_stillborn(self):
        events = [
            ev(1, "FW", date(2026, 8, 5), {"born_alive": 12, "stillborn": 1, "mummified": 1}),
            ev(2, "FW", date(2026, 8, 10), {"born_alive": 10}),    # 缺死胎欄位,不計入
        ]
        r = schedule.monthly_report([], events, self.START, self.END)
        m = r["metrics"]
        assert m["total_born_per_litter"]["n"] == 1
        assert m["total_born_per_litter"]["value"] == 14
        assert m["born_alive_per_litter"]["value"] == 12
        assert m["mummification_rate"]["value"] == pytest.approx(1 / 14 * 100)
        assert m["stillbirth_rate"]["value"] == pytest.approx(1 / 14 * 100)

    def test_weaned_per_litter(self):
        events = [
            ev(1, "WN", date(2026, 8, 5), {"weaned": 11}),
            ev(2, "WN", date(2026, 8, 10), {"weaned": 9}),
        ]
        r = schedule.monthly_report([], events, self.START, self.END)
        assert r["metrics"]["weaned_per_litter"]["value"] == 10.0

    def test_lactation_days_pairs_each_weaning_with_her_preceding_farrow(self):
        events = [
            ev(1, "FW", date(2026, 2, 1)),
            ev(1, "WN", date(2026, 8, 5)),      # 配對到 2/1 那胎,不是隨便算
        ]
        r = schedule.monthly_report([], events, self.START, self.END)
        assert r["metrics"]["lactation_days"]["value"] == (date(2026, 8, 5) - date(2026, 2, 1)).days

    def test_weaning_without_any_prior_farrow_is_not_counted(self):
        events = [ev(1, "WN", date(2026, 8, 5), {"weaned": 11})]
        r = schedule.monthly_report([], events, self.START, self.END)
        assert r["metrics"]["lactation_days"]["n"] == 0
        assert r["metrics"]["lactation_days"]["value"] is None

    def test_psy_is_annualized_not_a_bare_monthly_count(self):
        sows = [sow(1, "1183", entry_date=date(2026, 1, 1))]     # 1 頭母豬全月在場
        events = [ev(1, "WN", date(2026, 8, 5), {"weaned": 10})]
        r = schedule.monthly_report(sows, events, self.START, self.END)
        # (10 頭離乳 / 1 頭母豬)*(365.25 / 31 天)—— 這裡只驗證公式,
        # 不代表這是合理範圍,單月樣本本來就會被年化放大。
        expected = 10 / 1 * (365.25 / 31)
        assert r["metrics"]["psy"]["value"] == pytest.approx(expected)

    def test_cull_rate_is_annualized(self):
        # 淘汰的那頭本身月中才離群,月初、月底平均下來只算 1.5 頭,不是
        # 乾淨的 2 —— 她整個月只有一部分時間真的在群內,這正是「淘汰率」
        # 這種指標會遇到的情形,不是算錯。
        sows = [sow(i, str(i), entry_date=date(2026, 1, 1)) for i in range(1, 3)]  # 2 頭
        events = [ev(1, "SAL", date(2026, 8, 5))]
        r = schedule.monthly_report(sows, events, self.START, self.END)
        expected = 1 / 1.5 * 100 * (365.25 / 31)
        assert r["metrics"]["cull_rate"]["value"] == pytest.approx(expected)

    def test_mortality_rate_is_annualized(self):
        sows = [sow(i, str(i), entry_date=date(2026, 1, 1)) for i in range(1, 3)]
        events = [ev(1, "DTH", date(2026, 8, 5))]
        r = schedule.monthly_report(sows, events, self.START, self.END)
        expected = 1 / 1.5 * 100 * (365.25 / 31)     # 同上,離群那頭只算半個月
        assert r["metrics"]["mortality_rate"]["value"] == pytest.approx(expected)

    def test_excluded_events_are_ignored(self):
        sows = [sow(1, "1183", entry_date=date(2026, 1, 1))]
        events = [ev(1, "MT", date(2026, 8, 5), excluded=True)]
        r = schedule.monthly_report(sows, events, self.START, self.END)
        assert r["metrics"]["mating_rate"]["value"] == 0.0

    def test_events_outside_the_period_do_not_count(self):
        sows = [sow(1, "1183", entry_date=date(2026, 1, 1))]
        events = [ev(1, "MT", date(2026, 7, 31)), ev(1, "MT", date(2026, 9, 1))]
        r = schedule.monthly_report(sows, events, self.START, self.END)
        assert r["metrics"]["mating_rate"]["value"] == 0.0

    def test_no_herd_means_annualized_metrics_are_none_not_a_crash(self):
        """一頭母豬都不在場的月份 —— 用她本來就沒有的分母算年化指標
        會除以零,必須回 None 而不是炸掉或宣稱 0。
        """
        r = schedule.monthly_report([], [], self.START, self.END)
        assert r["metrics"]["psy"]["value"] is None
        assert r["metrics"]["cull_rate"]["value"] is None
        assert r["metrics"]["mortality_rate"]["value"] is None
