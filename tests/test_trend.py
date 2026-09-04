"""生產性能趨勢報告。

這個模組最容易出錯的地方都是**分母**,所以測試也集中在那裡:

1. 分母是「發情次數」不是「配種筆數」。一次發情連配 2–3 天,拿筆數當分母
   會把配種率灌水成兩倍、分娩率砍成一半。
2. 受胎率不能用「陽性/驗孕總數」。這個場只登記驗孕陰性,那樣算是 0%。
3. 離乳前死亡率要按窩配對,不能拿當期活仔硬除 —— 分子分母差三週。
4. 分娩率的分母要回推約懷孕天數,不是當期配種。

還有一條貫串全部的:**沒有記錄就是 None,不補 0。** 0 是「這期真的一頭都
沒死」,None 是「這期沒有人記」,混在一起會讓趨勢圖憑空多出一個谷底。
"""

from datetime import date, timedelta

import pytest

import trend


def ev(sow_id, code, day, **detail):
    return {"id": abs(hash((sow_id, code, day))) % 10**6, "sow_id": sow_id,
            "event_type": code, "event_date": day, "detail": detail,
            "excluded": False}


def sow(sow_id, entry="2020-01-01"):
    return {"id": sow_id, "ear_tag": str(sow_id), "status": "active",
            "entry_date": date.fromisoformat(entry)}


def d(text):
    return date.fromisoformat(text)


def values(sows, events, spans, settings=None):
    """把報告攤平成 {指標: [每期的值]},斷言才讀得懂。"""
    rep = trend.trend_report(sows, events, spans, settings)
    return {r["key"]: r["values"] for s in rep["sections"] for r in s["rows"]}


class TestPeriods:
    def test_weeks_start_on_the_configured_day(self):
        """週界要跟工作清單同一天。不一樣的話同一批母豬會被切到兩欄,
        牧場主拿兩個畫面對不起來。"""
        # week_start_day = 3 是禮拜四
        spans = trend.periods(d("2026-08-31"), d("2026-09-10"), "week",
                              {"week_start_day": 3})
        # 第一格往回補成完整的一週(8/27 是禮拜四),不是從 8/31 切一半 ——
        # 每一欄長度一樣,數字才比得起來。
        assert spans[0].start == d("2026-08-27")
        assert all(p.start.weekday() == 3 for p in spans)

    def test_months_quarters_years(self):
        assert [p.label for p in trend.periods(d("2026-01-15"), d("2026-03-02"), "month")] \
            == ["2026/01", "2026/02", "2026/03"]
        assert [p.label for p in trend.periods(d("2026-01-01"), d("2026-07-01"), "quarter")] \
            == ["2026 Q1", "2026 Q2", "2026 Q3"]
        assert [p.label for p in trend.periods(d("2024-06-01"), d("2026-02-01"), "year")] \
            == ["2024 年", "2025 年", "2026 年"]

    def test_the_last_period_stops_at_the_end_date(self):
        """今年那一格不可以算成整年。

        算成 365 天的話,年化的淘汰率、更新率、非生產天數會全部被稀釋成
        三分之二 —— 今年憑空看起來變好,而那是最常被拿來看的一格。
        """
        spans = trend.periods(d("2026-01-01"), d("2026-08-31"), "year")
        assert spans[-1].end == d("2026-08-31")
        spans = trend.periods(d("2026-01-01"), d("2026-08-15"), "month")
        assert spans[-1].end == d("2026-08-15")

    def test_an_unknown_grain_is_refused(self):
        with pytest.raises(ValueError, match="期別"):
            trend.periods(d("2026-01-01"), d("2026-02-01"), "fortnight")


