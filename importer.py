"""PigCHAMP 匯出檔的解析與匯入。

**做成通用功能,不是為單一牧場寫死** —— 產品要給其他牧場主用,任何人都能
上傳自己的匯出檔。因此解析與驗證是純函式(可離線測試),寫入資料庫是另一
個函式,兩者分開。

不碰 HTTP(在 server.py)。這裡只有格式知識。

## 已知的坑

**編碼混雜**:`.txt` 是 UTF-8,同一批匯出的 CSV 卻是 Big5(cp950)。
用錯編碼**不會報錯**,只會產生看起來像資料損壞的假象 —— 檔案裡有 56 個
ID 含中文字(`L文`、`L鄭`、`D謝-112/10/02`),誤讀時中文字被拆壞並吃掉
分隔符,讓 ID 與事件代碼看起來黏在一起。這曾經被誤判成「5 筆欄位錯位」,
寫進規格當成必須處理的風險,實際上並不存在
(見 specs/v2-facts.md 第 8 條)。

**耳號的離群年份後綴**:`-Dxxx` 是離群當年的民國年(D115=2026)。
匯入時照原樣保留 —— 那是牧場的既有慣例,也是防碰撞機制。
"""

import collections
import json
import re
from datetime import date
from typing import Dict, Iterable, List, NamedTuple, Optional

# 匯出檔的事件代碼 → 內部代碼。目前一對一,但保留這層對應:別的
# PigCHAMP 版本用的代碼可能不同,屆時只要改這裡。
EVENT_CODES = {
    "MT": "MT",    # 配種
    "PD": "PD",    # 驗孕
    "FW": "FW",    # 分娩
    "WN": "WN",    # 離乳
    "PL": "PL",    # 仔豬離乳前損失
    "GA": "GA",    # 後備母豬進場
    "SAL": "SAL",  # 淘汰售賣
    "DTH": "DTH",  # 死亡
    "AB": "AB",    # 流產
    "FON": "FON",  # 寄養移入
    "FOF": "FOF",  # 寄養移出
}
# 公豬專屬,分流進 result.boar_rows(不進 result.rows)。三個代碼裡只有
# SC 真的會寫進 boar_events —— BA 只用來建立公豬身分,SP 不是這個 app
# 認得的事件類型(精蟲活力/濃度已併進 SC 表單),見 import_into() 的說明。
BOAR_CODES = {"BA": "BA", "SC": "SC", "SP": "SP"}
# 明確略過(不是錯誤,是這個版本用不到):HD 發情、SA 其他、RT 轉欄
SKIPPED_CODES = {"HD", "SA", "RT"}

ENCODINGS = ("utf-8-sig", "utf-8", "cp950")


class Row(NamedTuple):
    ear_tag: str
    code: str
    when: date
    detail: dict
    line_no: int


class Anomaly(NamedTuple):
    """可疑但不一定是錯的記錄。使用者逐筆決定要不要納入統計。"""
    line_no: int
    ear_tag: str
    code: str
    when: date
    reason: str


class ParseResult(NamedTuple):
    rows: List[Row]
    boar_rows: List[Row]
    anomalies: List[Anomaly]
    skipped: Dict[str, int]      # 代碼 → 筆數
    bad_lines: List[str]


def decode(raw: bytes) -> str:
    """依序試幾種編碼。

    UTF-8 放前面而非 Big5:UTF-8 解碼失敗會拋例外(多位元組序列有嚴格
    規則),Big5 幾乎什麼位元組都吃得下去 —— 先試 Big5 的話,UTF-8 的檔案
    會被「成功」解成亂碼而不報錯。
    """
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_date(text: str) -> Optional[date]:
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None            # 20240230 這種不存在的日期


def _int(text: str) -> Optional[int]:
    text = (text or "").strip()
    try:
        return int(text)
    except ValueError:
        return None


def _field(parts: List[str], i: int) -> str:
    return parts[i].strip() if len(parts) > i else ""


