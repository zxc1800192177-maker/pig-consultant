"""顯示文字產生。

從 benchmark.py 拆出來的理由(SRP):
  benchmark.py 因「資料檔格式改變」而修改
  labels.py    因「顯示措辭改變」而修改
兩者混在一起時,改個標題文字要動到資料存取模組,反之亦然。

本模組只負責把資料轉成給人看的字串,不做任何查詢或計算。
"""

from core.benchmark import BENCHMARK, get_metric


def source_label() -> str:
    """常模資料的來源標註。憲法第三條要求顯示常模數字時一併呈現。"""
    src = BENCHMARK["source"]
    return f"{src['name']} · {src['region']} {src['year']} 年 · 全國 {src['farms']} 場"


def sample_size_note(key: str) -> str:
    """樣本數不足全體時的標註;足額則回空字串。

    憲法第三條:樣本數不足的項目必須顯示實際樣本數,不得隱藏。
    """
    metric = get_metric(key)
    total = BENCHMARK["source"]["farms"]
    if metric["sample_size"] < total:
        return f"樣本數 {metric['sample_size']}(未達全體 {total} 場)"
    return ""


def grade_label(letter: str) -> str:
    """級距的完整說明,例如 "D 級(50~75%)"。"""
    from core.grading import BANDS, WORST_GRADE

    cuts = [cut for cut, _ in BANDS]
    letters = [ltr for _, ltr in BANDS]
    if letter == WORST_GRADE:
        return f"{letter} 級(後 {100 - cuts[-1]}%)"
    i = letters.index(letter)
    lower = 0 if i == 0 else cuts[i - 1]
    return f"{letter} 級({lower}~{cuts[i]}%)"


def shortfall_note() -> str:
    """排序依據的說明。憲法第三條:顯示的數字要能追溯到來源。"""
    return "依距離全國平均的標準差排序,數值取自年報常模表"


def upstream_note() -> str:
    """連鎖影響關係的標註。

    這是本專案唯一仍屬「文字解讀」的部分,依憲法第三條必須誠實標示。
    """
    return "連鎖影響關係整理自 2025 年報的因果敘述,建議由領域專家覆核"


def ai_unavailable_note() -> str:
    """AI 不可用時對使用者的說明。

    同一句話原本散落在 server.py 的錯誤回應、啟動訊息與 app.js 三處,
    措辭一改就會不同步。集中在這裡,前端也改由 /api/health 取得,
    不再自己寫一份(規格 6.5:AI 停擺時健檢仍完全可用)。
    """
    return "AI 諮詢目前無法使用。生產健檢為純計算,不受影響,仍可正常使用。"


def medical_disclaimer() -> str:
    """每則 AI 回答正上方的固定免責條。

    原本只放在頁尾,但使用者拿到用藥建議時看的是畫面中間,不一定會捲到底部。
    這裡由程式強制加在回答之前,不依賴 AI 自己寫 —— 憲法第一條。

    休藥期尤其關鍵:講錯會讓藥物殘留的豬肉進入食物鏈,受害的是第三方。
    """
    return (
        "本回答由 AI 生成,僅供參考方向,不能取代獸醫師診斷。"
        "用藥前請務必與執業獸醫師確認劑量與休藥期,並以藥品標示為準。"
    )


def reportable_disclaimer() -> str:
    """法定傳染病提醒的完整度聲明。

    關鍵字清單不可能完整,畫面上必須讓使用者知道沒跳提示不等於安全。
    """
    return "本系統的傳染病提醒非完整法定清單,不可作為合規依據;未出現提醒不代表安全"


# 工作類型的顯示名稱。放在這裡而不是 schedule.py 或前端 ——
# 使用者看到的文字只該有一份定義(見 tests/test_single_source.py)。
TASK_LABELS = {
    "move_in": "移入產房",
    "induce": "催產",
    "farrow": "分娩",
    "wean": "離乳",
    "mate": "配種",
    "preg_check": "驗孕",
}


def task_label(kind: str) -> str:
    return TASK_LABELS.get(kind, kind)


