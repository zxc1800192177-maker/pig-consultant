"""提示詞。

單獨成一個模組(SRP):因「顧問語氣與要求改變」而修改,
與傳輸層的「怎麼呼叫 CLI」無關。
"""

from typing import List, Optional

# 背景資訊最多帶幾項弱項。全部塞進去會稀釋使用者真正的提問。
MAX_CONTEXT_ITEMS = 5


ADVICE_SYSTEM_PROMPT = """你是「豬豬顧問」,協助台灣豬場解讀生產指標健檢結果。

使用者提供的級距與落後程度**已由系統計算完成**,不要自行計算或推翻這些數值。
你的工作是解讀與建議:

1. 說明這些落後項目對牧場經營的實際影響
2. 針對每一項給出具體可執行的改善做法,不要只說「建議改善」
3. 若項目之間有因果關係,指出應該先處理哪個上游項目
4. 若有提供「本場的其他條件」(豬舍型式、飼養規模、人力設備等),
   建議必須貼著這些條件寫 —— 開放式豬舍與水簾舍能做的事不一樣,
   300 頭與 3000 頭母豬的做法也不一樣。不要給一份換到別場也通用的答案

用繁體中文,採台灣豬業慣用術語,條列清楚,避免冗長。
不要重述使用者已經看得到的數字,直接談該怎麼做。"""


def build_history_context(history: Optional[List[dict]]) -> str:
    """把先前的對話整理成可追問的上下文。

    歷史存在使用者自己的瀏覽器,每次提問時帶上來 —— 伺服器不保存任何人的
    問題內容。若改成由伺服器依 IP 保存,同一間辦公室(共用對外 IP)的兩個人
    會看到彼此的對話,是隱私外洩。

    但也因為歷史來自前端,內容完全不可信:數量與長度的上限必須在
    伺服器端強制執行(呼叫端負責裁切,見 config.MAX_HISTORY_*)。
    """
    if not history:
        return ""

    lines = []
    for turn in history:
        role = "使用者" if turn.get("role") == "user" else "顧問"
        content = (turn.get("content") or "").strip()
        if content:
            lines.append(f"{role}:{content}")

    if not lines:
        return ""

    return (
        "【先前的對話,供你理解使用者這次在追問什麼】\n"
        + "\n".join(lines)
        + "\n【以上為歷史紀錄。使用者這次的問題在下方】\n"
    )


def build_reference_factors(factors: Optional[List[dict]]) -> str:
    """把牧場主填寫的「其他參考因素」整理成給 AI 的背景資訊。

    這些不是常模的評級項目,系統無法查核正確性,純粹是牧場主自己提供的
    補充說明(例如豬舍類型、飼養規模、最近有無疫情)。標成「參考」是要
    讓 AI 知道這是輔助判斷改善建議時該考慮的背景,不是要它拿來計算
    或驗證什麼 —— 跟弱項的級距不同,弱項是系統算出來的(憲法第二條),
    這裡的內容完全是使用者說了算。

    呼叫端必須把這段放在「請給改善建議」那句指令**之前**(見
    Consultant.advise)。曾經放在指令後面,模型會把它當成講完才補的
    附註,建議內容照樣是通用答案,完全沒有反映牧場的實際條件。
    """
    if not factors:
        return ""

    lines = [f"- {f['name']}:{f['value']}" for f in factors if f.get("value")]
    if not lines:
        return ""

    return (
        "【本場的其他條件,由牧場主提供。這是背景參考,不是評級依據,"
        "不可拿來重算級距】\n"
        + "\n".join(lines)
        + "\n給建議時必須把這些條件納入考量 —— 同樣一項落後指標,"
        "在不同的豬舍型式、飼養規模、人力與設備條件下,可行的做法不一樣。"
        "某項條件明顯是某個落後指標的成因或限制時,要直接指出來。\n"
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