class TestHeatsNotServices:
    """一次發情連配 2–3 天是**一次**配種行為,不是三次。"""

    def test_consecutive_days_are_one_heat(self):
        days = [d("2026-01-05"), d("2026-01-06"), d("2026-01-07")]
        assert trend._heats(days, 5) == [days]

    def test_a_new_heat_three_weeks_later_is_separate(self):
        days = [d("2026-01-05"), d("2026-01-06"), d("2026-01-26")]
        assert [len(g) for g in trend._heats(days, 5)] == [2, 1]

    def test_the_report_counts_both(self):
        s = [sow(1)]
        events = [ev(1, "MT", d("2026-03-05")), ev(1, "MT", d("2026-03-06")),
                  ev(1, "MT", d("2026-03-07"))]
        got = values(s, events, trend.periods(d("2026-03-01"), d("2026-03-31"), "month"))
        assert got["services"] == [3]
        assert got["heats"] == [1]
        assert got["services_per_heat"] == [3.0]


class TestConceptionRate:
    """**不能**用「陽性/驗孕總數」。這個場只在驗孕陰性時登記,那樣算是 0%。"""

    def _one_heat(self, extra=(), horizon="2026-12-31"):
        events = [ev(1, "MT", d("2026-03-05")), ev(1, "MT", d("2026-03-06"))]
        events += list(extra)
        # 撐開資料範圍,讓那次配種過得了「判斷期」
        events.append(ev(2, "MT", d(horizon)))
        spans = trend.periods(d("2026-03-01"), d("2026-03-31"), "month")
        return values([sow(1), sow(2)], events, spans)

    def test_no_check_and_no_repeat_means_she_held(self):
        """沒登記就代表有懷孕(使用者說明)。她後來也真的分娩了。"""
        got = self._one_heat([ev(1, "FW", d("2026-06-27"), born_alive=12)])
        assert got["conception_rate"] == [100.0]

    def test_an_explicit_negative_check_counts_as_failed(self):
        got = self._one_heat([ev(1, "PD", d("2026-03-25"), positive=False)])
        assert got["conception_rate"] == [0.0]

    def test_being_served_again_means_the_first_one_failed(self):
        """配種後隔 21 天又配種 = 重發情 = 第一次是陰性(使用者說明)。

        重發情那一次刻意放到下個月:它自己也是一次可判定的發情,留在三月
        的話這條測試會同時量到兩次發情的結果,測不出「第一次是陰性」。
        """
        got = self._one_heat([ev(1, "MT", d("2026-04-01"))])
        assert got["conception_rate"] == [0.0]

    def test_an_abortion_still_counts_as_conceived(self):
        """流產代表**有**受胎,只是沒生下來 —— 算進受胎率,不算進分娩率。"""
        got = self._one_heat([ev(1, "AB", d("2026-05-01"))])
        assert got["conception_rate"] == [100.0]

    def test_a_heat_too_recent_to_judge_is_left_out(self):
        """最近的配種還沒機會顯示重發情。算進分母的話最後一期永遠 100%。"""
        events = [ev(1, "MT", d("2026-03-30"))]
        spans = trend.periods(d("2026-03-01"), d("2026-03-31"), "month")
        got = values([sow(1)], events, spans)
        assert got.get("conception_rate", [None]) == [None]
        assert got.get("judged_heats", [None]) == [None]

    def test_no_bad_news_counts_as_conceived_before_she_farrows(self):
        """**這是一個修過的真實 bug。**

        分娩是受胎的正面證據,但它要等 114 天才出現;重發情、驗孕陰性這些
        負面證據 21–45 天就看得到。所以「只有分娩才算受胎」的話,最近三、
        四個月判得出來的全是失敗的 —— 實測讓 2026/05 的受胎率變成 4.2%,
        而同期歷史水準是七成多。

        依使用者說明的規則,過了反證該出現的期間還是沒有反證,就是受胎。
        """
        events = [ev(1, "MT", d("2026-03-05")), ev(2, "MT", d("2026-06-30"))]
        spans = trend.periods(d("2026-03-01"), d("2026-03-31"), "month")
        got = values([sow(1), sow(2)], events, spans)
        assert got["conception_rate"] == [100.0]
        assert got["judged_heats"] == [1]

    def test_she_who_left_before_the_answer_showed_is_not_counted(self):
        """還沒看得出結果就被淘汰的,算受胎會虛報成績、算陰性會冤枉她。"""
        events = [ev(1, "MT", d("2026-03-05")),
                  ev(1, trend.CULL, d("2026-03-20"), reason="肢蹄"),
                  ev(2, "MT", d("2026-06-30"))]
        spans = trend.periods(d("2026-03-01"), d("2026-03-31"), "month")
        got = values([sow(1), sow(2)], events, spans)
        assert got.get("conception_rate", [None]) == [None]
        assert got.get("judged_heats", [None]) == [None]

    def test_a_period_of_only_negative_checks_is_not_zero_percent(self):
        """真實資料裡 2022–2026 的驗孕記錄全是陰性,而同期每年分娩八百多窩。
        照「陽性/總數」算會得到 0%,那是把記錄習慣讀成了生產結果。
        """
        events = [
            ev(1, "MT", d("2026-03-05")), ev(1, "FW", d("2026-06-27"), born_alive=11),
            ev(2, "MT", d("2026-03-05")), ev(2, "PD", d("2026-03-23"), positive=False),
            ev(3, "MT", d("2026-12-31")),
        ]
        spans = trend.periods(d("2026-03-01"), d("2026-03-31"), "month")
        got = values([sow(1), sow(2), sow(3)], events, spans)
        assert got["conception_rate"] == [50.0], "一頭受胎一頭沒有,就是五成"


