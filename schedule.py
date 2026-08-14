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
from typing import Dict, Iterable, List, NamedTuple, Optional

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

# 事件代碼(沿用 PigCHAMP,匯入才對得起來)
MATE, PREG_CHECK, FARROW, WEAN = "MT", "PD", "FW", "WN"
PIGLET_LOSS, DEATH, CULL, ABORT = "PL", "DTH", "SAL", "AB"
EXIT_EVENTS = (DEATH, CULL)

# 工作類型
MOVE_IN, INDUCE, FARROW_DUE, WEAN_DUE, MATE_DUE, CHECK_DUE = (
    "move_in", "induce", "farrow", "wean", "mate", "preg_check")

# 可以記錄的事件代碼。server.py 用它擋掉不認得的類型 ——
# 前端送什麼過來都不可信(憲法第四條)。
KNOWN_EVENTS = frozenset({
    MATE, PREG_CHECK, FARROW, WEAN, PIGLET_LOSS,
    DEATH, CULL, ABORT, "GA", "FON", "FOF",
})


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

    **配種後未驗孕不等於懷孕。** 分成「配種待驗孕」與「懷孕中」兩種狀態:
    這個場目前有 50 頭驗孕陰性,把她們一律當懷孕會排出永遠不會發生的
    分娩工作(先前分娩預測平均誤差被拉到 +8.9 天就是這個原因)。
    但預產期兩種狀態都給 —— 還沒驗孕的那幾天照樣要準備產房。
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
        state = "pregnant" if c["preg_positive"] else "mated"
        # 預產日過了卻沒有分娩記錄:她要嘛生了沒登記,要嘛沒保住。
        # 兩種都需要有人去看,所以不能把一個已經過去的日期當成未來的
        # 「預產」顯示 —— 實測 200 頭裡有 88 頭的預產日已過,最久的過了
        # 611 天,那樣的畫面等於在說謊。
        overdue = (today - due).days
        result = {"state": state, "since": c["mate"],
                  "day": (today - c["mate"]).days, "due": due,
                  "overdue_days": overdue if overdue > 0 else None,
                  "move_in_due": due - timedelta(days=cfg["pre_farrow_move_days"])}

        # 「配種待驗孕」本身就代表還沒確認懷孕 —— 但這件事原本只出現在
        # 狀態列,時間軸裡完全看不出來:2580 配種 143 天、從沒驗孕過,
        # 時間軸裡就是少了一列「驗孕」,使用者得自己數才會發現。
        # 這裡把「該驗孕了」這件事變成明確的資料,畫面才有東西可以顯示。
        if state == "mated":
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

    order = [INDUCE, FARROW_DUE, WEAN_DUE, MATE_DUE, CHECK_DUE, MOVE_IN]
    return [
        {"kind": kind, "tasks": sorted(buckets[kind], key=lambda t: (t.due, t.ear_tag))}
        for kind in order if kind in buckets
    ]


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

    return out


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
    """一頭母豬的生產表現,附上她在場內的級距。"""
    mine = sow_performance(grouped.get(sow_id, []))
    if mine is None:
        return None

    others = {}
    for sow in sows:
        if sow.get("status") not in (None, "active"):
            continue
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
    """
    cfg = settings_with_defaults(settings)
    grouped = _by_sow(events)

    live = [s for s in sows if s.get("status") in (None, "active")]

    # 先算出全場的活仔數分布,才有得比。每頭母豬取她自己的平均,
    # 不是把所有窩混在一起 —— 否則多產的母豬會主導整個分布。
    averages: Dict[int, float] = {}
    for sow in live:
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


def pen_pressure(sows: Iterable[dict], events: Iterable[dict], pens: List[dict],
                 today: date, settings: Optional[dict] = None) -> dict:
    """產房空間是否夠用。

    逐欄位追蹤(使用者選擇),所以除了「夠不夠」還回傳**還空著哪幾欄** ——
    只給一個「不足」的布林值,牧場主還是得自己去數。
    """
    cfg = settings_with_defaults(settings)
    grouped = _by_sow(events)

    occupied = {s["pen_id"] for s in sows if s.get("pen_id")}
    free = [p for p in pens if p["id"] not in occupied]

    horizon = today + timedelta(days=cfg["pre_farrow_move_days"])
    incoming = 0
    for sow in sows:
        if sow.get("status") not in (None, "active") or sow.get("pen_id"):
            continue
        c = current_cycle(grouped.get(sow["id"], []))
        if c["mate"] and not c["farrow"]:
            due = c["mate"] + timedelta(days=cfg["gestation_days"]
                                        - cfg["pre_farrow_move_days"])
            if today <= due <= horizon:
                incoming += 1

    return {
        "total": len(pens),
        "occupied": len(occupied),
        "free": [{"id": p["id"], "name": p["name"]} for p in free],
        "incoming": incoming,
        "short_by": max(0, incoming - len(free)),
    }