def _detail_for(code: str, parts: List[str]) -> dict:
    """各事件要留下的欄位。欄位位置對照實際匯出檔確認過。"""
    if code == "MT":
        # 第 4 欄有時段標記(上午/下午),實測 337 筆。不讀的話,同一頭公豬
        # 早上配一次、下午配一次會變成兩筆完全相同的記錄而被判重合併 ——
        # 那是真的兩次配種,不是重複輸入。
        return {"boar_tag": _field(parts, 3), "session": _field(parts, 4)}
    if code == "PD":
        mark = _field(parts, 8)
        return {"positive": True if mark == "+" else False if mark == "-" else None}
    if code == "FW":
        return {"born_alive": _int(_field(parts, 3)),
                "stillborn": _int(_field(parts, 4)),
                "mummified": _int(_field(parts, 5))}
    if code == "WN":
        return {"weaned": _int(_field(parts, 3))}
    if code == "PL":
        return {"count": _int(_field(parts, 3)), "reason": _field(parts, 4)}
    if code in ("GA", "BA"):
        return {"breed": _field(parts, 3), "birth_date": _field(parts, 5),
                "sire_tag": _field(parts, 6), "dam_tag": _field(parts, 7)}
    if code in ("SAL", "DTH"):
        return {"reason": _field(parts, 3)}
    if code in ("FON", "FOF"):
        return {"partner_tag": _field(parts, 3), "count": _int(_field(parts, 4))}
    if code == "SC":
        return {"volume": _int(_field(parts, 3)), "doses": _int(_field(parts, 4))}
    if code == "SP":
        return {"doses": _int(_field(parts, 3)), "note": _field(parts, 4)}
    return {}


# 離群值的判定門檻。刻意寬鬆 —— 目的是抓出打錯的數字,不是質疑牧場的
# 生產成績。這個場 32,814 筆只有 2 筆被抓到。
LIMITS = {
    "max_litter": 25,        # 單窩總仔數
    "max_lactation": 45,     # 哺乳天數
    "max_parity": 15,
}


def _check_row(row: Row) -> Optional[str]:
    d = row.detail
    if row.code == "FW":
        nums = [d.get("born_alive"), d.get("stillborn"), d.get("mummified")]
        if any(n is not None and n < 0 for n in nums):
            return "活仔/死胎/木乃伊出現負數"
        total = sum(n for n in nums if n)
        if total > LIMITS["max_litter"]:
            return f"單窩總仔數 {total} 隻,超過 {LIMITS['max_litter']}"
    if row.code == "WN" and (d.get("weaned") or 0) < 0:
        return "離乳數為負"
    if row.code == "PL" and (d.get("count") or 0) < 0:
        return "仔豬損失數為負"
    return None


def parse(text: str, today: Optional[date] = None) -> ParseResult:
    """把匯出檔解析成可匯入的列。**不寫入任何東西。**

    `today` 由呼叫端傳入(用來判斷未來日期),與 schedule.py 同一個理由:
    模組內不取當下時間,測試才能固定日期斷言。
    """
    rows: List[Row] = []
    boar_rows: List[Row] = []
    bad_lines: List[str] = []
    skipped: Dict[str, int] = collections.Counter()

    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 3:
            bad_lines.append(f"第 {line_no} 行:欄位不足")
            continue

        tag, code = parts[0].strip(), parts[1].strip()
        when = _parse_date(parts[2].strip())
        if not tag or not code:
            bad_lines.append(f"第 {line_no} 行:缺少耳號或事件代碼")
            continue
        if when is None:
            bad_lines.append(f"第 {line_no} 行:日期無法解讀({parts[2].strip()!r})")
            continue

        if code in SKIPPED_CODES:
            skipped[code] += 1
            continue

        target = EVENT_CODES.get(code) or BOAR_CODES.get(code)
        if target is None:
            skipped[code] += 1
            continue

        row = Row(tag, target, when, _detail_for(target, parts), line_no)
        (boar_rows if code in BOAR_CODES else rows).append(row)

    anomalies = _find_anomalies(rows, today)
    return ParseResult(rows, boar_rows, anomalies, dict(skipped), bad_lines)