# 母豬目前狀態。「配種待驗孕」與「懷孕中」刻意分開 —— 配種了不等於懷孕,
# 這個場目前有 50 頭驗孕陰性。混為一談會讓畫面宣稱一件還沒確認的事。
SOW_STATE_LABELS = {
    "pregnant": "懷孕中",
    "mated": "配種待驗孕",
    "lactating": "哺乳中",
    "open": "待配種",
    "exited": "已離群",
}

# 狀態旁邊那個「第幾天」要說清楚是從哪天算起,不然只是一個沒有意義的數字。
SOW_DAY_LABELS = {
    "pregnant": "懷孕第 {n} 天",
    "mated": "配種後 {n} 天",
    "lactating": "哺乳第 {n} 天",
    "open": "已空 {n} 天",
}


def sow_state_label(state: str) -> str:
    return SOW_STATE_LABELS.get(state, state)


def sow_day_label(state: str, days) -> str:
    template = SOW_DAY_LABELS.get(state)
    return template.format(n=days) if template and days is not None else ""


def pending_check_note(checked: bool, day: int, check_days: int,
                       overdue_days) -> str:
    """時間軸裡「還沒驗孕」的提示。

    這是缺席的資訊 —— 配種後沒有驗孕記錄,原本得自己數時間軸裡有沒有
    一筆「驗孕」才看得出來。2580 配種 143 天、從沒驗孕過,原本的時間軸
    完全看不出這件事。

    `checked` 為真時代表**有**驗孕記錄但結果沒填(匯入資料裡存在這種
    情形)—— 跟「根本沒驗」是不同的問題,不可以用同一句話帶過。
    """
    if checked:
        return "已記錄驗孕,但結果未填,建議補登"
    if overdue_days:
        return (f"尚未驗孕,已超過建議驗孕時間"
                f"(配種後 {check_days} 天)共 {overdue_days} 天")
    return f"尚未驗孕(建議配種後 {check_days} 天內,目前第 {day} 天)"


def overdue_farrow_label(days: int) -> str:
    """預產日已過卻沒有分娩記錄。

    她要嘛生了沒登記,要嘛沒保住 —— 兩種都要有人去看。把已經過去的日期
    當成未來的「預產」顯示,畫面等於在說謊(實測 200 頭裡有 88 頭的預產日
    已過,最久的過了 611 天)。
    """
    return f"預產日已過 {days} 天,尚無分娩記錄"


# 母豬卡的生產表現項目。單位分開放,畫面才能把數字放大、單位縮小。
PERFORMANCE_TEXT = {
    "total_born": ("窩均總仔數", "隻", 1),
    "born_alive": ("窩均活仔數", "隻", 1),
    "weaned": ("平均離乳數", "隻", 1),
    "litters_per_year": ("年產胎數", "胎", 2),
    "stillborn_rate": ("死胎率", "%", 1),
    "lactation_days": ("平均哺乳天數", "天", 1),
    "repeat_estrus": ("重發情次數", "次", 0),
}

# 三級。**不是 A~F 五級** —— 已確認改成三級,而且不得由三級反推五級
# (憲法第三條第 5 款:不推導未公布的級距)。
TIER_LABELS = {"good": "優秀", "mid": "中等", "poor": "待改善"}


def performance_label(key: str) -> str:
    return PERFORMANCE_TEXT.get(key, (key, "", 1))[0]


def performance_unit(key: str) -> str:
    return PERFORMANCE_TEXT.get(key, (key, "", 1))[1]


def performance_digits(key: str) -> int:
    return PERFORMANCE_TEXT.get(key, (key, "", 1))[2]


def tier_label(tier: str) -> str:
    return TIER_LABELS.get(tier, "")