class TestFarrowingRate:
    def test_the_denominator_looks_back_a_gestation(self):
        """當期分娩對應的是約 114 天前的配種,不是當期配種。

        拿當期配種當分母,配種量一波動分娩率就會出現跟真實表現無關的
        假跳動 —— 而那正是牧場主用來判斷繁殖成績的數字。
        """
        cfg = {"gestation_days": 114}
        events = [
            # 三月配了兩次發情,七月分娩兩窩
            ev(1, "MT", d("2026-03-05")), ev(2, "MT", d("2026-03-05")),
            ev(1, "FW", d("2026-06-27"), born_alive=12),
            ev(2, "FW", d("2026-06-28"), born_alive=10),
            # 六月又配了二十次 —— 這些跟六月的分娩率無關
            *[ev(10 + i, "MT", d("2026-06-10")) for i in range(20)],
        ]
        sows = [sow(1), sow(2)] + [sow(10 + i) for i in range(20)]
        spans = trend.periods(d("2026-06-01"), d("2026-06-30"), "month")
        got = values(sows, events, spans, cfg)
        assert got["farrowing_rate"] == [100.0], "兩窩對兩次三月的配種"


class TestPreweaningMortality:
    def test_it_pairs_each_weaning_with_its_own_litter(self):
        """不能拿當期活仔硬除 —— 分子分母差了三週,週報會錯開一整批。"""
        events = [
            ev(1, "FW", d("2026-05-04"), born_alive=12),
            ev(1, "WN", d("2026-05-25"), weaned=9),
            # 同一期又有一窩剛分娩、還沒離乳 —— 它的活仔數不該進分母
            ev(2, "FW", d("2026-05-28"), born_alive=14),
        ]
        spans = trend.periods(d("2026-05-01"), d("2026-05-31"), "month")
        got = values([sow(1), sow(2)], events, spans)
        assert got["preweaning_mortality"] == [25.0], "(12-9)/12,不是 (26-9)/26"

    def test_recorded_deaths_are_a_separate_number(self):
        """逐筆記錄到的仔豬死亡跟離乳前死亡率是兩件事,而且差很多。

        真實資料 2025 年:離乳前死亡率 23.6%,逐筆記錄只有 7.2% —— 三分之二
        的損失沒有被記下來。兩個都要顯示,那個落差本身就是要看見的事。
        """
        events = [
            ev(1, "FW", d("2026-05-04"), born_alive=12),
            ev(1, "PL", d("2026-05-06"), count=1, reason="母豬壓死"),
            ev(1, "WN", d("2026-05-25"), weaned=9),
        ]
        spans = trend.periods(d("2026-05-01"), d("2026-05-31"), "month")
        got = values([sow(1)], events, spans)
        assert got["preweaning_mortality"] == [25.0]
        assert got["piglet_deaths"] == [1]
        assert got["piglet_death_pct"] == [pytest.approx(100 / 12)]