def _find_anomalies(rows: List[Row], today: Optional[date]) -> List[Anomaly]:
    """逐列與跨列的檢查。跨列的部分(哺乳天數、離乳早於分娩)必須先依
    母豬分組並排序,不能只看單一列。
    """
    found: List[Anomaly] = []

    for row in rows:
        why = _check_row(row)
        if why:
            found.append(Anomaly(row.line_no, row.ear_tag, row.code, row.when, why))
        if today and row.when > today:
            found.append(Anomaly(row.line_no, row.ear_tag, row.code, row.when,
                                 "日期在未來"))

    by_sow: Dict[str, List[Row]] = {}
    for row in rows:
        by_sow.setdefault(row.ear_tag, []).append(row)

    for tag, lst in by_sow.items():
        lst.sort(key=lambda r: r.when)
        last_fw = None
        farrows = 0
        for row in lst:
            if row.code == "FW":
                last_fw = row
                farrows += 1
            elif row.code == "WN":
                if last_fw is None:
                    # 依日期排序後,「離乳早於分娩」會表現成「離乳前找不到
                    # 分娩」—— 直接比日期大小偵測不到,因為排序已經把順序
                    # 調正了。這個檢查同時涵蓋兩種情形。
                    found.append(Anomaly(row.line_no, tag, "WN", row.when,
                                         "離乳前沒有對應的分娩記錄"))
                    continue
                days = (row.when - last_fw.when).days
                if days > LIMITS["max_lactation"]:
                    found.append(Anomaly(row.line_no, tag, "WN", row.when,
                                         f"哺乳 {days} 天,超過 {LIMITS['max_lactation']}"))
                last_fw = None
        if farrows > LIMITS["max_parity"]:
            found.append(Anomaly(lst[-1].line_no, tag, "FW", lst[-1].when,
                                 f"分娩 {farrows} 胎,超過 {LIMITS['max_parity']}"))

    found.sort(key=lambda a: a.line_no)
    return found


def odd_boar_tags(result: ParseResult) -> List[str]:
    """看起來不像耳號的公豬 ID。

    實測這個場的檔案裡有 25 個(共 154 個)長得像民國日期:`109/09/28`、
    `110/07/06` —— 有人把日期填進了公豬 ID 欄位。

    **不修正也不丟掉**,那是使用者的資料。只是講出來:配種記錄要從公豬
    清單裡選,不講的話那 25 個會混在選單裡,使用者只會覺得系統壞了。
    """
    odd = set()
    for row in result.boar_rows:
        tag = row.ear_tag
        # 民國年/月/日,前面可能還帶一兩個字母
        if re.match(r"^[A-Za-z-]{0,2}\d{3}/\d{1,2}/\d{1,2}", tag):
            odd.add(tag)
    return sorted(odd)


def summarize(result: ParseResult) -> dict:
    """給匯入預覽畫面的統計。**上傳後先看這個再確認**,尤其是別的牧場的
    檔案格式可能不同 —— 預覽能在寫入前就看出解析錯誤。
    """
    odd_tags = set(odd_boar_tags(result))
    semen = [r for r in result.boar_rows if r.code == "SC"]
    return {
        "sows": len({r.ear_tag for r in result.rows}),
        "boars": len({r.ear_tag for r in result.boar_rows}),
        "events": len(result.rows),
        # 公豬的**身分**會建起來(配種記錄要選公豬)。採精(SC)事件本身會
        # 寫進 boar_events;精液品質(SP)不寫 —— 精蟲活力/濃度已經併進
        # SC 表單,SP 不再是這個 app 認得的事件類型(使用者決定的範圍,
        # 見 schedule.KNOWN_BOAR_EVENTS)。BA 只用來建立身分,本來就不是
        # 事件。耳號長得像民國日期的 SC 列對不到真公豬,略過並回報筆數。
        "boarEvents": len(result.boar_rows),
        "semenCollections": len(semen),
        "semenCollectionsSkipped": len([r for r in semen if r.ear_tag in odd_tags]),
        "semenQualityRows": len([r for r in result.boar_rows if r.code == "SP"]),
        "oddBoarTags": sorted(odd_tags),
        "byCode": dict(collections.Counter(r.code for r in result.rows)),
        "dateRange": (
            [min(r.when for r in result.rows).isoformat(),
             max(r.when for r in result.rows).isoformat()]
            if result.rows else None
        ),
        "anomalies": [
            {"line": a.line_no, "earTag": a.ear_tag, "code": a.code,
             "date": a.when.isoformat(), "reason": a.reason}
            for a in result.anomalies
        ],
        "skipped": result.skipped,
        "badLines": result.bad_lines[:20],
        "badLineCount": len(result.bad_lines),
    }


