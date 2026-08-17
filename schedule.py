"""工作推算 —— 從母豬的事件記錄算出這週要做什麼。

**為什麼放頂層而不是 core/**:`tests/test_core_purity.py` 禁止 `core/` 匯入
`datetime` 與 `time`(理由是破壞確定性)。這裡整個模組都是日期運算。
`auth.py` 是同樣情況的先例 —— 它需要 `datetime` 所以也不在 `core/`。

確定性改用**把「今天」與週次當參數傳入**來維持:模組內絕不呼叫
`date.today()`,測試才能固定日期斷言。這達成了 `core/` 禁 datetime 想達成
的效果,只是換一種方式。

不碰 HTTP(在 server.py),也不碰 SQL(在 db.py)。這裡只有規則本身。
"""

from datetime import date, timedelta
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

# 預設間隔。**這些數字量自這個牧場 32,814 筆真實事件的中位數**,
# 不是教科書數字:
#
#   離乳 → 配種    5 天   (四分位 4–5,   n=4547)
#   配種 → 分娩  114 天   (四分位 113–114, n=4149)
#   分娩 → 離乳   22 天   (四分位 22–22,  n=5145)
#   配種 → 驗孕   26 天   (四分位 25–28,  n=1252)
#
# 各場慣例不同,因此全部可由牧場設定覆寫(見 Settings)。
DEFAULTS = {
    "gestation_days": 114,        # 配種 → 預產期
    "pre_farrow_move_days": 14,   # 預產期前幾天移入產房
    "induction_day": 113,         # 懷孕第幾天提醒催產
    "lactation_days": 22,         # 分娩 → 離乳
    "service_after_wean_days": 5, # 離乳 → 配種
    "preg_check_days": 26,        # 配種 → 驗孕
    "open_sow_alert_days": 30,    # 離乳/驗孕陰性後多久沒動作要提醒

    # 配種後幾天,從配種區移至待產區。**配種了、沒登記驗孕陰性就當懷孕**
    # (使用者決定)—— 不必等驗孕陽性才觸發,這個場很少逐頭驗孕,等驗孕
    # 的話大多數其實已經懷孕的母豬會一直卡在配種區。
    "to_gestation_zone_days": 60,

    # 總產房欄位數。**使用者自己填,不是算出來的**(使用者決定)——
    # 移欄記錄(pens 表)只會累積「曾經被記錄過的欄位名稱」,牧場實際的
    # 產房總數在還沒被移欄記錄提到之前不會出現在那份清單裡,拿清單長度
    # 當總數會系統性低估,「產房空間不足」的提醒會失真(憲法第三條)。
    # 0 表示未設定,不宣稱空間夠或不夠。
    "farrowing_pens": 0,

    # 「值得檢視」的判準。門檻量自這個牧場的實際分布(451 頭在場母豬):
    #
    #   連續下滑胎數   下滑 2 胎 20 頭、3 胎 2 頭 → 取 2 才有意義的名單長度
    #   每胎非生產天數 中位數 8.3 天、第 90 百分位 39 天 → 取 40
    #   活仔數         用場內百分位而非固定數字,見 review_low_alive_pct
    #
    # 全部可調:別的牧場的分布不會一樣,寫死等於只為這一場服務。
    "review_decline_litters": 2,   # 連續幾胎活仔數下滑才列入
    "review_npd_days": 40,         # 每胎非生產天數超過幾天才列入
    "review_low_alive_pct": 10,    # 活仔數落在場內後幾 % 才列入
    "review_min_litters": 3,       # 至少幾胎才判斷 —— 一兩胎看不出趨勢
    "review_min_herd": 10,         # 全場不足這個頭數就不比活仔數(見下)
}

# 事件代碼(沿用 PigCHAMP,匯入才對得起來)。MOVE_PEN 是本系統自己的
# 代碼,不是 PigCHAMP 原生代碼(匯入的檔案不會產生它,不會撞號)。
MATE, PREG_CHECK, FARROW, WEAN = "MT", "PD", "FW", "WN"
PIGLET_LOSS, DEATH, CULL, ABORT = "PL", "DTH", "SAL", "AB"
MOVE_PEN = "MV"
EXIT_EVENTS = (DEATH, CULL)

# 三個區域。母豬依生產週期在三者之間搬動,移欄記錄時要指定 zone
# (見 server.py 對 MOVE_PEN 事件的處理)。
ZONE_MATING, ZONE_GESTATION, ZONE_FARROWING = "mating", "gestation", "farrowing"
ZONES = (ZONE_MATING, ZONE_GESTATION, ZONE_FARROWING)

# 工作類型
(MOVE_IN, INDUCE, FARROW_DUE, WEAN_DUE, MATE_DUE, CHECK_DUE,
 MOVE_TO_MATING, MOVE_TO_GESTATION) = (
    "move_in", "induce", "farrow", "wean", "mate", "preg_check",
    "move_to_mating", "move_to_gestation")

# 可以記錄的事件代碼。server.py 用它擋掉不認得的類型 ——
# 前端送什麼過來都不可信(憲法第四條)。
KNOWN_EVENTS = frozenset({
    MATE, PREG_CHECK, FARROW, WEAN, PIGLET_LOSS,
    DEATH, CULL, ABORT, "GA", "FON", "FOF", MOVE_PEN,
})

# 公豬專屬的事件代碼:SC 採精。跟母豬事件分開檢查 ——
# 兩邊代碼不通用,一頭母豬不能記「採精」。
#
# DEATH(DTH)例外:種豬死亡不分公母是同一種事件(使用者決定「種豬死亡」
# 跟母豬死亡合併),只是公豬跟母豬本來就存在不同資料表,所以兩邊都要
# 認得這個代碼。
SEMEN_COLLECT = "SC"
KNOWN_BOAR_EVENTS = frozenset({SEMEN_COLLECT, DEATH})


class Task(NamedTuple):
    """一件待辦。`why` 說明「為什麼是這天」,畫面上要顯示 —— 使用者才知道
    系統憑什麼這樣算,而不是看到一個沒有理由的名單。
    """
    kind: str
    sow_id: int
    ear_tag: str
    due: date
    why: str


