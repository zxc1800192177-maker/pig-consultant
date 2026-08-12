"""工作推算。

「今天」一律由測試指定,模組內不呼叫 date.today() —— 這是 schedule.py
放頂層(而非 core/)之後仍要維持確定性的方式。

預設間隔量自這個牧場的真實資料(見 schedule.DEFAULTS 的註解),所以測試
用的日期也照那些間隔算,不是隨便挑的數字。
"""

from datetime import date, timedelta

import pytest

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
    """配種之後會排出四件事:驗孕、移入產房、催產、分娩。"""

    MATED = date(2026, 2, 3)

    @pytest.fixture
    def tasks(self):
        return tasks_for_sow(sow(), [ev(1, "MT", self.MATED)], D)

    def test_all_four(self, tasks):
        assert kinds(tasks) == {CHECK_DUE, MOVE_IN, INDUCE, FARROW_DUE}

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


class TestTasksAfterFarrowing:
    def test_weaning_is_due(self):
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", date(2025, 10, 13)),
            ev(1, "FW", date(2026, 2, 4)),
        ], D)
        assert kinds(tasks) == {WEAN_DUE}
        assert due_of(tasks, WEAN_DUE) == date(2026, 2, 4) + timedelta(days=22)

    def test_nothing_left_after_weaning_except_mating(self):
        tasks = tasks_for_sow(sow(), [
            ev(1, "MT", date(2025, 10, 13)),
            ev(1, "FW", date(2026, 2, 4)),
            ev(1, "WN", date(2026, 2, 26)),
        ], D)
        assert kinds(tasks) == {MATE_DUE}
        assert due_of(tasks, MATE_DUE) == date(2026, 2, 26) + timedelta(days=5)


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
    """逐欄位追蹤,所以要能回答「還剩哪幾欄」而不只是「不夠」。"""

    PENS = [{"id": 1, "name": "A-01"}, {"id": 2, "name": "A-02"}]

    def test_reports_which_pens_are_free(self):
        r = pen_pressure([sow(1, "1183", pen_id=1)], [], self.PENS, date(2026, 3, 1))
        assert r["occupied"] == 1
        assert [p["name"] for p in r["free"]] == ["A-02"]

    def test_counts_sows_due_to_move_in(self):
        mated = date(2026, 3, 1) - timedelta(days=114 - 14)
        r = pen_pressure([sow(1, "1183"), sow(2, "2580")],
                         [ev(1, "MT", mated), ev(2, "MT", mated)],
                         self.PENS, date(2026, 3, 1))
        assert r["incoming"] == 2
        assert r["short_by"] == 0

    def test_short_when_more_coming_than_free(self):
        mated = date(2026, 3, 1) - timedelta(days=114 - 14)
        sows = [sow(i, str(i)) for i in range(1, 4)]
        events = [ev(i, "MT", mated) for i in range(1, 4)]
        r = pen_pressure(sows, events, self.PENS, date(2026, 3, 1))
        assert r["incoming"] == 3
        assert r["short_by"] == 1


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
        r = pen_pressure([sow()], events, [{"id": 1, "name": "A-01"}], date(2026, 3, 1))
        assert r["incoming"] == 0