def import_into(store, farm_id: int, result: ParseResult,
                exclude_lines: Iterable[int] = (), recorded_by=None) -> dict:
    """把解析結果寫入資料庫。

    **冪等** —— 同一份檔案匯兩次不會產生兩倍資料(事件以
    母豬+類型+日期判重,見 db.py 的 sow_events_dedupe)。

    `exclude_lines` 是使用者在預覽畫面上勾選「不納入統計」的行號。
    那些事件**照樣寫入**,只是標記 excluded —— 不刪使用者的資料,
    日後可以改回來,母豬卡的時間軸也仍看得到。

    整段包在 `store.batch()` 裡 —— 底下對 store 的呼叫動輒上萬次
    (母豬、公豬、事件全部加起來),PostgresStore 沒有這個的話等於
    每一筆都各自連一次資料庫,實測 300 行/198 筆寫入要 17.9 秒,
    推算整份 3.5 萬行的檔案要 50 分鐘,而且逾時被砍斷還會留下寫到
    一半的資料。batch() 借同一條連線重複用,結束時才一次 commit。
    """
    with store.batch() as store:
        return _write_import(store, farm_id, result, set(exclude_lines), recorded_by)


def _write_import(store, farm_id: int, result: ParseResult,
                  excluded: set, recorded_by=None) -> dict:
    entries = {r.ear_tag: r for r in result.rows if r.code == "GA"}

    # 一次撈全場建對照表。不能用 find_sow_by_tag —— 它只找 status='active'
    # 的豬,已淘汰的查不到,重跑匯入時會想再新增一次而撞唯一鍵。
    # (這個 bug 只有拿真實資料重跑才會出現:單元測試裡的母豬都還在場。)
    # 順帶避免每頭各查一次的 N+1。
    existing_by_tag: Dict[str, int] = {}
    for s in store.list_sows(farm_id):
        existing_by_tag.setdefault(s["ear_tag"], s["id"])

    tag_to_id: Dict[str, int] = {}
    for tag in sorted({r.ear_tag for r in result.rows}):
        entry = entries.get(tag)
        detail = entry.detail if entry else {}
        if tag in existing_by_tag:
            tag_to_id[tag] = existing_by_tag[tag]
            continue
        tag_to_id[tag] = store.add_sow(
            farm_id, tag,
            entry_date=entry.when if entry else None,
            birth_date=_parse_date(detail.get("birth_date", "")),
            breed=detail.get("breed", ""),
            sire_tag=detail.get("sire_tag", ""),
            dam_tag=detail.get("dam_tag", ""),
        )

    # 公豬的身分。**豬要先建起來** —— 配種記錄要從公豬清單裡選,少了這一
    # 步,匯入完資料的牧場打開配種表單會看到一個空的選單。(採精事件本身
    # 見下面的區塊;BA 只在這裡用來抓身分,它自己不是事件。)
    #
    # 進場日期取她自己最早那筆事件的日期:檔案沒有公豬的進場記錄,而用
    # 今天當進場日會讓一頭 2020 年就在的公豬看起來是今天剛到的。
    existing_boars = {b["ear_tag"] for b in store.list_boars(farm_id)}
    first_seen: Dict[str, date] = {}
    for row in result.boar_rows:
        seen = first_seen.get(row.ear_tag)
        if seen is None or row.when < seen:
            first_seen[row.ear_tag] = row.when

    boars_added = 0
    for tag, when in sorted(first_seen.items()):
        if tag in existing_boars:
            continue                    # 重跑匯入不重複建(冪等)
        store.add_boar(farm_id, tag, entry_date=when)
        boars_added += 1

    # 公豬的採精(SC)事件寫進 boar_events。精液品質(SP)不寫 —— 現在的
    # 表單/事件類型設計已經把精蟲活力、濃度併進 SC,SP 不再是這個 app
    # 認得的事件類型(schedule.KNOWN_BOAR_EVENTS 沒有它),硬寫進去畫面
    # 也顯示不出名字,是使用者決定的範圍。BA 只用來建立身分,本來就
    # 不是事件。
    #
    # 耳號長得像民國日期的列(odd_boar_tags)對不到真公豬,跳過不寫,
    # 只回報筆數 —— 跟母豬事件的異常一樣,不默默修正也不默默丟掉。
    boar_tag_to_id = {b["ear_tag"]: b["id"] for b in store.list_boars(farm_id)}
    odd_tags = set(odd_boar_tags(result))
    existing_boar_keys = {
        (e["boar_id"], e["event_type"], e["event_date"],
         json.dumps(e["detail"], sort_keys=True, ensure_ascii=False))
        for e in store.list_boar_events(farm_id)
    }

    semen_written = 0
    semen_skipped = 0
    for row in result.boar_rows:
        if row.code != "SC":
            continue
        if row.ear_tag in odd_tags:
            semen_skipped += 1
            continue
        boar_id = boar_tag_to_id[row.ear_tag]
        key = (boar_id, row.code, row.when,
               json.dumps(row.detail, sort_keys=True, ensure_ascii=False))
        if key in existing_boar_keys:
            continue                        # 重跑匯入不重複寫(冪等)
        store.add_boar_event(farm_id, boar_id, row.code, row.when, row.detail,
                             recorded_by=recorded_by)
        existing_boar_keys.add(key)
        semen_written += 1

    # 同一頭豬、同一天、同樣內容的重複行編號。
    #
    # 來源檔案裡合法地存在一模一樣的連續兩行 —— 實測 153 組,其中 101 組是
    # 仔豬損失(同一天死兩隻、死因相同,各記一筆)。若把它們當成重複而合併,
    # 會少算仔豬死亡數,直接影響離乳前死亡率這個最關鍵的指標。
    #
    # 用「第幾次出現」當鍵的一部分,既保住每一筆,重跑同一份檔案時編號也
    # 會一模一樣,冪等仍然成立。
    seen = collections.Counter()
    written = 0
    for row in result.rows:
        key = (row.ear_tag, row.code, row.when,
               json.dumps(row.detail, sort_keys=True, ensure_ascii=False))
        seq = seen[key]
        seen[key] += 1

        event_id = store.add_sow_event(
            farm_id, tag_to_id[row.ear_tag], row.code, row.when,
            row.detail, recorded_by=recorded_by, seq=seq)
        if row.line_no in excluded:
            store.set_event_excluded(farm_id, event_id, True)
        written += 1

    # 匯入後把胎次與狀態補正,否則母豬卡的胎次全是 0。
    #
    # **查一次全場,在記憶體裡分組** —— 早期版本對每頭母豬各查一次,
    # 這個場就是 1,531 次查詢。用 PostgreSQL 等於 1,531 個來回,匯入從
    # 幾秒變成幾十秒,而且是隨牧場規模線性惡化的那種慢。
    per_sow: Dict[int, List[dict]] = {}
    for e in store.list_sow_events(farm_id):
        per_sow.setdefault(e["sow_id"], []).append(e)

    for sow_id in tag_to_id.values():
        events = per_sow.get(sow_id, [])
        fields = {"parity": sum(1 for e in events if e["event_type"] == "FW")}
        exits = [e for e in events if e["event_type"] in ("SAL", "DTH")]
        if exits:
            fields["status"] = "culled" if exits[-1]["event_type"] == "SAL" else "dead"
        store.update_sow(farm_id, sow_id, **fields)

    return {"sows": len(tag_to_id), "events": written, "excluded": len(excluded),
            "boars": boars_added, "semenCollections": semen_written,
            "semenCollectionsSkipped": semen_skipped}