# 每一項的合理範圍。上下限都刻意寬鬆 —— 這是防呆與防惡意,不是替牧場主
# 決定怎麼養豬。但一定要有:懷孕天數設成 0 會讓整個工作清單瞬間爆量,
# 設成 100000 則是永遠沒有工作,兩種都是「畫面壞掉」而不是「設定特別」。
SETTING_RANGES = {
    "gestation_days": (100, 130),
    "pre_farrow_move_days": (0, 40),
    "induction_day": (100, 130),
    "lactation_days": (10, 45),
    "service_after_wean_days": (0, 60),
    "preg_check_days": (14, 60),
    "open_sow_alert_days": (7, 180),
    "to_gestation_zone_days": (7, 150),
    "farrowing_pens": (0, 5000),
    "review_decline_litters": (1, 10),
    "review_npd_days": (10, 200),
    "review_low_alive_pct": (1, 50),
    "review_min_litters": (1, 12),
    "review_min_herd": (2, 200),
}


def settings_with_defaults(settings: Optional[dict] = None) -> dict:
    merged = dict(DEFAULTS)
    if settings:
        merged.update({k: v for k, v in settings.items() if v is not None})
    return merged


def clean_settings(incoming: dict) -> tuple:
    """驗證前端送來的設定,回 (只含非預設值的 dict, 問題清單)。

    三件事在這裡一起做完:

    1. **不認得的鍵直接丟掉。** 前端送什麼都不可信(憲法第四條)。
    2. **超出範圍就拒絕**,不是默默夾到邊界 —— 使用者填了 999 卻被存成
       130,畫面顯示 130 而他以為是 999,那比報錯還糟。
    3. **與預設值相同的項目不存。** 存下來的話,日後調整預設值不會生效
       在任何既有牧場,而且沒有人會發現(見 db.Store.get_farm_settings)。
    """
    cleaned, problems = {}, []

    for key, value in incoming.items():
        if key not in SETTING_RANGES:
            continue                          # 不認得的鍵:忽略,不報錯
        if isinstance(value, bool) or not isinstance(value, int):
            # bool 是 int 的子類別,不擋掉的話 True 會被當成 1 存進去
            problems.append(f"{key} 必須是整數")
            continue
        low, high = SETTING_RANGES[key]
        if not low <= value <= high:
            problems.append(f"{key} 必須介於 {low} 到 {high} 之間")
            continue
        if value != DEFAULTS[key]:
            cleaned[key] = value

    return cleaned, problems


def _by_sow(events: Iterable[dict]) -> Dict[int, List[dict]]:
    """把事件依母豬分組並排序。

    **排除 excluded 的事件** —— 那些是匯入時使用者判定為離群值的記錄
    (例如單窩 56 隻),不納入統計也不該拿來推算下一步。
    """
    grouped: Dict[int, List[dict]] = {}
    for e in events:
        if e.get("excluded"):
            continue
        grouped.setdefault(e["sow_id"], []).append(e)
    for rows in grouped.values():
        rows.sort(key=lambda e: (e["event_date"], e.get("id", 0)))
    return grouped


def current_cycle(events: List[dict]) -> dict:
    """母豬目前的生產週期狀態。

    只看**最後一次離乳(或進場)之後**的事件 —— 上一胎的配種與這一胎的
    無關,混在一起會讓推算取到過期的日期。

    回傳各關鍵事件的日期;沒發生過的是 None。配種取**該批的第一天**:
    這個場一次配種連續 2–3 天,用最後一天算預產期會systematically 短算。
    """
    state = {"mate": None, "preg_check": None, "preg_positive": None,
             "farrow": None, "wean": None, "exited": None}

    for e in events:
        code, when = e["event_type"], e["event_date"]
        if code in EXIT_EVENTS:
            state["exited"] = when
        elif code == WEAN:
            state["wean"] = when
            # 離乳結束一個週期,下一胎重新開始
            state["mate"] = state["preg_check"] = state["farrow"] = None
            state["preg_positive"] = None
        elif code == MATE:
            if state["mate"] is None or state["farrow"] is not None:
                state["mate"] = when      # 一批配種只取第一天
                state["farrow"] = None
        elif code == PREG_CHECK:
            state["preg_check"] = when
            positive = (e.get("detail") or {}).get("positive")
            state["preg_positive"] = positive
            # 驗孕陰性 = 沒懷孕,回到待配種。少了這一行,她會一直被排出
            # 永遠不會發生的分娩與催產工作 —— 用真實資料驗證時,分娩預測的
            # 平均誤差因此被拉到 +8.9 天(中位數其實是 0)。
            # 這個場目前就有 50 頭處於驗孕陰性狀態。
            if positive is False:
                state["mate"] = None
        elif code == FARROW:
            state["farrow"] = when
        elif code == ABORT:
            state["mate"] = None          # 流產:回到待配種
    return state