class TestPigletDeathAges:
    def test_deaths_are_bucketed_by_age(self):
        events = [
            ev(1, "FW", d("2026-05-04"), born_alive=12),
            ev(1, "PL", d("2026-05-04"), count=2, reason="母豬壓死"),   # 0 日齡
            ev(1, "PL", d("2026-05-09"), count=1, reason="下痢"),       # 5 日齡
            ev(1, "PL", d("2026-05-20"), count=1, reason="體弱"),       # 16 日齡
        ]
        spans = trend.periods(d("2026-05-01"), d("2026-05-31"), "month")
        got = values([sow(1)], events, spans)
        assert got["deaths_under_2d"] == [2]
        assert got["deaths_2_8d"] == [1]
        assert got["deaths_over_8d"] == [1]
        assert got["crushed_pct"] == [50.0]


class TestNoRecordIsNotZero:
    def test_a_metric_with_no_records_is_none(self):
        """0 是「真的一頭都沒死」,None 是「沒有人記」。混在一起會讓
        趨勢圖憑空多出一個谷底(憲法第三條)。"""
        events = [ev(1, "MT", d("2026-03-05"))]
        spans = trend.periods(d("2026-03-01"), d("2026-03-31"), "month")
        rep = trend.trend_report([sow(1)], events, spans)
        keys = {r["key"] for s in rep["sections"] for r in s["rows"]}
        assert "services" in keys
        assert "piglet_deaths" not in keys, "整排都沒資料的指標不該出現"

    def test_excluded_events_do_not_count(self):
        """匯入時被判為離群、標記不納入統計的記錄不進報告。"""
        bad = ev(1, "FW", d("2026-05-04"), born_alive=56)
        bad["excluded"] = True
        spans = trend.periods(d("2026-05-01"), d("2026-05-31"), "month")
        rep = trend.trend_report([sow(1)], [bad], spans)
        keys = {r["key"] for s in rep["sections"] for r in s["rows"]}
        assert "born_alive" not in keys


class TestChange:
    def test_direction_depends_on_the_metric(self):
        """死胎率下降是好事,活仔數下降是壞事 —— 同樣是負數,意思相反。"""
        events = [
            ev(1, "FW", d("2026-01-10"), born_alive=10, stillborn=2),
            ev(2, "FW", d("2026-02-10"), born_alive=12, stillborn=1),
        ]
        spans = trend.periods(d("2026-01-01"), d("2026-02-28"), "month")
        rep = trend.trend_report([sow(1), sow(2)], events, spans)
        rows = {r["key"]: r for s in rep["sections"] for r in s["rows"]}
        assert rows["alive_per_litter"]["change"]["improved"] is True
        assert rows["stillborn_pct"]["change"]["improved"] is True

    def test_a_missing_end_gives_no_change(self):
        """一端沒有記錄就不算 —— 拿「沒記」去減「有記」會算出一個看起來
        很嚴重、其實不存在的變化。"""
        events = [ev(1, "FW", d("2026-01-10"), born_alive=10)]
        spans = trend.periods(d("2026-01-01"), d("2026-02-28"), "month")
        rep = trend.trend_report([sow(1)], events, spans)
        rows = {r["key"]: r for s in rep["sections"] for r in s["rows"]}
        assert rows["alive_per_litter"]["change"] is None

    def test_counts_have_no_direction(self):
        """配種筆數變多不代表變好或變壞,不該標顏色。"""
        assert trend.METRICS["services"].better is None
        assert trend.METRICS["psy"].better == trend.HIGH
        assert trend.METRICS["stillborn_pct"].better == trend.LOW