# ── 完整備份的還原 ────────────────────────────────────────────────
#
# 匯出的「完整備份」是這個 app 自己的 JSON,不是 PigCHAMP 的匯出檔。兩者
# 長得完全不一樣,所以 parse() 讀它會逐行失敗 —— 使用者拿自己剛備份出來的
# 檔案想放回去,得到的是「452,266 行無法解析」。那等於把備份做成一扇單向
# 門:存得出來,放不回去,而備份的用途正好就是放回去。
#
# 還原**不走 parse()**,因為資料的來源不同:PigCHAMP 的檔案要猜欄位、要
# 驗證、要挑出可疑值;備份是這個系統自己寫出來的,每個欄位都已經是最終
# 形態,照原樣放回去就是正確答案。硬塞進同一條路徑,等於在還原時重跑一次
# 匯入的清洗邏輯,反而把原本好好的資料改掉。

# 還原認得的事件類型。跟記錄時的白名單分開列:備份裡可能有這個版本的表單
# 已經拿掉、但資料還在的舊事件,那些照樣要放得回去。
KNOWN_RESTORE_EVENTS = frozenset(set(EVENT_CODES.values()) | {"MV", "MKD"})

# 備份至少要有這兩個陣列才算數。少了它們就不是這個系統匯出的東西,
# 與其猜著寫進去,不如講清楚。
BACKUP_KEYS = ("sows", "events")