def sow_status(sow: dict, events: List[dict], today: date,
               settings: Optional[dict] = None) -> dict:
    """母豬現在處於哪個階段,以及預產期。

    回傳 `state`(給程式判斷)與 `day`/`due`/`since`(給畫面顯示)。
    文字一律不在這裡組 —— 那在 core/labels.py(憲法:使用者看得到的字
    只該有一份定義)。

    **配種後沒登記驗孕陰性,就當她懷孕。**(使用者決定)這個場配種後
    很少逐頭驗孕,曾經要求「只有陽性驗孕才算懷孕」,結果大多數其實已經
    懷孕的母豬一直卡在模糊狀態,產房、待產區的提醒都推算不出來。
    驗孕陰性仍然作數 —— `current_cycle()` 一驗到陰性就把 `mate` 重置掉,
    根本不會走到這個分支,所以這裡不用再另外判斷陰性。
    """
    cfg = settings_with_defaults(settings)
    c = current_cycle(events)

    if c["exited"] or sow.get("status") not in (None, "active"):
        return {"state": "exited", "since": c["exited"], "day": None, "due": None}

    if c["farrow"] and not c["wean"]:
        return {"state": "lactating", "since": c["farrow"],
                "day": (today - c["farrow"]).days, "due": None,
                "wean_due": c["farrow"] + timedelta(days=cfg["lactation_days"])}

    if c["mate"] and not c["farrow"]:
        due = c["mate"] + timedelta(days=cfg["gestation_days"])
        # 預產日過了卻沒有分娩記錄:她要嘛生了沒登記,要嘛沒保住。
        # 兩種都需要有人去看,所以不能把一個已經過去的日期當成未來的
        # 「預產」顯示 —— 實測 200 頭裡有 88 頭的預產日已過,最久的過了
        # 611 天,那樣的畫面等於在說謊。
        overdue = (today - due).days
        result = {"state": "pregnant", "since": c["mate"],
                  "day": (today - c["mate"]).days, "due": due,
                  "overdue_days": overdue if overdue > 0 else None,
                  "move_in_due": due - timedelta(days=cfg["pre_farrow_move_days"])}

        # 還沒有「真的驗過且結果是陽性」時,時間軸仍要提示「還沒驗孕」——
        # 這件事原本只出現在狀態列,時間軸裡完全看不出來:2580 配種
        # 143 天、從沒驗孕過,時間軸裡就是少了一列「驗孕」,使用者得自己
        # 數才會發現。只是現在這件事不再影響她被不被當成懷孕,單純只是
        # 「還沒確認,建議去驗」的提示。
        if c["preg_positive"] is not True:
            check_due = c["mate"] + timedelta(days=cfg["preg_check_days"])
            check_overdue = (today - check_due).days
            result["preg_checked"] = c["preg_check"] is not None
            result["preg_check_due"] = check_due
            result["preg_check_overdue_days"] = check_overdue if check_overdue > 0 else None

        return result

    # 待配種。since 取最後一次離乳或驗孕陰性 —— 「空了幾天」是這個階段
    # 唯一有意義的數字,而這個場有 44 頭超過 30 天沒有下一步動作。
    since = max([d for d in (c["wean"], c["preg_check"]) if d], default=None)
    if since is None and events:
        since = events[-1]["event_date"]
    return {"state": "open", "since": since, "due": None,
            "day": (today - since).days if since else None}


def tasks_for_sow(sow: dict, events: List[dict], cfg: dict) -> List[Task]:
    """一頭母豬目前該做的事。每頭在同一時間點只會有一件主要工作。"""
    if sow.get("status") not in (None, "active"):
        return []

    c = current_cycle(events)
    if c["exited"]:
        return []

    tag, sid = sow.get("ear_tag", ""), sow["id"]
    out: List[Task] = []

    if c["mate"] and not c["farrow"]:
        mated = c["mate"]
        due_farrow = mated + timedelta(days=cfg["gestation_days"])

        if not c["preg_check"]:
            out.append(Task(CHECK_DUE, sid, tag,
                            mated + timedelta(days=cfg["preg_check_days"]),
                            f"配種後 {cfg['preg_check_days']} 天"))

        # 日期用 f-string 手動組,不用 strftime:%-m(去掉補零)在 Windows
        # 上不支援(那邊是 %#m),用了會直接拋 ValueError。
        out.append(Task(MOVE_IN, sid, tag,
                        due_farrow - timedelta(days=cfg["pre_farrow_move_days"]),
                        f"預產 {due_farrow.month}/{due_farrow.day}"
                        f" 前 {cfg['pre_farrow_move_days']} 天"))
        out.append(Task(INDUCE, sid, tag,
                        mated + timedelta(days=cfg["induction_day"]),
                        f"懷孕第 {cfg['induction_day']} 天"))
        out.append(Task(FARROW_DUE, sid, tag, due_farrow, "預產日"))

        # 移至待產區:配種了、沒登記驗孕陰性,就當懷孕看待(使用者決定),
        # 從配種日起算,不必等驗孕陽性 —— 這個場很少逐頭驗孕,原本只認
        # 陽性驗孕的規則,會讓大多數其實已經懷孕的母豬一直卡在配種區,
        # 提醒不出待產區該準備。
        out.append(Task(MOVE_TO_GESTATION, sid, tag,
                        mated + timedelta(days=cfg["to_gestation_zone_days"]),
                        f"懷孕第 {cfg['to_gestation_zone_days']} 天"))

    elif c["farrow"] and not c["wean"]:
        out.append(Task(WEAN_DUE, sid, tag,
                        c["farrow"] + timedelta(days=cfg["lactation_days"]),
                        f"哺乳第 {cfg['lactation_days']} 天"))

    elif c["wean"] or (not c["mate"] and not c["farrow"]):
        base = c["wean"] or c["preg_check"]
        if base:
            out.append(Task(MATE_DUE, sid, tag,
                            base + timedelta(days=cfg["service_after_wean_days"]),
                            f"離乳後 {cfg['service_after_wean_days']} 天"))
            # 移至配種區:跟該再配種同一個起算點(離乳,或驗孕陰性後
            # 重新開放配種),但不延遲 —— 得先人在配種區才配得到種,
            # 所以移動排在「該配種」之前那一刻,不是同一天以後才動作。
            out.append(Task(MOVE_TO_MATING, sid, tag, base, "轉為待配種"))
    return out


def build_week_tasks(sows: Iterable[dict], events: Iterable[dict],
                     week_start: date, week_end: date,
                     settings: Optional[dict] = None) -> List[dict]:
    """這一週的工作,依**工作類型**分組。

    不按日期拆:這個場跑批次生產,一週一批,整批母豬同一週做同一件事
    (每頭的配種都是連續兩天,整批同步)。按日拆會讓使用者以為每天都有
    零散工作,與實際作業方式不符(specs/v2-facts.md 第 7 條)。
    """
    cfg = settings_with_defaults(settings)
    grouped = _by_sow(events)

    buckets: Dict[str, List[Task]] = {}
    for sow in sows:
        for task in tasks_for_sow(sow, grouped.get(sow["id"], []), cfg):
            if week_start <= task.due <= week_end:
                buckets.setdefault(task.kind, []).append(task)

    order = [INDUCE, FARROW_DUE, WEAN_DUE, MATE_DUE, MOVE_TO_MATING,
             CHECK_DUE, MOVE_TO_GESTATION, MOVE_IN]
    return [
        {"kind": kind, "tasks": sorted(buckets[kind], key=lambda t: (t.due, t.ear_tag))}
        for kind in order if kind in buckets
    ]


