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
}

# 事件代碼(沿用 PigCHAMP,匯入才對得起來)
MATE, PREG_CHECK, FARROW, WEAN = "MT", "PD", "FW", "WN"
PIGLET_LOSS, DEATH, CULL, ABORT = "PL", "DTH", "SAL", "AB"
EXIT_EVENTS = (DEATH, CULL)

# 工作類型
MOVE_IN, INDUCE, FARROW_DUE, WEAN_DUE, MATE_DUE, CHECK_DUE = (
    "move_in", "induce", "farrow", "wean", "mate", "preg_check")


class Task(NamedTuple):
    """一件待辦。`why` 說明「為什麼是這天」,畫面上要顯示 —— 使用者才知道
    系統憑什麼這樣算,而不是看到一個沒有理由的名單。
    """
    kind: str
    sow_id: int
    ear_tag: str
    due: date
    why: str


def settings_with_defaults(settings: Optional[dict] = None) -> dict:
    merged = dict(DEFAULTS)
    if settings:
        merged.update({k: v for k, v in settings.items() if v is not None})
    return merged


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