class TestComparingArbitraryPeriods:
    def test_periods_do_not_have_to_be_contiguous(self):
        """「2024 全年 vs 2025 全年 vs 今年至今」跟「連續 12 個月」是同一個
        功能,差別只在傳進來的清單長什麼樣。"""
        events = [ev(1, "FW", d("2024-05-04"), born_alive=10),
                  ev(1, "FW", d("2026-05-04"), born_alive=12)]
        spans = [trend.Period("a", "2024", d("2024-01-01"), d("2024-12-31")),
                 trend.Period("b", "今年", d("2026-01-01"), d("2026-08-31"))]
        got = values([sow(1)], events, spans)
        assert got["alive_per_litter"] == [10.0, 12.0]


def rows(sows, events, spans, settings=None):
    """把報告攤平成 {指標: 整列},要看 total/avg/tiers 時用這個。"""
    rep = trend.trend_report(sows, events, spans, settings)
    return {r["key"]: r for s in rep["sections"] for r in s["rows"]}


def months(*keys):
    """給幾個 "YYYY-MM",切成對應的整月期間。"""
    out = []
    for key in keys:
        start = d(key + "-01")
        out.append(trend.Period(key, key, start,
                                trend._add_month(start, 1) - timedelta(days=1)))
    return out


class TestSummaryColumns:
    """右側的「總計 / 平均」兩欄(對照 PigCHAMP 原版報告的同兩欄)。"""

    def test_counts_add_up_to_the_total_shown(self):
        """總計必須等於畫面上那幾欄相加 —— 牧場主會自己加一遍。"""
        events = [ev(1, "FW", d("2026-01-05"), born_alive=10),
                  ev(2, "FW", d("2026-01-20"), born_alive=11),
                  ev(3, "FW", d("2026-02-10"), born_alive=12)]
        got = rows([sow(1), sow(2), sow(3)], events, months("2026-01", "2026-02"))
        assert got["litters"]["values"] == [2, 1]
        assert got["litters"]["total"] == 3
        assert got["litters"]["avg"] == 1.5          # 3 ÷ 2 期

    def test_a_rate_is_pooled_not_averaged(self):
        """比率的平均要把整段當一期重算,不是把各期的比率平均。

        一月 2 窩、二月 1 窩,活仔 10+11 與 30。各期窩均是 10.5 與 30,
        算術平均 20.25;但整段其實是 51÷3 = 17.0。窩數差很多的時候,
        算術平均等於讓那 1 窩跟 2 窩一樣重。
        """
        events = [ev(1, "FW", d("2026-01-05"), born_alive=10),
                  ev(2, "FW", d("2026-01-20"), born_alive=11),
                  ev(3, "FW", d("2026-02-10"), born_alive=30)]
        got = rows([sow(1), sow(2), sow(3)], events, months("2026-01", "2026-02"))
        assert got["alive_per_litter"]["values"] == [10.5, 30.0]
        assert got["alive_per_litter"]["avg"] == 17.0
        assert got["alive_per_litter"]["total"] is None   # 比率相加沒有意義

    def test_distinct_counts_are_not_summed(self):
        """同一頭公豬跨兩期用,總計是 1 頭不是 2 頭。"""
        events = [ev(1, "MT", d("2026-01-05"), boar_tag="B1"),
                  ev(2, "MT", d("2026-02-05"), boar_tag="B1")]
        got = rows([sow(1), sow(2)], events, months("2026-01", "2026-02"))
        assert got["boars_used"]["values"] == [1, 1]
        assert got["boars_used"]["total"] == 1
        assert got["boars_used"]["avg"] == 1.0

    def test_a_stock_level_gets_no_total(self):
        """期末在養是某一天的存量,各期相加是個沒有意義的數字。"""
        events = [ev(1, "FW", d("2026-01-05"), born_alive=10)]
        got = rows([sow(1), sow(2)], events, months("2026-01", "2026-02"))
        assert got["ending_herd"]["total"] is None
        assert got["ending_herd"]["avg"] is not None

    def test_non_contiguous_periods_get_no_summary(self):
        """2023 跟 2025 並排比較時,「整段重算」會把沒被選到的 2024 也算
        進去 —— 那不是使用者要的任何一個數字,所以整個不給。"""
        events = [ev(1, "FW", d("2024-05-04"), born_alive=10),
                  ev(1, "FW", d("2026-05-04"), born_alive=12)]
        spans = [trend.Period("a", "2024", d("2024-01-01"), d("2024-12-31")),
                 trend.Period("b", "今年", d("2026-01-01"), d("2026-08-31"))]
        rep = trend.trend_report([sow(1)], events, spans)
        assert rep["hasSummary"] is False
        row = {r["key"]: r for s in rep["sections"] for r in s["rows"]}["litters"]
        assert row["total"] is None and row["avg"] is None

    def test_contiguous_periods_do_get_a_summary(self):
        events = [ev(1, "FW", d("2026-01-05"), born_alive=10)]
        rep = trend.trend_report([sow(1)], events, months("2026-01", "2026-02"))
        assert rep["hasSummary"] is True