# 自訂工作的重複規則。刻意只有這三種 —— 「每 N 天」「每月第幾個星期二」
# 這類規則要配一整套介面才講得清楚,而牧場實際會用的就是這幾種
# (消毒、疫苗、設備檢查)。
REPEAT_RULES = ("once", "weekly", "monthly")


def _add_months(day: date, months: int) -> date:
    """加幾個月。落在不存在的日子(1/31 + 1 個月)時退到當月最後一天。

    直接算 day.replace(month=...) 會在 2 月 30 日這種日子拋 ValueError,
    而「每月 31 號消毒」是完全合理的設定 —— 不能因此炸掉整個工作清單。
    """
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    # 下個月的第一天往回退一天 = 這個月的最後一天
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return date(year, month, min(day.day, last.day))


def custom_task_dates(task: dict, week_start: date, week_end: date) -> List[date]:
    """這件自訂工作在這一週會發生的日期。

    起始日之前不算 —— 設定「從下個月開始每週消毒」時,這個月不該冒出來。

    回傳清單而不是單一日期:一週剛好跨到兩次的規則(例如每月 1 號與
    31 號之間的月份邊界)不該只算到一次。實務上 weekly/monthly 一週最多
    一次,但讓呼叫端不必假設這件事。
    """
    start = task["start_date"]
    rule = task.get("repeat_rule") or "once"
    if start > week_end:
        return []

    if rule == "once":
        return [start] if week_start <= start <= week_end else []

    if rule == "weekly":
        # 對齊到同一個星期幾,再往前推到這一週
        offset = (week_start - start).days % 7
        first = week_start + timedelta(days=(7 - offset) % 7)
        return [first] if first <= week_end else []

    if rule == "monthly":
        out = []
        # 從起始日所在的月份往後掃,最多掃到週末所在月份的下一個月
        cursor = start
        while cursor <= week_end:
            if cursor >= week_start:
                out.append(cursor)
            cursor = _add_months(start, (cursor.year - start.year) * 12
                                 + cursor.month - start.month + 1)
        return out

    return []      # 不認得的規則不猜,寧可不顯示也不要顯示錯的


def build_custom_tasks(tasks: Iterable[dict], done: Iterable[dict],
                       week_start: date, week_end: date) -> List[dict]:
    """這一週的自訂工作,含每一次是否已完成。

    **與系統推算的工作分開回傳**(已確認的設計決定)—— 推算出來的是
    系統依生產週期算的,自訂的是牧場自己排的,混在一起使用者分不出
    哪些是系統說的、哪些是自己設的。
    """
    marked = {(d["task_id"], d["due_date"]) for d in done}
    out = []
    for task in tasks:
        for due in custom_task_dates(task, week_start, week_end):
            out.append({
                "id": task["id"],
                "name": task["name"],
                "repeat": task.get("repeat_rule") or "once",
                "due": due,
                "done": (task["id"], due) in marked,
            })
    out.sort(key=lambda t: (t["due"], t["name"]))
    return out


def overdue_sows(sows: Iterable[dict], events: Iterable[dict], today: date,
                 settings: Optional[dict] = None) -> List[dict]:
    """離乳或驗孕陰性後太久沒有下一步動作的母豬。

    這是**提醒**不是工作 —— 工作清單列的是「這週該做的」,這裡列的是
    「已經拖太久的」。分開才不會讓逾期的個案混進正常批次裡被忽略。
    """
    cfg = settings_with_defaults(settings)
    grouped = _by_sow(events)
    out = []

    for sow in sows:
        if sow.get("status") not in (None, "active"):
            continue
        rows = grouped.get(sow["id"], [])
        if not rows:
            continue
        c = current_cycle(rows)
        if c["exited"] or c["mate"] or c["farrow"]:
            continue

        last = max([d for d in (c["wean"], c["preg_check"]) if d], default=None)
        if last is None:
            last = rows[-1]["event_date"]
        days = (today - last).days
        if days > cfg["open_sow_alert_days"]:
            out.append({"sow_id": sow["id"], "ear_tag": sow.get("ear_tag", ""),
                        "days": days, "since": last})

    out.sort(key=lambda r: -r["days"])
    return out


def _born_alive_series(rows: List[dict]) -> List[int]:
    """這頭母豬歷次分娩的活仔數,依時間排列。

    只取有填的:PigCHAMP 的分娩記錄偶爾沒有仔數欄位,把缺值當 0 會憑空
    造出一次「活仔 0」的慘況,讓她被誤判成表現崩壞。
    """
    out = []
    for e in rows:
        if e["event_type"] != FARROW:
            continue
        value = (e.get("detail") or {}).get("born_alive")
        if isinstance(value, int):
            out.append(value)
    return out


def _decline_streak(series: List[int]) -> int:
    """從最後一胎往回數,連續嚴格下滑幾次。"""
    streak = 0
    for i in range(len(series) - 1, 0, -1):
        if series[i] < series[i - 1]:
            streak += 1
        else:
            break
    return streak


def _npd_per_litter(rows: List[dict], cfg: dict) -> Optional[float]:
    """每胎的非生產天數(估)。

    用「首末胎間隔 ÷ 胎數 − 懷孕 − 哺乳」回推,而不是逐段累加:逐段累加
    需要每一次配種與離乳都完整,而實際記錄常有缺漏,缺一筆就整個算歪。
    間隔法只需要頭尾兩次分娩,對缺漏穩健得多。

    不足兩胎回 None —— 沒有間隔可算,不可以拿 0 充數。
    """
    farrows = [e["event_date"] for e in rows if e["event_type"] == FARROW]
    if len(farrows) < 2:
        return None
    span = (farrows[-1] - farrows[0]).days
    interval = span / (len(farrows) - 1)
    return interval - cfg["gestation_days"] - cfg["lactation_days"]


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """第 pct 百分位(最近排名法)。空清單回 None,不回 0。"""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * pct / 100)))
    return ordered[index]


