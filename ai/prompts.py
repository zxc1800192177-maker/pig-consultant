"""提示詞。

單獨成一個模組(SRP):因「顧問語氣與要求改變」而修改,
與傳輸層的「怎麼呼叫 CLI」無關。
"""

from typing import List, Optional

# 背景資訊最多帶幾項弱項。全部塞進去會稀釋使用者真正的提問。
MAX_CONTEXT_ITEMS = 5


DISEASE_SYSTEM_PROMPT = """你是「豬豬顧問」,協助台灣豬場的疾病與用藥諮詢。

回答時必須包含:
1. 可能病因(分類列出,依可能性排序)
2. 建議藥品與使用方式
3. 風險評估:副作用、休藥期、抗藥性,三者缺一不可

用語規範:
- 用繁體中文,採台灣豬業慣用術語
- 說「可能病因」「建議方向」,不要用確診語氣斷定豬隻得了什麼病
- 每則回答都要提醒實際確診與用藥須由執業獸醫師判斷
- 條列清楚,避免冗長
- 這是單次問答,回答要完整,不要請使用者提供更多資訊後再說

若問題與豬隻健康無關,禮貌說明你的專長範圍即可。"""


ADVICE_SYSTEM_PROMPT = """你是「豬豬顧問」,協助台灣豬場解讀生產指標健檢結果。

使用者提供的級距與落後程度**已由系統計算完成**,不要自行計算或推翻這些數值。
你的工作是解讀與建議:

1. 說明這些落後項目對牧場經營的實際影響
2. 針對每一項給出具體可執行的改善做法,不要只說「建議改善」
3. 若項目之間有因果關係,指出應該先處理哪個上游項目

用繁體中文,採台灣豬業慣用術語,條列清楚,避免冗長。
不要重述使用者已經看得到的數字,直接談該怎麼做。"""


def build_farm_context(weaknesses: Optional[List[dict]]) -> str:
    """把健檢弱項整理成疾病諮詢的背景資訊。

    US-1 驗收條件 7、8:有做過健檢就帶入,沒做過也要能正常使用。
    必須標明這是背景參考,否則模型可能把它當成使用者的提問內容。
    """
    if not weaknesses:
        return ""

    lines = [
        f"- {w['name']}:{w['grade']} 級,落後全國平均 {w['shortfall_sd']:.2f} 個標準差"
        for w in weaknesses[:MAX_CONTEXT_ITEMS]
    ]
    return (
        "【背景參考:本場生產指標健檢的落後項目】\n"
        + "\n".join(lines)
        + "\n以上僅供你判斷時參考,使用者的提問在下方。若與提問無關就不必提及。\n"
    )


def build_advice_prompt(weaknesses: List[dict]) -> str:
    """把已算好的弱項整理成給 AI 解讀的輸入。"""
    lines = []
    for i, w in enumerate(weaknesses, 1):
        line = (
            f"{i}. {w['name']} —— {w['grade']} 級,"
            f"落後全國平均 {w['shortfall_sd']:.2f} 個標準差"
        )
        if w.get("improvement"):
            line += f"(年報建議方向:{w['improvement']})"
        if w.get("downstream_names"):
            line += f";改善後會帶動:{'、'.join(w['downstream_names'])}"
        lines.append(line)

    return (
        "以下是本場生產指標健檢中低於全國中位數的項目,已依落後程度排序:\n\n"
        + "\n".join(lines)
        + "\n\n請針對這些項目給出改善建議。"
    )