class TestNationalNormTiers:
    """逐格對照全國常模的紅綠級距。"""

    def test_a_bad_value_is_marked_poor(self):
        """離乳前死亡率 30% 落在全國最差一段(第 90 百分位是 24.38%)。"""
        assert trend._tier(30.0, trend._norm("preweaning_mortality")) == "poor"

    def test_a_good_value_is_marked_good(self):
        """8% 優於全國前 10%(8.77%)。"""
        assert trend._tier(8.0, trend._norm("preweaning_mortality")) == "good"

    def test_the_middle_half_is_left_uncoloured(self):
        """全國中間 50% 不上色 —— 55 個指標滿頁紅綠等於沒有重點。"""
        assert trend._tier(17.0, trend._norm("preweaning_mortality")) == "mid"

    def test_direction_is_taken_from_the_metric_not_guessed(self):
        """越高越好的指標,高值才是 good。"""
        assert trend._tier(26.0, trend._norm("psy")) == "good"
        assert trend._tier(17.0, trend._norm("psy")) == "poor"

    def test_no_norm_means_no_colour_not_a_middle_grade(self):
        """比不出來跟「比得出來但普通」在畫面上必須是兩種樣子。"""
        assert trend._norm("services") is None
        assert trend._tier(123.0, trend._norm("services")) is None

    def test_missing_value_is_never_coloured(self):
        assert trend._tier(None, trend._norm("psy")) is None

    def test_every_mapped_metric_agrees_with_the_norm_on_direction(self):
        """**這條是護欄。** 本系統說「越低越好」而常模說「越高越好」的
        指標如果被加進 NORM_KEYS,整排顏色會反過來 —— 而且反得很安靜,
        畫面上看起來一切正常。日後有人加對照時,這裡要先炸。
        """
        for metric_key, norm_key in trend.NORM_KEYS.items():
            metric = trend.METRICS[metric_key]
            assert metric.better is not None, (
                f"{metric_key} 本身沒有好壞方向,不該對照常模上色")
            norm = trend._norm(metric_key)
            assert norm["higherBetter"] == (metric.better == trend.HIGH), (
                f"{metric_key} 與常模 {norm_key} 的好壞方向相反")

    def test_the_norm_source_is_reported(self):
        """畫面上要講清楚顏色的依據是哪一年、幾場的常模。"""
        rep = trend.trend_report([sow(1)], [], months("2026-01"))
        assert rep["normSource"]["year"] == 2025
        assert rep["normSource"]["farms"] > 0