# 母豬卡的生產表現項目。`higher_better` 決定分級方向 —— 死胎率跟其他項
# 相反,漏掉的話最會生的那幾頭會被標成待改善。
PERFORMANCE_METRICS = (
    ("total_born", True),      # 窩均總仔數
    ("born_alive", True),      # 窩均活仔數
    ("weaned", True),          # 平均離乳數
    ("litters_per_year", True),
    ("stillborn_rate", False), # 死胎率:越低越好
    ("lactation_days", True),
    ("repeat_estrus", False),  # 重發情次數:越少越好
)

# 場內分級至少要幾頭母豬才有意義。頭數太少時三分位只是把三頭豬各給一級,
# 那不是評價,是排名(值得檢視的活仔數判準踩過同一個坑)。
MIN_HERD_FOR_TIERS = 10


# 死胎率要先高到這個程度,才值得提「集中在最早那一胎」。低於這個數字時
# 整體本來就不算問題,再拆解只是雜訊。
FIRST_LITTER_NOTE_MIN_RATE = 10.0


def _stillborn_rate(farrows: List[dict]) -> Optional[float]:
    """這幾窩的死胎率(%)。缺欄位的窩不計入,不當成 0。"""
    alive = still = mummy = 0
    counted = 0
    for e in farrows:
        d = e.get("detail") or {}
        if not isinstance(d.get("stillborn"), int) or not isinstance(d.get("born_alive"), int):
            continue
        alive += d["born_alive"]
        still += d["stillborn"]
        mummy += d.get("mummified") if isinstance(d.get("mummified"), int) else 0
        counted += 1
    total = alive + still + mummy
    return still / total * 100 if counted and total else None


def sow_performance(events: List[dict]) -> Optional[dict]:
    """一頭母豬的累計生產表現。沒有分娩記錄回 None —— 沒生過就沒有表現
    可談,補一組 0 會讓她在場內比較裡把別人的分級一起拉歪。

    **不算離乳前死亡率。** 仔豬會在窩間流動(5,190 組分娩→離乳配對裡有
    25.3% 離乳數大於活仔數),`(活仔−離乳)/活仔` 對個別母豬無意義。
    這個坑已經踩過一次,有測試把關(specs/v2-facts.md 第 1 條)。
    """
    farrows = [e for e in events if e["event_type"] == FARROW]
    if not farrows:
        return None

    def total(key):
        return sum(v for e in farrows
                   if isinstance(v := (e.get("detail") or {}).get(key), int))

    def counted(key):
        return sum(1 for e in farrows
                   if isinstance((e.get("detail") or {}).get(key), int))

    alive_n, still_n, mummy_n = counted("born_alive"), counted("stillborn"), counted("mummified")
    alive, still, mummy = total("born_alive"), total("stillborn"), total("mummified")

    out = {"litters": len(farrows)}
    out["born_alive"] = alive / alive_n if alive_n else None

    # 總仔數只在活仔與死胎都有記錄時才算 —— 缺一項就當 0 會少算,
    # 而少算的窩看起來只是「比較小窩」,沒有人會發現數字是壞的。
    out["total_born"] = ((alive + still + mummy) / alive_n) if alive_n and still_n else None
    out["stillborn_rate"] = (still / (alive + still + mummy) * 100
                             if (alive + still + mummy) and still_n else None)

    weans = [e for e in events if e["event_type"] == WEAN]
    weaned = [v for e in weans
              if isinstance(v := (e.get("detail") or {}).get("weaned"), int)]
    out["weaned"] = sum(weaned) / len(weaned) if weaned else None

    # 年產胎數:用首末胎的間隔回推,不用「胎數 ÷ 在場年數」——
    # 後者會把她進場後還沒配種的那段也算進去,新母豬因此永遠很難看。
    if len(farrows) >= 2:
        span = (farrows[-1]["event_date"] - farrows[0]["event_date"]).days
        out["litters_per_year"] = (len(farrows) - 1) * 365 / span if span else None
    else:
        out["litters_per_year"] = None

    # 死胎全部集中在最早那一胎的情形。實測 2580:整體 17.1%,排除最早那胎
    # 只有 3.2%(14 隻裡 11 隻死胎都在那一窩,之後六胎再無死胎)。不講的話
    # 她的死胎率會被當成長期問題,但那其實是一次性的難產。
    #
    # 措辭是「最早記錄的那一胎」而不是「第 1 胎」—— 匯入的歷史不保證從
    # 她的頭胎開始,宣稱是第 1 胎會是編的。
    out["stillborn_note"] = None
    if len(farrows) >= 3 and out["stillborn_rate"] is not None:
        rest = _stillborn_rate(farrows[1:])
        if (out["stillborn_rate"] >= FIRST_LITTER_NOTE_MIN_RATE
                and rest is not None and rest * 2 < out["stillborn_rate"]):
            out["stillborn_note"] = {"overall": out["stillborn_rate"], "without_first": rest}

    # 哺乳天數:逐次配對分娩與其後的第一次離乳
    spans = []
    for f in farrows:
        nxt = next((w["event_date"] for w in weans
                    if w["event_date"] >= f["event_date"]), None)
        if nxt:
            spans.append((nxt - f["event_date"]).days)
    out["lactation_days"] = sum(spans) / len(spans) if spans else None

    # 重發情次數:驗孕結果為陰性的次數(使用者明確定義 —— 陰性一次就算
    # 一次重發情,不必推論配種批次後面有沒有接著分娩)。用她完整的事件
    # 記錄算,不是只看目前這一胎,才算得出終身總次數。
    out["repeat_estrus"] = sum(
        1 for e in events
        if e["event_type"] == PREG_CHECK and (e.get("detail") or {}).get("positive") is False
    )

    return out


