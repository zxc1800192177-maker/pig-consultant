"""生產性能趨勢報告:同一組指標,切成一段一段的期間並排比較。

跟 `schedule.monthly_report` 的差別不是指標多寡,是**問題不同**:
月報回答「這個月做得怎樣」,趨勢報告回答「跟上一期比、跟去年比,
哪裡在變好、哪裡在變壞」。所以這裡的輸出一定是**多個期間**,
而且每一項都帶著跟前一期的差。

形狀對照使用者手上的 PigCHAMP「生產性能趨勢分析報告」:五個區段
(配種/分娩/仔豬死亡/離乳/在養),期別可切週、月、季、年。

## 這裡刻意跟 PigCHAMP 不一樣的地方

**分母是「發情次數」而不是「配種筆數」。** 一頭母豬一次發情連配 2–3 天,
每天各記一筆(這個場實測平均 1.95 筆/次發情)。拿筆數當分母的話,配種率
會被灌水成兩倍、分娩率會被砍到剩一半 —— 而那兩個數字正是牧場主用來判斷
繁殖成績的。`_heats()` 把 mating_series_days 內的連續配種併成一次。

**沒有資料的欄位不出現。** 窩重、仔豬離乳重、校正離乳重、配種類型、
仔豬性別在這個系統裡沒有記錄的地方,PigCHAMP 那份報告裡也整欄是空的。
印一排空格只會讓人以為是自己漏記了(憲法第三條)。

不碰 HTTP、不碰資料庫。跟 schedule.py 同樣的理由放在頂層而不是 core/:
整份都是日期運算,而 core/ 不得匯入 datetime(test_core_purity.py 強制)。
`today` 之類的當下時間一律由呼叫端傳入,測試才固定得住。
"""

import bisect
import collections
from datetime import date, timedelta
from typing import Dict, Iterable, List, NamedTuple, Optional

from schedule import (
    ABORT, CULL, DEATH, EXIT_EVENTS, FARROW, MATE, PIGLET_LOSS, PREG_CHECK,
    WEAN, settings_with_defaults, _by_sow,
)

# 期別。週對這個場最有意義(週批生產,一週一批同步做同一件事),
# 但一年 52 欄在手機上讀不完,所以預設用月,要細看再切週。
WEEK, MONTH, QUARTER, YEAR = "week", "month", "quarter", "year"
GRAINS = (WEEK, MONTH, QUARTER, YEAR)

DAYS_PER_YEAR = 365.25

# 判定一次配種有沒有受胎,最少要等幾天。
#
# 這個場**只在驗孕陰性時才登記**(使用者說明),所以「沒有驗孕記錄」代表
# 她受胎了,不是資料缺漏。判斷陰性的第二條線索是重發情:沒分娩就又配了
# 一次,表示前一次是陰性。實測 1,566 組「沒分娩就再配」的間隔,集中在
# 15–27 天(602 組,一個發情週期)與 35–48 天(324 組,兩個週期)。
#
# 取 28 天:足夠讓第一個發情週期的重配種出現。比這更新的配種還沒有機會
# 顯示出重發情,算進分母的話最近幾期的受胎率會永遠是 100%。
CONCEPTION_JUDGE_DAYS = 28

# 明確的驗孕陰性要落在配種後幾天內才算是在講這一次配種。
NEGATIVE_CHECK_WINDOW = 45


class Period(NamedTuple):
    key: str            # 排序與比對用的穩定值,例如 "2026-W31"
    label: str          # 畫面上的字,例如 "08/27–09/02"
    start: date
    end: date


class Metric(NamedTuple):
    """一個指標。

    `better` 說明哪個方向算進步 —— 「死胎率下降」跟「活仔數下降」是完全
    相反的兩件事,畫面上要標對顏色就得知道這件事,不能讓前端自己猜。
    """
    key: str
    label: str
    unit: str
    digits: int
    better: Optional[str]      # "high" / "low" / None(純數量,無所謂好壞)


class Section(NamedTuple):
    key: str
    label: str
    metrics: List[Metric]


HIGH, LOW = "high", "low"