def looks_like_backup(text: str) -> bool:
    """這份內容是完整備份還是 PigCHAMP 匯出檔?

    只看開頭一個字元,不解析整份 —— 8MB 的檔案光為了分辨格式就
    json.loads 一次太浪費。PigCHAMP 的檔案每一行都是 `耳號|代碼|日期`,
    不可能以 `{` 開頭。
    """
    return text.lstrip()[:1] == "{"


class BackupResult(NamedTuple):
    farm_name: str
    exported_at: str
    sows: List[dict]
    boars: List[dict]
    events: List[dict]
    boar_events: List[dict]
    market_deaths: List[dict]
    pens: List[dict]
    custom_tasks: List[dict]
    settings: dict
    problems: List[str]


def _parse_iso(text) -> Optional[date]:
    if isinstance(text, date):
        return text
    try:
        return date.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def parse_backup(text: str) -> BackupResult:
    """讀一份完整備份。**不寫入任何東西。**

    壞掉的個別欄位收進 problems 而不是拋例外 —— 使用者上傳什麼都不可信
    (憲法第四條),就算它理論上是我們自己寫出來的格式。只有整份檔案根本
    不是備份時才拋,那種情況繼續往下做沒有意義。
    """
    problems = []
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError("這不是一份讀得開的備份檔(%s)" % e)
    if not isinstance(data, dict):
        raise ValueError("這不是一份完整備份(最外層不是物件)")
    missing = [k for k in BACKUP_KEYS if not isinstance(data.get(k), list)]
    if missing:
        raise ValueError("這不是這個系統匯出的完整備份(缺少 %s)"
                         % "、".join(missing))

    def rows(key):
        value = data.get(key)
        if not isinstance(value, list):
            return []
        return [r for r in value if isinstance(r, dict)]

    sows = []
    for s in rows("sows"):
        if str(s.get("earTag") or "").strip():
            sows.append(s)
        else:
            problems.append("有一頭母豬沒有耳號,略過")

    known_ids = {s["id"] for s in sows if isinstance(s.get("id"), int)}
    known_tags = {str(s["earTag"]).strip() for s in sows}
    events = []
    for e in rows("events"):
        if e.get("type") not in KNOWN_RESTORE_EVENTS:
            problems.append("不認得的事件類型 %r,略過" % (e.get("type"),))
        elif _parse_iso(e.get("date")) is None:
            problems.append("日期讀不出來(%r),略過" % (e.get("date"),))
        elif (e.get("sowId") not in known_ids
              and str(e.get("earTag") or "").strip() not in known_tags):
            problems.append("有一筆事件對不到任何一頭母豬,略過")
        else:
            events.append(e)

    settings = data.get("settings")
    return BackupResult(
        farm_name=str(data.get("farmName") or ""),
        exported_at=str(data.get("exportedAt") or ""),
        sows=sows,
        boars=rows("boars"),
        events=events,
        boar_events=[e for e in rows("boarEvents")
                     if _parse_iso(e.get("date")) is not None],
        market_deaths=[d for d in rows("marketDeaths")
                       if _parse_iso(d.get("date")) is not None],
        pens=rows("pens"),
        custom_tasks=rows("customTasks"),
        settings=settings if isinstance(settings, dict) else {},
        problems=problems,
    )