def _mating_attempts(sow_id: int, events: List[dict]) -> List[dict]:
    """把一頭母豬的完整配種史切成一次次嘗試:從一次配種到下一次配種
    (不含)之間發生的事都算這一次的結果。

    跟 current_cycle 不一樣 —— 那裡只看她**目前**這一輪,這裡要看她
    **一輩子**配過的每一次,不論配的公豬換過幾頭,才算得出公豬的
    終身配種績效。
    """
    attempts: List[dict] = []
    cur: Optional[dict] = None
    for e in events:
        code = e["event_type"]
        if code == MATE:
            if cur is not None:
                attempts.append(cur)
            cur = {"sow_id": sow_id,
                  "boar_tag": (e.get("detail") or {}).get("boar_tag") or "",
                  "date": e["event_date"], "positive": None,
                  "farrowed": False, "born_alive": None}
        elif cur is not None:
            if code == PREG_CHECK:
                pos = (e.get("detail") or {}).get("positive")
                if pos is not None:
                    cur["positive"] = pos
            elif code == FARROW:
                cur["farrowed"] = True
                v = (e.get("detail") or {}).get("born_alive")
                if isinstance(v, int):
                    cur["born_alive"] = v
    if cur is not None:
        attempts.append(cur)
    return attempts


def boar_performance(boar_tag: str, events: Iterable[dict]) -> Optional[dict]:
    """一頭公豬的配種績效。從母豬那邊的配種記錄比對公豬耳號算出來的
    ——MT 事件本來就記了 boar_tag,不必另外猜。

    **只用有結果的嘗試算比率**:還沒驗孕、還沒到預產期的最新一次配種
    結果未知,不能當失敗算(憲法第三條)。`checked`/`litters` 各自的
    分母只算真的有那項結果的嘗試,不是全部配種次數。
    """
    if not boar_tag:
        return None

    attempts = [a for sow_id, evs in _by_sow(events).items()
               for a in _mating_attempts(sow_id, evs) if a["boar_tag"] == boar_tag]
    if not attempts:
        return None

    checked = [a for a in attempts if a["positive"] is not None]
    farrowed = [a for a in attempts if a["farrowed"]]
    litters = [a["born_alive"] for a in farrowed if a["born_alive"] is not None]

    return {
        "matings": len(attempts),
        "sowsMated": len({a["sow_id"] for a in attempts}),
        "checked": len(checked),
        "positiveRate": (sum(1 for a in checked if a["positive"]) / len(checked) * 100
                         if checked else None),
        "litters": len(farrowed),
        "avgBornAlive": sum(litters) / len(litters) if litters else None,
    }


def tier_within_farm(value: Optional[float], peers: List[float],
                     higher_better: bool) -> Optional[str]:
    """把一個數字放進場內的三級:good / mid / poor。

    **與同場其他母豬比,不與全國常模比**(已確認的設計決定)。全國常模是
    場級指標 —— 拿一頭母豬去對照整場的年報數字,是拿不同單位的東西相比。
    而且門檻寫死就只為某一場服務,別的牧場匯進來不是全綠就是全紅。

    分不出高下時回 None 而不是硬給一級:全場數字一模一樣、或頭數太少時,
    三分位只是把母豬排名,那不是評價。
    """
    if value is None or len(peers) < MIN_HERD_FOR_TIERS:
        return None

    ordered = sorted(peers)
    low = _percentile(ordered, 100 / 3)
    high = _percentile(ordered, 200 / 3)
    if low is None or high is None or low == high:
        return None                # 分布沒有差異,不分級

    if not higher_better:
        low, high = high, low
        if value <= low:
            return "good"
        return "poor" if value > high else "mid"

    if value >= high:
        return "good"
    return "poor" if value < low else "mid"


def performance_with_tiers(sow_id: int, sows: Iterable[dict],
                           grouped: Dict[int, List[dict]]) -> Optional[dict]:
    """一頭母豬的生產表現,附上她在場內的級距。

    比較基準包含**已離群(死亡/淘汰)的母豬**,不是只看目前在場的。
    只拿在場母豬當比較基準的話,表現最差的那批（往往正是被淘汰的原因）
    一離群就從分母消失,活下來的人會顯得比實際情況好看,級距因此愈算
    愈寬鬆。要比多少頭由呼叫端決定(見 server.py `_sow_detail`),這裡
    不再自己過濾狀態。
    """
    mine = sow_performance(grouped.get(sow_id, []))
    if mine is None:
        return None

    others = {}
    for sow in sows:
        stats = sow_performance(grouped.get(sow["id"], []))
        if stats:
            for key, _ in PERFORMANCE_METRICS:
                if stats.get(key) is not None:
                    others.setdefault(key, []).append(stats[key])

    return {
        "litters": mine["litters"],
        "stillborn_note": mine.get("stillborn_note"),
        "metrics": [
            {"key": key, "value": mine.get(key),
             "tier": tier_within_farm(mine.get(key), others.get(key, []), higher)}
            for key, higher in PERFORMANCE_METRICS
        ],
    }