def stillborn_note(overall: float, without_first: float) -> str:
    """死胎全部集中在最早那一胎時的說明。

    措辭是「最早記錄的那一胎」而非「第 1 胎」—— 匯入的歷史不保證從她的
    頭胎開始,宣稱是第 1 胎會是編出來的(憲法第三條)。
    """
    return (
        f"死胎幾乎全來自最早記錄的那一胎。排除該胎後為 {without_first:.1f}%,"
        f"整體 {overall:.1f}% 主要反映的是那一次,不是長期表現。"
    )


def performance_basis() -> str:
    """生產表現這一區一定要一起顯示的說明。

    **與同場其他母豬比,不與全國常模比。** 全國常模是場級指標,拿一頭母豬
    去對照整場的年報數字是拿不同單位的東西相比 —— 母豬卡的設計初稿寫成
    「對照全國常模」,那是錯的。
    """
    return "由事件記錄計算,非 AI 生成 ・ 級距是與本場其他母豬比較,不是全國常模"


# 「值得檢視」的理由。**措辭不得出現「淘汰」** —— 這個場實際的淘汰原因
# 裡「年齡太大」佔 48.0%,「生產性能差」只佔 2.9%,系統算得出來的正好是
# 最少被拿來當決策依據的那一項(憲法第三條第 6 款)。
REVIEW_LABELS = {
    "decline": "產仔數連續下滑",
    "npd": "非生產天數偏長",
    "low_alive": "活仔數低於場內多數",
}


def review_label(code: str) -> str:
    return REVIEW_LABELS.get(code, code)


# 設定畫面的文字。跟工作類型同樣的道理:使用者看得到的字只該有一份定義。
# `hint` 要講清楚這個數字**影響什麼**,不然牧場主無從判斷該不該改。
SETTING_TEXT = {
    "gestation_days":
        ("懷孕天數", "配種 → 預產期。移入產房、催產、分娩三項工作都由它推算", "天"),
    "pre_farrow_move_days":
        ("移入產房", "預產前幾天移入。也決定產房空間提醒的預估範圍", "天"),
    "induction_day":
        ("催產提醒", "懷孕第幾天提醒。系統只提醒時機,不提供藥名與劑量", "天"),
    "lactation_days":
        ("泌乳天數", "分娩 → 離乳。決定離乳工作出現在哪一週", "天"),
    "service_after_wean_days":
        ("離乳後配種", "離乳 → 配種的間隔", "天"),
    "preg_check_days":
        ("驗孕時機", "配種後幾天驗孕", "天"),
    "open_sow_alert_days":
        ("空胎提醒", "離乳或驗孕陰性後多久沒動作就提醒", "天"),
    "review_decline_litters":
        ("連續下滑胎數", "活仔數連續下滑幾胎才列入「值得檢視」", "胎"),
    "review_npd_days":
        ("非生產天數門檻", "每胎非生產天數超過幾天才列入「值得檢視」", "天"),
    "review_low_alive_pct":
        ("活仔數偏低門檻", "活仔數落在全場最低幾 % 才列入。與同場其他母豬比,不與全國常模比", "%"),
    "review_min_litters":
        ("最少判斷胎數", "至少幾胎才納入「值得檢視」判斷。一兩胎看不出趨勢", "胎"),
    "review_min_herd":
        ("最少比較頭數", "全場不足這個頭數就不比活仔數 —— 頭數太少時百分位只是最小值", "頭"),
}


def setting_label(key: str) -> str:
    return SETTING_TEXT.get(key, (key, "", ""))[0]


def setting_hint(key: str) -> str:
    return SETTING_TEXT.get(key, (key, "", ""))[1]


def setting_unit(key: str) -> str:
    return SETTING_TEXT.get(key, (key, "", ""))[2]


def review_caveat() -> str:
    """這份名單一定要一起顯示的但書。

    只列名單而不講清楚它看的是什麼,使用者會以為系統在替他做淘汰決定。
    """
    return (
        "這份名單只看得到生產記錄算得出來的東西。"
        "本場實際的淘汰原因裡,年齡太大佔 48.0%、生產性能差只佔 2.9% —— "
        "系統算得出來的正好不是主要依據。請當成「值得看一眼」的提示,"
        "不是淘汰建議。"
    )