def summarize_backup(result: BackupResult) -> dict:
    """預覽用。刻意跟 summarize() 回同一組鍵 —— 兩者要回答的是同一個問題
    (按下去之後會進來什麼),匯入預覽的畫面因此不必為備份再寫一份。
    """
    dates = [d for d in (_parse_iso(e.get("date")) for e in result.events) if d]
    return {
        "kind": "backup",
        "farmName": result.farm_name,
        "exportedAt": result.exported_at,
        "sows": len(result.sows),
        "boars": len(result.boars),
        "events": len(result.events),
        "boarEvents": len(result.boar_events),
        "marketDeaths": len(result.market_deaths),
        "pens": len(result.pens),
        "customTasks": len(result.custom_tasks),
        "hasSettings": bool(result.settings),
        "semenCollections": len([e for e in result.boar_events
                                 if e.get("type") == "SC"]),
        "semenCollectionsSkipped": 0,
        "semenQualityRows": 0,
        "oddBoarTags": [],
        "byCode": dict(collections.Counter(e.get("type") for e in result.events)),
        "dateRange": ([min(dates).isoformat(), max(dates).isoformat()]
                      if dates else None),
        # 備份裡的「可疑記錄」在當初匯入時就已經判斷過了,`excluded` 就是
        # 那次的答案 —— 還原時再問一次,等於要使用者把做過的決定重做一遍。
        "anomalies": [],
        "excludedCount": sum(1 for e in result.events if e.get("excluded")),
        "skipped": {},
        "badLines": result.problems[:20],
        "badLineCount": len(result.problems),
    }


def restore_backup(store, farm_id, result: BackupResult, recorded_by=None) -> dict:
    """把備份放回一座牧場。

    **照原樣寫回去,不重算。** 耳號帶著離群年份後綴(2580-D115)就照樣
    存 —— 所以不能走「記錄一筆淘汰事件」那條路,那條路看到淘汰會再加一次
    後綴,變成 2580-D115-D115。胎次與狀態也直接用備份裡的值,不從事件重
    推:備份存的是當時系統算好的答案,重推只是多一個出錯的機會。

    整段包在 store.batch() 裡,理由跟 import_into 一樣 —— 三萬筆事件各自
    連一次資料庫的話,PostgresStore 上要跑幾十分鐘。
    """
    with store.batch() as store:
        return _write_backup(store, farm_id, result, recorded_by)