def sows_worth_review(sows: Iterable[dict], events: Iterable[dict], today: date,
                      settings: Optional[dict] = None) -> List[dict]:
    """**值得檢視**的母豬 —— 措辭刻意不是「建議淘汰」。

    這個場實際的淘汰原因裡,「年齡太大」佔 48.0%,而「生產性能差」只佔
    2.9%(specs/v2-facts.md 第 10 條)。系統算得出來的正好是最少被拿來
    當決策依據的那一項,所以這份名單只陳述事實與依據,決定權在牧場主
    (憲法第三條第 6 款)。

    每一頭都附上 `reasons`,講明白是憑什麼列進來的 —— 只給一份名單而不
    給理由,使用者無從判斷該不該採信。

    活仔數用**場內百分位**而不是固定數字:已確認的設計是母豬與同場其他
    母豬比,而且門檻寫死就只為這一場服務,別的牧場匯進來會全軍覆沒或
    一頭都不列。

    百分位的比較基準包含**已離群(死亡/淘汰)的母豬**:她們往往正是
    表現差才離群的,拿掉她們只留下場面較好的在場母豬,門檻會愈墊愈高,
    愈來愈難有人被標出來。**但只有在場的母豬會被列進最終名單** ——
    她才有「接下來要不要繼續留」這個決定可做,已經離群的母豬列出來
    也沒有意義。
    """
    cfg = settings_with_defaults(settings)
    grouped = _by_sow(events)

    live = [s for s in sows if s.get("status") in (None, "active")]

    # 先算出全場的活仔數分布,才有得比。每頭母豬取她自己的平均,
    # 不是把所有窩混在一起 —— 否則多產的母豬會主導整個分布。
    # 用 `sows`(含離群的)而非 `live`,理由見上。
    averages: Dict[int, float] = {}
    for sow in sows:
        series = _born_alive_series(grouped.get(sow["id"], []))
        if len(series) >= cfg["review_min_litters"]:
            averages[sow["id"]] = sum(series) / len(series)
    cutoff = _percentile(list(averages.values()), cfg["review_low_alive_pct"])

    out = []
    for sow in live:
        rows = grouped.get(sow["id"], [])
        series = _born_alive_series(rows)
        if len(series) < cfg["review_min_litters"]:
            continue          # 一兩胎看不出趨勢,列出來只是雜訊

        reasons = []

        streak = _decline_streak(series)
        if streak >= cfg["review_decline_litters"]:
            reasons.append({
                "code": "decline",
                "detail": f"活仔數連續 {streak} 胎下滑({'→'.join(map(str, series[-streak - 1:]))})",
            })

        npd = _npd_per_litter(rows, cfg)
        if npd is not None and npd >= cfg["review_npd_days"]:
            reasons.append({
                "code": "npd",
                "detail": f"每胎非生產天數約 {round(npd)} 天,超過 {cfg['review_npd_days']} 天",
            })

        # **嚴格小於**,而且全場要夠多頭才比。
        #
        # 百分位天生會標出後 N% —— 就算全場一模一樣、或全部都很優秀,
        # 照樣有人被列出來。測試抓到兩種情況:整場都是 8 隻時 11 頭全被
        # 標(average <= cutoff 對每一頭都成立),以及只有一頭母豬時她自己
        # 就是後 10%。改成嚴格小於,兩種情況都自然消失:分不出高下時
        # 就不分。頭數太少時百分位只是「最小值」,標出來沒有意義。
        average = averages.get(sow["id"])
        if (cutoff is not None and average is not None
                and len(averages) >= cfg["review_min_herd"]
                and average < cutoff):
            reasons.append({
                "code": "low_alive",
                "detail": f"平均活仔 {average:.1f} 隻,落在全場最低 "
                          f"{cfg['review_low_alive_pct']}%(場內門檻 {cutoff:.1f} 隻)",
            })

        if reasons:
            out.append({
                "sow_id": sow["id"],
                "ear_tag": sow.get("ear_tag", ""),
                "parity": sow.get("parity") or 0,
                "litters": len(series),
                "reasons": reasons,
            })

    # 理由多的排前面;同樣多則胎次高的排前面(年齡本來就是最常見的
    # 淘汰原因,牧場主會想先看那幾頭)。
    out.sort(key=lambda r: (-len(r["reasons"]), -r["parity"], r["ear_tag"]))
    return out


def pen_pressure(sows: Iterable[dict], events: Iterable[dict], pens: Iterable[dict],
                 today: date, settings: Optional[dict] = None) -> dict:
    """產房空間是否夠用。

    **總欄數是使用者在設定裡自己填的「總產房數」**(使用者決定)——
    不是算出來的。這裡曾經拿 pens 資料表裡「已經被移欄記錄提到的欄位
    名稱」數量當總數,但那份清單只會累積曾經打過的編號,牧場實際的
    總欄數在還沒被記錄過之前不會出現在清單裡,用清單長度當總數會
    系統性低估。

    **佔用**仍然來自真實的欄位指派(`sows.pen_id` → 產房區的
    `pens`,由 MOVE_PEN 事件維護,見 server.py),不是猜的。

    **沒有設定總產房數時不宣稱空間不足**。不知道容量時憑空給一個
    警示是捏造的(憲法第三條)。
    """
    cfg = settings_with_defaults(settings)
    grouped = _by_sow(events)

    total = cfg["farrowing_pens"]
    configured = total > 0

    farrowing_pen_ids = {p["id"] for p in pens
                         if p.get("zone", ZONE_FARROWING) == ZONE_FARROWING}
    occupied = sum(1 for s in sows if s.get("status") in (None, "active")
                  and s.get("pen_id") in farrowing_pen_ids)
    free = max(0, total - occupied)

    horizon = today + timedelta(days=cfg["pre_farrow_move_days"])
    incoming = 0
    for sow in sows:
        if sow.get("status") not in (None, "active"):
            continue
        c = current_cycle(grouped.get(sow["id"], []))
        if c["mate"] and not c["farrow"]:
            due = c["mate"] + timedelta(days=cfg["gestation_days"]
                                        - cfg["pre_farrow_move_days"])
            if today <= due <= horizon:
                incoming += 1

    return {
        "configured": configured,
        "total": total,
        "occupied": occupied,
        "free": free,
        "incoming": incoming,
        "short_by": max(0, incoming - free) if configured else 0,
    }


# 生產月報的 12 項指標,依 specs 的計畫文件列出的順序。
MONTH_REPORT_METRICS = (
    "mating_rate", "conception_rate", "farrowing_rate",
    "total_born_per_litter", "born_alive_per_litter",
    "mummification_rate", "stillbirth_rate",
    "weaned_per_litter", "lactation_days",
    "psy", "cull_rate", "mortality_rate",
)


def month_bounds(year: int, month: int) -> Tuple[date, date]:
    """該年月的起訖日期(該月第一天到最後一天)。"""
    start = date(year, month, 1)
    end = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return start, end