SECTIONS: List[Section] = [
    Section("breeding", "配種", [
        Metric("services", "配種筆數", "筆", 0, None),
        Metric("heats", "配種頭次", "次", 0, None),
        Metric("services_per_heat", "每次發情配種次數", "次", 2, None),
        Metric("first_services", "初配頭數", "頭", 0, None),
        Metric("gilt_first_services", "新女豬初配", "頭", 0, None),
        Metric("wean_to_service_days", "離乳到配種間隔", "天", 1, LOW),
        Metric("serviced_within_7d_pct", "離乳後 7 天內配種", "%", 1, HIGH),
        Metric("repeat_heats", "重發情配種", "次", 0, LOW),
        Metric("repeat_pct", "重發情比率", "%", 1, LOW),
        Metric("boars_used", "使用公豬數", "頭", 0, None),
        Metric("conception_rate", "受胎率", "%", 1, HIGH),
        Metric("judged_heats", "受胎率可判定次數", "次", 0, None),
        Metric("avg_service_parity", "平均配種胎次", "胎", 1, None),
    ]),
    Section("farrowing", "分娩", [
        Metric("litters", "分娩窩數", "窩", 0, None),
        Metric("farrowing_rate", "分娩率", "%", 1, HIGH),
        Metric("assisted", "助產分娩", "窩", 0, LOW),
        Metric("small_litters_pct", "活仔少於 7 頭的窩", "%", 1, LOW),
        Metric("total_born", "總產仔數", "隻", 0, None),
        Metric("born_per_litter", "窩均總仔數", "隻", 1, HIGH),
        Metric("born_alive", "活仔數", "隻", 0, None),
        Metric("alive_per_litter", "窩均活仔數", "隻", 1, HIGH),
        Metric("stillborn_pct", "死胎率", "%", 1, LOW),
        Metric("stillborn_per_litter", "窩均死胎", "隻", 1, LOW),
        Metric("mummified_pct", "木乃伊率", "%", 1, LOW),
        Metric("gestation_days", "懷孕天數", "天", 1, None),
        Metric("farrowing_interval", "分娩間隔", "天", 1, LOW),
        Metric("avg_farrow_parity", "平均分娩胎次", "胎", 1, None),
    ]),
    Section("piglet_loss", "仔豬死亡", [
        Metric("piglet_deaths", "仔豬死亡(逐筆記錄)", "隻", 0, LOW),
        Metric("piglet_death_pct", "記錄到的死亡占活仔數", "%", 1, LOW),
        Metric("avg_death_age", "死亡平均日齡", "天", 1, None),
        Metric("deaths_under_2d", "未滿 2 日齡", "隻", 0, LOW),
        Metric("deaths_under_2d_pct", "未滿 2 日齡占死亡數", "%", 1, None),
        Metric("deaths_2_8d", "2–8 日齡", "隻", 0, LOW),
        Metric("deaths_over_8d", "超過 8 日齡", "隻", 0, LOW),
        Metric("crushed_pct", "母豬壓死占死亡數", "%", 1, LOW),
    ]),
    Section("weaning", "離乳", [
        Metric("sows_weaned", "離乳母豬數", "頭", 0, None),
        Metric("piglets_weaned", "離乳仔豬數", "隻", 0, None),
        Metric("weaned_per_litter", "窩均離乳數", "隻", 1, HIGH),
        Metric("preweaning_mortality", "離乳前死亡率", "%", 1, LOW),
        Metric("lactation_days", "哺乳天數", "天", 1, None),
        Metric("psy", "PSY(母豬年產離乳仔豬數)", "隻", 1, HIGH),
        Metric("avg_wean_parity", "平均離乳胎次", "胎", 1, None),
        Metric("wean_score", "離乳仔豬評分", "分", 1, HIGH),
    ]),
    Section("herd", "在養與異動", [
        Metric("avg_herd", "平均在養母豬", "頭", 0, None),
        Metric("ending_herd", "期末在養母豬", "頭", 0, None),
        Metric("avg_parity", "平均胎次", "胎", 1, None),
        Metric("gilt_entries", "新女豬入群", "頭", 0, None),
        Metric("replacement_rate", "更新率(年化)", "%", 1, None),
        Metric("culls", "淘汰", "頭", 0, None),
        Metric("cull_rate", "淘汰率(年化)", "%", 1, LOW),
        Metric("avg_cull_parity", "平均淘汰胎次", "胎", 1, None),
        Metric("sow_deaths", "母豬死亡", "頭", 0, LOW),
        Metric("mortality_rate", "母豬死亡率(年化)", "%", 1, LOW),
        Metric("abortions", "流產", "次", 0, LOW),
        Metric("npd", "非生產天數/母豬/年", "天", 1, LOW),
    ]),
]