def _write_backup(store, farm_id, result: BackupResult, recorded_by=None) -> dict:
    existing_sows = {s["ear_tag"]: s["id"] for s in store.list_sows(farm_id)}
    existing_boars = {b["ear_tag"]: b["id"] for b in store.list_boars(farm_id)}

    # 產房欄位要先建,母豬的 pen_id 才有東西可以指。備份裡的 id 是舊資料庫
    # 的流水號,還原後一定是另一組,所以用「區域 + 編號」對應而不是 id。
    pen_map = {(p["zone"], p["name"]): p["id"] for p in store.list_pens(farm_id)}
    old_pen = {}
    pens_added = 0
    for p in result.pens:
        name = str(p.get("name") or "").strip()
        zone = p.get("zone") or "farrowing"
        if not name:
            continue
        if (zone, name) not in pen_map:
            pen_map[(zone, name)] = store.add_pen(farm_id, name, zone)
            pens_added += 1
        if isinstance(p.get("id"), int):
            old_pen[p["id"]] = pen_map[(zone, name)]

    sow_map = {}
    sows_added = 0
    for s in result.sows:
        tag = str(s["earTag"]).strip()
        sow_id = existing_sows.get(tag)
        if sow_id is None:
            sow_id = store.add_sow(
                farm_id, tag,
                entry_date=_parse_iso(s.get("entryDate")),
                birth_date=_parse_iso(s.get("birthDate")),
                breed=s.get("breed") or "",
                sire_tag=s.get("sireTag") or "",
                dam_tag=s.get("damTag") or "",
                created_by=recorded_by,
                is_unknown=bool(s.get("isUnknown")),
            )
            sows_added += 1
        store.update_sow(farm_id, sow_id,
                         parity=s.get("parity") or 0,
                         status=s.get("status") or "active",
                         pen_id=old_pen.get(s.get("penId")))
        if isinstance(s.get("id"), int):
            sow_map[s["id"]] = sow_id
        sow_map[tag] = sow_id

    boar_map = {}
    boars_added = 0
    for b in result.boars:
        tag = str(b.get("earTag") or "").strip()
        if not tag:
            continue
        boar_id = existing_boars.get(tag)
        if boar_id is None:
            boar_id = store.add_boar(
                farm_id, tag, entry_date=_parse_iso(b.get("entryDate")),
                breed=b.get("breed") or "", sire_tag=b.get("sireTag") or "",
                dam_tag=b.get("damTag") or "", created_by=recorded_by)
            boars_added += 1
        if b.get("status") and b["status"] != "active":
            store.update_boar(farm_id, boar_id, status=b["status"])
        if isinstance(b.get("id"), int):
            boar_map[b["id"]] = boar_id
        boar_map[tag] = boar_id

    # 同一頭、同一天、同樣內容的記錄用 seq 區分,跟匯入同一個做法 ——
    # 同一天死兩隻仔豬是真的兩筆,不是重複輸入(見 db.py 的 seq 欄位)。
    seen = collections.Counter()
    written = excluded = 0
    for e in result.events:
        sow_id = sow_map.get(e.get("sowId"))
        if sow_id is None:
            sow_id = sow_map.get(str(e.get("earTag") or "").strip())
        if sow_id is None:
            continue
        detail = e.get("detail") if isinstance(e.get("detail"), dict) else {}
        if detail.get("pen_id") in old_pen:
            detail = dict(detail, pen_id=old_pen[detail["pen_id"]])
        when = _parse_iso(e["date"])
        key = (sow_id, e["type"], when,
               json.dumps(detail, sort_keys=True, ensure_ascii=False))
        seq = seen[key]
        seen[key] += 1
        event_id = store.add_sow_event(farm_id, sow_id, e["type"], when, detail,
                                       recorded_by=recorded_by, seq=seq)
        if e.get("excluded"):
            store.set_event_excluded(farm_id, event_id, True)
            excluded += 1
        written += 1

    existing_boar_keys = {
        (be["boar_id"], be["event_type"], be["event_date"],
         json.dumps(be.get("detail") or {}, sort_keys=True, ensure_ascii=False))
        for be in store.list_boar_events(farm_id)}
    boar_written = 0
    for e in result.boar_events:
        boar_id = boar_map.get(e.get("boarId"))
        if boar_id is None:
            boar_id = boar_map.get(str(e.get("earTag") or "").strip())
        if boar_id is None:
            continue
        detail = e.get("detail") if isinstance(e.get("detail"), dict) else {}
        when = _parse_iso(e["date"])
        key = (boar_id, e["type"], when,
               json.dumps(detail, sort_keys=True, ensure_ascii=False))
        if key in existing_boar_keys:
            continue
        store.add_boar_event(farm_id, boar_id, e["type"], when, detail,
                             recorded_by=recorded_by)
        existing_boar_keys.add(key)
        boar_written += 1

    deaths = 0
    for d in result.market_deaths:
        detail = d.get("detail") if isinstance(d.get("detail"), dict) else {}
        store.add_market_death(
            farm_id, _parse_iso(d["date"]),
            reason=detail.get("reason") or d.get("reason") or "",
            weight_kg=detail.get("weight_kg", d.get("weightKg")),
            recorded_by=recorded_by)
        deaths += 1

    existing_tasks = {(t["name"], t["start_date"], t["repeat_rule"])
                      for t in store.list_custom_tasks(farm_id)}
    tasks_added = 0
    for t in result.custom_tasks:
        name = str(t.get("name") or "").strip()
        start = _parse_iso(t.get("startDate"))
        rule = t.get("repeat") or "once"
        if not name or start is None or (name, start, rule) in existing_tasks:
            continue
        store.add_custom_task(farm_id, name, start, rule)
        existing_tasks.add((name, start, rule))
        tasks_added += 1

    if result.settings:
        store.set_farm_settings(farm_id, result.settings)

    return {"sows": sows_added, "events": written, "excluded": excluded,
            "boars": boars_added, "boarEvents": boar_written,
            "marketDeaths": deaths, "pens": pens_added,
            "customTasks": tasks_added, "settings": bool(result.settings)}
