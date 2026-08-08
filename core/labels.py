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


def reportable_disclaimer() -> str:
    """法定傳染病提醒的完整度聲明。

    關鍵字清單不可能完整,畫面上必須讓使用者知道沒跳提示不等於安全。
    """
    return "本系統的傳染病提醒非完整法定清單,不可作為合規依據;未出現提醒不代表安全"