def _in_herd_on(sow: dict, sow_events: List[dict], on_date: date) -> bool:
    """這頭母豬在指定的這一天是不是在場(進場之後、還沒離群)。

    entry_date 缺的話退回她自己最早一筆事件的日期 —— 歷史資料不一定有
    進場記錄,用「今天」當進場日會讓一頭早就在場的母豬看起來是剛到的
    (跟 importer.py 對公豬進場日的處理是同一個理由)。
    """
    entry = sow.get("entry_date")
    if entry is None:
        entry = sow_events[0]["event_date"] if sow_events else None
    if entry is None or entry > on_date:
        return False
    return not any(e["event_type"] in EXIT_EVENTS and e["event_date"] <= on_date
                   for e in sow_events)


def _avg_herd_size(sows: Iterable[dict], grouped: Dict[int, List[dict]],
                   start: date, end: date) -> float:
    """月初、月底在場頭數的平均,估這段期間的平均在群母豬數。"""
    sows = list(sows)
    at_start = sum(1 for s in sows if _in_herd_on(s, grouped.get(s["id"], []), start))
    at_end = sum(1 for s in sows if _in_herd_on(s, grouped.get(s["id"], []), end))
    return (at_start + at_end) / 2


def monthly_report(sows: Iterable[dict], events: Iterable[dict],
                   start: date, end: date, settings: Optional[dict] = None) -> dict:
    """生產月報,12 項指標,即時重算不存快照 —— 事件記錄本身才是唯一
    真相,存快照的話事後補登或修正舊記錄不會反映到已經算過的月份。

    兩個容易算錯的分母:

    1. **分娩率**:當月分娩的窩對應大約 gestation_days 天前的配種,
       不是當月配種 —— 配種量一波動,拿當月配種當分母會讓分娩率出現
       跟真實表現無關的假跳動。

    2. **PSY / 母豬淘汰率 / 母豬死亡率**:業界慣例是年化數字,不是當月
       原始比率 —— 用當月數字乘以 365.25 / 當月天數換算成年率,樣本
       只有一個月,數字本來就會比全年平均噪一些。

    每一項都各自看有沒有底層記錄,**沒有就回 None,不補 0 也不猜**
    (憲法第三條)。`n` 依各指標自然的樣本數而定(驗孕次數、對應窗口內
    的配種次數、記錄到的窩數……),不是統一的意義。
    """
    cfg = settings_with_defaults(settings)
    sows = list(sows)
    grouped = _by_sow(events)
    days_in_month = (end - start).days + 1
    annualize = 365.25 / days_in_month
    herd = _avg_herd_size(sows, grouped, start, end)

    by_type: Dict[str, List[dict]] = {}
    for e in events:
        if e.get("excluded"):
            continue
        by_type.setdefault(e["event_type"], []).append(e)

    def in_range(code, lo, hi):
        return [e for e in by_type.get(code, []) if lo <= e["event_date"] <= hi]

    mt_month = in_range(MATE, start, end)
    fw_month = in_range(FARROW, start, end)
    wn_month = in_range(WEAN, start, end)
    pd_month = in_range(PREG_CHECK, start, end)
    sal_month = in_range(CULL, start, end)
    dth_month = in_range(DEATH, start, end)
    mt_for_farrowing = in_range(MATE, start - timedelta(days=cfg["gestation_days"]),
                                end - timedelta(days=cfg["gestation_days"]))

    def rate(numerator: int, denominator) -> dict:
        value = numerator / denominator * 100 if denominator else None
        return {"value": value, "n": denominator}

    checked = [e for e in pd_month
              if isinstance((e.get("detail") or {}).get("positive"), bool)]
    positive = sum(1 for e in checked if e["detail"]["positive"])

    litters = []
    for e in fw_month:
        d = e.get("detail") or {}
        if isinstance(d.get("born_alive"), int) and isinstance(d.get("stillborn"), int):
            mummy = d.get("mummified") if isinstance(d.get("mummified"), int) else 0
            litters.append((d["born_alive"], d["stillborn"], mummy))

    alive_sum = sum(x[0] for x in litters)
    still_sum = sum(x[1] for x in litters)
    mummy_sum = sum(x[2] for x in litters)
    total_born_sum = alive_sum + still_sum + mummy_sum

    weaned_vals = []
    for e in wn_month:
        v = (e.get("detail") or {}).get("weaned")
        if isinstance(v, int):
            weaned_vals.append(v)
    weaned_total = sum(weaned_vals)

    # 哺乳天數:每一筆離乳配對她自己在那之前最近一次分娩 —— 跟
    # current_cycle() 抓「上一胎」同樣的道理,只是這裡要看整段歷史,
    # 不是只看目前這一輪。
    lactation_spans = []
    for e in wn_month:
        prior_farrows = [x["event_date"] for x in grouped.get(e["sow_id"], [])
                         if x["event_type"] == FARROW and x["event_date"] <= e["event_date"]]
        if prior_farrows:
            lactation_spans.append((e["event_date"] - max(prior_farrows)).days)

    def avg(values):
        return sum(values) / len(values) if values else None

    metrics = {
        "mating_rate": rate(len(mt_month), round(herd) if herd else 0),
        "conception_rate": rate(positive, len(checked)),
        "farrowing_rate": rate(len(fw_month), len(mt_for_farrowing)),
        "total_born_per_litter":
            {"value": avg([sum(x) for x in litters]), "n": len(litters)},
        "born_alive_per_litter": {"value": avg([x[0] for x in litters]), "n": len(litters)},
        "mummification_rate":
            {"value": mummy_sum / total_born_sum * 100 if total_born_sum else None,
             "n": len(litters)},
        "stillbirth_rate":
            {"value": still_sum / total_born_sum * 100 if total_born_sum else None,
             "n": len(litters)},
        "weaned_per_litter": {"value": avg(weaned_vals), "n": len(weaned_vals)},
        "lactation_days": {"value": avg(lactation_spans), "n": len(lactation_spans)},
        "psy": {"value": weaned_total / herd * annualize if herd else None,
               "n": len(wn_month)},
        "cull_rate": {"value": len(sal_month) / herd * 100 * annualize if herd else None,
                     "n": len(sal_month)},
        "mortality_rate": {"value": len(dth_month) / herd * 100 * annualize if herd else None,
                          "n": len(dth_month)},
    }

    return {"start": start, "end": end, "herdSize": herd, "metrics": metrics}