METRICS: Dict[str, Metric] = {m.key: m for s in SECTIONS for m in s.metrics}


# ── 期間切分 ──────────────────────────────────────────────────

def _week_start(day: date, start_day: int) -> date:
    return day - timedelta(days=(day.weekday() - start_day) % 7)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _add_month(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def periods(start: date, end: date, grain: str,
            settings: Optional[dict] = None) -> List[Period]:
    """把一段日期切成一格一格。

    週的起點跟工作清單同一個設定(`week_start_day`)—— 這個場的一週從
    禮拜四開始,報告的週界要是跟工作清單不一樣,同一批母豬會被切到兩欄
    去,牧場主拿兩個畫面對不起來。
    """
    if grain not in GRAINS:
        raise ValueError(f"不認得的期別:{grain}")
    cfg = settings_with_defaults(settings)
    out: List[Period] = []

    def add(key, label, lo, hi):
        # **最後一格要夾到報告的結束日。** 不夾的話,「2026 年」這一格會被
        # 當成 365 天,但資料只到 8 月 —— 年化的淘汰率、更新率、非生產天數
        # 會全部被稀釋成三分之二,今年看起來憑空變好。
        #
        # 起點刻意**不**夾:第一格要是完整的一週/一個月,每一欄的長度才
        # 一樣、數字才比得起來。夾了的話「從 8/31 開始的週報」第一欄只涵蓋
        # 一天,分娩窩數看起來是別欄的七分之一。PigCHAMP 那份報告也是從
        # 週界起算的(標題寫 01/03 到 01/01,第一欄就是 01/03–01/09)。
        out.append(Period(key, label, lo, min(hi, end)))

    if grain == WEEK:
        cur = _week_start(start, cfg["week_start_day"])
        while cur <= end:
            stop = cur + timedelta(days=6)
            iso = cur.isocalendar()
            add(f"{iso[0]}-W{iso[1]:02d}", f"{cur:%m/%d}–{stop:%m/%d}", cur, stop)
            cur = stop + timedelta(days=1)
        return out

    if grain == MONTH:
        cur = _month_start(start)
        while cur <= end:
            stop = _add_month(cur, 1) - timedelta(days=1)
            add(f"{cur:%Y-%m}", f"{cur:%Y/%m}", cur, stop)
            cur = _add_month(cur, 1)
        return out

    if grain == QUARTER:
        cur = date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
        while cur <= end:
            stop = _add_month(cur, 3) - timedelta(days=1)
            q = (cur.month - 1) // 3 + 1
            add(f"{cur.year}-Q{q}", f"{cur.year} Q{q}", cur, stop)
            cur = _add_month(cur, 3)
        return out

    cur = date(start.year, 1, 1)
    while cur <= end:
        stop = date(cur.year, 12, 31)
        add(str(cur.year), f"{cur.year} 年", cur, stop)
        cur = date(cur.year + 1, 1, 1)
    return out


# 沒指定範圍時,一次看幾期才夠看出走向又不會塞爆手機畫面。
#   週:12 期 ≈ 一季,這個場一週一批,12 批看得出批間差異。
#   月:12 期 = 一年,跟月報的直覺一致。
#   季:8 期 = 兩年,足以跨過一次淡旺季循環。
#   年:5 期,對照使用者手上的 PigCHAMP 報告一次看五年。
DEFAULT_PERIODS = {WEEK: 12, MONTH: 12, QUARTER: 8, YEAR: 5}

# 每期粗估幾天,用來從「要幾期」反推一個夠早的起點。刻意抓寬一點
# (月用 32 天、季用 95 天)——寧可多算出一期被呼叫端捨去,也不要因為
# 抓太緊少算一期。
_APPROX_DAYS = {WEEK: 7, MONTH: 32, QUARTER: 95, YEAR: 367}


def default_start(end: date, grain: str, count: int) -> date:
    """從結束日往回推,推到一個「肯定涵蓋得了 `count` 期」的起點。

    不直接算出精確的起點,是因為期界會被月份長短、閏年這些因素影響 ——
    往回抓寬一點,交給 `periods()` 切出真正的期界,呼叫端再取最後
    `count` 個,兩邊都不必重新實作對方的切法。
    """
    return end - timedelta(days=_APPROX_DAYS[grain] * (count + 1))


# ── 計算 ──────────────────────────────────────────────────────

def _heats(days: List[date], gap: int) -> List[List[date]]:
    """把連續幾天的配種併成一次發情。

    一頭母豬一次發情連配 2–3 天、一天一筆,那是**一次**配種行為不是三次。
    受胎率、分娩率、重發情這些指標的分母都必須是發情次數 —— 拿筆數當分母
    會把成績算成實際的一半(這個場實測 1.95 筆/次)。
    """
    out: List[List[date]] = []
    for d in sorted(days):
        if out and (d - out[-1][-1]).days <= gap:
            out[-1].append(d)
        else:
            out.append([d])
    return out


class _Ctx(NamedTuple):
    sows: List[dict]
    by_sow: Dict[int, List[dict]]
    by_type: Dict[str, List[dict]]
    heats: List[dict]          # {"sow_id", "start", "end", "services", "boars", "next"}
    cfg: dict
    # 每頭母豬的分娩日期(已排序)。胎次 = 該日之前的分娩次數,用二分搜尋
    # 查 —— 原本每算一次都掃一遍她的全部事件,週報 53 期 x 三萬筆事件等於
    # 白掃幾百萬次。
    farrows: Dict[int, List[date]]
    # 每頭母豬的(進場日, 離群日)。「這天在不在場」本來也是每次重掃。
    span: Dict[int, tuple]
    # 資料的最後一天。受胎率要據此排除「還太新、看不出重發情」的那幾次
    # 配種 —— 少了它,最近一期永遠會是 100%。
    horizon: date


def _build_ctx(sows, events, settings) -> _Ctx:
    sows = list(sows)
    # 排除掉的記錄不進統計 —— 那是使用者在匯入時就判斷過的離群值。
    live = [e for e in events if not e.get("excluded")]
    by_sow = _by_sow(live)
    by_type: Dict[str, List[dict]] = {}
    for e in live:
        by_type.setdefault(e["event_type"], []).append(e)

    cfg = settings_with_defaults(settings)
    gap = cfg["mating_series_days"]
    heats = []
    for sow_id, rows in by_sow.items():
        mt = [e for e in rows if e["event_type"] == MATE]
        for group in _heats([e["event_date"] for e in mt], gap):
            boars = {(e.get("detail") or {}).get("boar_tag")
                     for e in mt if e["event_date"] in group}
            heats.append({"sow_id": sow_id, "start": group[0], "end": group[-1],
                          "services": len(group),
                          "boars": {b for b in boars if b}})
    heats.sort(key=lambda h: (h["sow_id"], h["start"]))
    # 每一次發情記住「她的下一次發情是哪天」。判定受胎與重發情都要用到,
    # 而事後再從清單裡找等於每次都掃一遍。
    for a, b in zip(heats, heats[1:]):
        a["next"] = b["start"] if b["sow_id"] == a["sow_id"] else None
    if heats:
        heats[-1]["next"] = None
    horizon = max((e["event_date"] for e in live), default=date(1900, 1, 1))

    farrows = {sid: sorted(e["event_date"] for e in rows
                          if e["event_type"] == FARROW)
               for sid, rows in by_sow.items()}
    span = {}
    for row in sows:
        rows = by_sow.get(row["id"], [])
        entry = row.get("entry_date")
        if entry is None:
            # 歷史資料不一定有進場記錄。用「今天」當進場日會讓一頭早就在場
            # 的母豬看起來是剛到的(跟 schedule._in_herd_on 同一個理由)。
            entry = rows[0]["event_date"] if rows else None
        exits = [e["event_date"] for e in rows if e["event_type"] in EXIT_EVENTS]
        span[row["id"]] = (entry, min(exits) if exits else None)

    return _Ctx(sows, by_sow, by_type, heats, cfg, farrows, span, horizon)


def _conceived(heat: dict, rows: List[dict], cfg: dict,
               horizon: date) -> Optional[bool]:
    """這一次配種有沒有受胎?判不出來就回 None(不猜)。

    這個場**只在驗孕陰性時才登記**,所以不能拿「陽性/驗孕總數」算受胎率
    —— 陽性根本沒進系統,那樣算會得到 0%。使用者給的兩條判定規則:

      1. 有明確的驗孕陰性記錄 → 陰性
      2. 沒分娩就又配了一次(約 21 天後重發情)→ 前一次是陰性

    反過來,受胎的證據是她後來真的分娩了,或是流產(流產代表**有**受胎,
    只是沒生下來 —— 算進受胎率,不算進分娩率)。

    三種證據都沒有就回 None:她可能在還沒看出結果之前就被淘汰了。把這種
    情形當成受胎會虛報成績,當成陰性會冤枉她,兩個都不誠實。
    """
    start = heat["start"]
    gest = cfg["gestation_days"]

    for e in rows:
        when = e["event_date"]
        if when < start:
            continue
        kind = e["event_type"]
        if (kind == PREG_CHECK and (e.get("detail") or {}).get("positive") is False
                and when <= start + timedelta(days=NEGATIVE_CHECK_WINDOW)):
            return False
        if kind == FARROW and start + timedelta(days=100) <= when <= start + timedelta(days=gest + 16):
            return True
        if kind == ABORT and when <= start + timedelta(days=gest + 16):
            return True

    nxt = heat.get("next")
    if nxt is not None and nxt > heat["end"]:
        # 沒分娩就又發情了 —— 上面那一圈已經確認這段期間沒有分娩記錄。
        return False

    # 判斷期還沒過完:太新的配種還看不出重發情,算進去會讓最近幾期永遠 100%。
    if start + timedelta(days=CONCEPTION_JUDGE_DAYS) > horizon:
        return None
    return None


def _avg(values):
    return sum(values) / len(values) if values else None


def _pct(part, whole):
    return part / whole * 100 if whole else None


def _parity_at(ctx: "_Ctx", sow_id: int, on: date) -> int:
    """這一天之前她生過幾胎。二分搜尋,不重掃她的事件。"""
    return bisect.bisect_left(ctx.farrows.get(sow_id, ()), on)


def _in_herd(ctx: "_Ctx", sow_id: int, on: date) -> bool:
    """這天她在不在場(進場之後、還沒離群)。"""
    entry, gone = ctx.span.get(sow_id, (None, None))
    if entry is None or entry > on:
        return False
    return gone is None or gone > on


def _period_values(ctx: _Ctx, start: date, end: date) -> Dict[str, Optional[float]]:
    """一個期間的所有指標。

    每一項都各自看有沒有底層記錄,**沒有就是 None,不補 0**。0 跟「沒記」
    在報告裡是兩件完全不同的事:前者是「這期真的一頭都沒死」,後者是
    「這期沒有人記」,混在一起會讓趨勢圖憑空多出一個谷底(憲法第三條)。
    """
    cfg = ctx.cfg
    days = (end - start).days + 1
    annualize = DAYS_PER_YEAR / days
    detail = lambda e: e.get("detail") or {}

    def within(code):
        return [e for e in ctx.by_type.get(code, [])
                if start <= e["event_date"] <= end]

    v: Dict[str, Optional[float]] = {}

    # ── 配種 ──
    heats_now = [h for h in ctx.heats if start <= h["start"] <= end]
    v["services"] = sum(h["services"] for h in heats_now) or None
    v["heats"] = len(heats_now) or None
    v["services_per_heat"] = (v["services"] / v["heats"]) if v["heats"] else None
    v["boars_used"] = len({b for h in heats_now for b in h["boars"]}) or None
    v["avg_service_parity"] = _avg([_parity_at(ctx, h["sow_id"], h["start"])
                                    for h in heats_now])

    # 初配 = 這頭母豬有史以來第一次發情;新女豬初配 = 她當時胎次還是 0
    first, gilt_first = 0, 0
    seen_first = {}
    for h in ctx.heats:
        seen_first.setdefault(h["sow_id"], h["start"])
    for h in heats_now:
        if seen_first.get(h["sow_id"]) == h["start"]:
            first += 1
            if _parity_at(ctx, h["sow_id"], h["start"]) == 0:
                gilt_first += 1
    v["first_services"] = first or None
    v["gilt_first_services"] = gilt_first or None

    # 離乳到配種:每次發情往前找她自己最近一次離乳
    gaps, within7 = [], 0
    for h in heats_now:
        weans = [e["event_date"] for e in ctx.by_sow.get(h["sow_id"], [])
                 if e["event_type"] == WEAN and e["event_date"] < h["start"]]
        if not weans:
            continue
        gap = (h["start"] - max(weans)).days
        # 離太遠的不是「離乳後配種」,是中間空過一整輪(那算在非生產天數)
        if gap <= cfg["open_sow_alert_days"]:
            gaps.append(gap)
            if gap <= 7:
                within7 += 1
    v["wean_to_service_days"] = _avg(gaps)
    v["serviced_within_7d_pct"] = _pct(within7, len(gaps))

    # 重發情:上一次發情之後沒分娩,又在一個發情週期內再配
    repeat = 0
    prev_by_sow: Dict[int, dict] = {}
    for h in ctx.heats:
        prev = prev_by_sow.get(h["sow_id"])
        if prev is not None and start <= h["start"] <= end:
            farrowed = any(e["event_type"] == FARROW
                           and prev["start"] < e["event_date"] < h["start"]
                           for e in ctx.by_sow.get(h["sow_id"], []))
            if not farrowed and (h["start"] - prev["end"]).days <= cfg["min_cycle_days"]:
                repeat += 1
        prev_by_sow[h["sow_id"]] = h
    v["repeat_heats"] = repeat or None
    v["repeat_pct"] = _pct(repeat, len(heats_now))

    # 受胎率:**不是**「陽性/驗孕總數」。這個場只登記驗孕陰性,陽性沒進
    # 系統,那樣算會得到 0%(見 _conceived 的說明)。改成逐次發情判定,
    # 判不出來的那幾次不進分母。
    judged = [(h, _conceived(h, ctx.by_sow.get(h["sow_id"], []), cfg, ctx.horizon))
              for h in heats_now]
    decided = [ok for _, ok in judged if ok is not None]
    v["conception_rate"] = _pct(sum(1 for ok in decided if ok), len(decided))
    v["judged_heats"] = len(decided) or None

    # ── 分娩 ──
    fw = within(FARROW)
    v["litters"] = len(fw) or None
    v["assisted"] = sum(1 for e in fw if detail(e).get("assisted")) or None

    alive = [detail(e).get("born_alive") for e in fw]
    alive = [n for n in alive if isinstance(n, int)]
    still = [n for n in (detail(e).get("stillborn") for e in fw) if isinstance(n, int)]
    mummy = [n for n in (detail(e).get("mummified") for e in fw) if isinstance(n, int)]
    total_born = sum(alive) + sum(still) + sum(mummy)
    v["born_alive"] = sum(alive) or None
    v["total_born"] = total_born or None
    v["born_per_litter"] = (total_born / len(alive)) if alive else None
    v["alive_per_litter"] = _avg(alive)
    v["stillborn_pct"] = _pct(sum(still), total_born)
    v["stillborn_per_litter"] = _avg(still)
    v["mummified_pct"] = _pct(sum(mummy), total_born)
    v["small_litters_pct"] = _pct(sum(1 for n in alive if n < 7), len(alive))
    v["avg_farrow_parity"] = _avg([_parity_at(ctx, e["sow_id"], e["event_date"]) + 1 for e in fw])

    # 分娩率:分母是**約 gestation 天前的發情次數**,不是這期的配種。
    # 拿當期配種當分母,配種量一波動分娩率就會出現跟表現無關的假跳動。
    back = timedelta(days=cfg["gestation_days"])
    mated_then = [h for h in ctx.heats if start - back <= h["start"] <= end - back]
    v["farrowing_rate"] = _pct(len(fw), len(mated_then))

    # 懷孕天數 / 分娩間隔:每一窩往前找她自己的上一次發情、上一次分娩
    gest, interval = [], []
    for e in fw:
        rows = ctx.by_sow.get(e["sow_id"], [])
        prior = [h for h in ctx.heats
                 if h["sow_id"] == e["sow_id"] and h["start"] < e["event_date"]]
        if prior:
            span = (e["event_date"] - prior[-1]["start"]).days
            if 100 <= span <= 130:          # 對不到的那次不拿來拉平均
                gest.append(span)
        prev_fw = [x["event_date"] for x in rows
                   if x["event_type"] == FARROW and x["event_date"] < e["event_date"]]
        if prev_fw:
            interval.append((e["event_date"] - max(prev_fw)).days)
    v["gestation_days"] = _avg(gest)
    v["farrowing_interval"] = _avg(interval)

    # ── 仔豬死亡 ──
    pl = within(PIGLET_LOSS)
    counts = [detail(e).get("count") for e in pl]
    counts = [n for n in counts if isinstance(n, int)]
    dead = sum(counts)
    v["piglet_deaths"] = dead or None
    v["piglet_death_pct"] = _pct(dead, sum(alive)) if alive else None

    ages, buckets = [], collections.Counter()
    for e in pl:
        prior = [x["event_date"] for x in ctx.by_sow.get(e["sow_id"], [])
                 if x["event_type"] == FARROW and x["event_date"] <= e["event_date"]]
        if not prior:
            continue
        age = (e["event_date"] - max(prior)).days
        n = detail(e).get("count")
        n = n if isinstance(n, int) else 1
        ages += [age] * n
        buckets["u2" if age < 2 else "d2_8" if age <= 8 else "o8"] += n
    v["avg_death_age"] = _avg(ages)
    v["deaths_under_2d"] = buckets["u2"] or None
    v["deaths_2_8d"] = buckets["d2_8"] or None
    v["deaths_over_8d"] = buckets["o8"] or None
    v["deaths_under_2d_pct"] = _pct(buckets["u2"], sum(buckets.values()))

    crushed = sum(n for e, n in ((e, detail(e).get("count")) for e in pl)
                  if isinstance(n, int) and "壓死" in (detail(e).get("reason") or ""))
    v["crushed_pct"] = _pct(crushed, dead)

    # ── 離乳 ──
    wn = within(WEAN)
    weaned = [n for n in (detail(e).get("weaned") for e in wn) if isinstance(n, int)]
    v["sows_weaned"] = len(wn) or None
    v["piglets_weaned"] = sum(weaned) or None
    v["weaned_per_litter"] = _avg(weaned)
    v["avg_wean_parity"] = _avg([_parity_at(ctx, e["sow_id"], e["event_date"]) for e in wn])
    scores = [n for n in (detail(e).get("wean_score") for e in wn)
              if isinstance(n, int)]
    v["wean_score"] = _avg(scores)

    # 哺乳天數,以及每一窩對應的活仔數。兩個一起算是因為都要往前找同一筆
    # 分娩 —— 分開算等於把同一個查找做兩次。
    lact, cohort_alive, cohort_weaned = [], 0, 0
    for e in wn:
        rows = ctx.by_sow.get(e["sow_id"], [])
        prior = [x for x in rows
                 if x["event_type"] == FARROW and x["event_date"] <= e["event_date"]]
        if not prior:
            continue
        birth = max(prior, key=lambda x: x["event_date"])
        lact.append((e["event_date"] - birth["event_date"]).days)
        born = (birth.get("detail") or {}).get("born_alive")
        got = detail(e).get("weaned")
        if isinstance(born, int) and isinstance(got, int):
            cohort_alive += born
            cohort_weaned += got
    v["lactation_days"] = _avg(lact)

    # 離乳前死亡率 = (活仔 − 離乳) / 活仔,**按窩配對**:這一期離乳的每一
    # 窩,拿她自己那次分娩的活仔數當分母。直接拿「當期活仔」除的話,分子
    # 分母差了三週的時距,週報那一格會被錯開整整一批。
    #
    # 這是**場級**比率,不是逐頭算的 —— 仔豬會在窩間流動(寄養),個別
    # 母豬的 (活仔−離乳)/活仔 沒有意義(見 specs 第 1 條)。整期加總就沒有
    # 這個問題:流進流出在同一個場內互相抵銷。
    #
    # 注意它跟上面「記錄到的仔豬死亡」是**兩個不同的數字**,而且差很多:
    # 2025 年這裡算出 23.6%,逐筆記錄的仔豬死亡只有 7.2%。差距不是誤差,
    # 是三分之二的損失沒有被逐筆記下來 —— 兩個都顯示,那個落差本身就是
    # 要讓牧場主看見的事情。
    v["preweaning_mortality"] = _pct(cohort_alive - cohort_weaned, cohort_alive)

    # ── 在養與異動 ──
    at_start = sum(1 for s in ctx.sows
                   if _in_herd(ctx, s["id"], start))
    at_end = sum(1 for s in ctx.sows
                 if _in_herd(ctx, s["id"], end))
    herd = (at_start + at_end) / 2
    v["avg_herd"] = herd or None
    v["ending_herd"] = at_end or None
    v["avg_parity"] = _avg([_parity_at(ctx, s["id"], end)
                            for s in ctx.sows
                            if _in_herd(ctx, s["id"], end)])

    entries = within("GA")
    culls = within(CULL)
    deaths = within(DEATH)
    v["gilt_entries"] = len(entries) or None
    v["culls"] = len(culls) or None
    v["sow_deaths"] = len(deaths) or None
    v["abortions"] = len(within(ABORT)) or None
    v["replacement_rate"] = _pct(len(entries), herd) * annualize if herd else None
    v["cull_rate"] = _pct(len(culls), herd) * annualize if herd else None
    v["mortality_rate"] = _pct(len(deaths), herd) * annualize if herd else None
    v["avg_cull_parity"] = _avg([_parity_at(ctx, e["sow_id"], e["event_date"]) for e in culls])

    v["psy"] = (sum(weaned) / herd * annualize) if herd and weaned else None

    # 非生產天數:在群天數扣掉懷孕與哺乳的天數,年化到每頭母豬。
    # 這個場的 105.6 天遠高於全國中位 62 —— 每一天都是純成本,所以它
    # 值得單獨一列,而不是埋在別的指標裡。
    productive = 0
    for e in fw:
        productive += cfg["gestation_days"]
    for span in lact:
        productive += span
    sow_days = herd * days
    v["npd"] = ((sow_days - productive) / herd * annualize) if herd else None
    return v


def trend_report(sows: Iterable[dict], events: Iterable[dict],
                 spans: List[Period], settings: Optional[dict] = None) -> dict:
    """把同一組指標算過每一個期間,並排回傳。

    `spans` 由呼叫端決定 —— 可以是連續的週/月/季(看趨勢),也可以是
    兜出來的幾段不連續期間(2024 全年 vs 2025 全年 vs 今年至今)。
    這兩種需求的差別只是傳進來的清單長什麼樣,不必是兩個功能。
    """
    ctx = _build_ctx(sows, events, settings)
    values = [_period_values(ctx, p.start, p.end) for p in spans]

    sections = []
    for section in SECTIONS:
        rows = []
        for m in section.metrics:
            series = [vals.get(m.key) for vals in values]
            # 整排都沒有資料的指標不出現。印一列空格會讓人以為自己漏記了。
            if all(x is None for x in series):
                continue
            rows.append({
                "key": m.key, "label": m.label, "unit": m.unit,
                "digits": m.digits, "better": m.better, "values": series,
                "change": _change(series, m.better),
            })
        if rows:
            sections.append({"key": section.key, "label": section.label,
                             "rows": rows})

    return {
        "periods": [{"key": p.key, "label": p.label,
                     "start": p.start, "end": p.end} for p in spans],
        "sections": sections,
    }


def _change(series: List[Optional[float]], better: Optional[str]) -> Optional[dict]:
    """最後一期跟第一期的差。

    只有兩端都有數字才算 —— 其中一端是 None 代表那期沒有記錄,拿「沒有
    記錄」去跟「有記錄」相減會算出一個看起來很嚴重、其實不存在的變化。
    """
    first = next((x for x in series if x is not None), None)
    last = next((x for x in reversed(series) if x is not None), None)
    if first is None or last is None or len(series) < 2 or first == last:
        return None
    delta = last - first
    return {
        "delta": delta,
        "pct": (delta / first * 100) if first else None,
        # 「變好還是變壞」由指標自己的方向決定 —— 死胎率下降是好事,
        # 活仔數下降是壞事,同樣是負數但意思相反。
        "improved": None if better is None else (delta > 0) == (better == HIGH),
    }
