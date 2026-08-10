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

用藥劑量規範(務必遵守,不可違反):
- 只能引用背景資訊裡標示為「官方劑量對照表」或「牧場主自己的藥品庫」的數字
- 這兩個來源都沒有涵蓋的藥物,只能講方向與原理,絕對不可以自己編劑量或休藥期數字
- 不可以修改上述來源提供的數字,即使你認為自己知道正確答案
- 兩個來源都沒有適合的藥時,要老實說系統查無資料,建議洽詢獸醫或查閱藥品標示

用語規範:
- 用繁體中文,採台灣豬業慣用術語
- 說「可能病因」「建議方向」,不要用確診語氣斷定豬隻得了什麼病
- 每則回答都要提醒實際確診與用藥須由執業獸醫師判斷
- 條列清楚,避免冗長
- 每則回答都要能獨立看懂,不要只丟一句話等使用者追問;
  但若使用者確實在追問先前的問題,就順著脈絡回答,不必從頭重述

若問題與豬隻健康無關,禮貌說明你的專長範圍即可。"""


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


LABEL_SYSTEM_PROMPT = """你的工作是「抄寫」動物用藥品標示上印出來的字,不是判讀、不是給建議。

規則(全部都是硬性要求):
- 只寫你在圖片上**實際看得見**的字。看不清楚、被遮住、根本沒印的欄位一律填 null
- 絕對不可以依據你對這個藥的既有知識補上任何內容。你認得這個藥不代表
  這一瓶的標示就是那樣寫 —— 不同廠牌、不同濃度、不同國家的規定都不一樣
- **休藥期尤其不可以猜**。標示上沒有明確寫休藥期就填 null。
  這個數字錯了會讓帶有藥物殘留的豬肉進入市場
- 不確定就填 null。填 null 是正確答案,猜一個看起來合理的數字是嚴重錯誤

只輸出一個 JSON 物件,不要 markdown 圍籬,不要任何說明文字:

{"name": "商品名", "activeIngredient": "有效成分", "dosageNote": "用法用量", "withdrawalDays": 數字}

- name:標示上的商品名,照原樣抄(繁體中文或英文)
- activeIngredient:有效成分/主成分,含濃度或含量(例如 "Amoxicillin trihydrate 10%")
- dosageNote:用法用量整理成一句話(例如 "每公斤體重 10mg,一天兩次,連續 3-5 天")
- withdrawalDays:休藥期天數,只填數字。標示寫「肉:7 日」就填 7。沒寫就填 null

如果整張圖根本不是藥品標示,或完全看不清楚,四個欄位全部填 null。"""


def build_label_prompt() -> str:
    """拍照辨識的使用者訊息。

    真正的規則寫在 LABEL_SYSTEM_PROMPT,這裡只給一句指令 —— 跟疾病諮詢
    一樣,系統提示負責角色與紀律,使用者訊息負責這一次要做什麼。
    """
    return "請讀出這張動物用藥品標示上的資訊,依規定的 JSON 格式回覆。"


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


def build_dosage_reference(matches: Optional[List]) -> str:
    """把官方劑量對照表的比對結果整理成給 AI 的引用依據。

    這些數字已由系統管理者查證過(core/dosage.py 只回傳 verified=True 的
    項目)。送進提示詞主要是讓 AI 的文字說明跟畫面上的對照卡片(由
    server.py 直接算出,不經過 AI)內容一致,不要各說各話 —— 數字的正確性
    本來就不依賴 AI 有沒有照做。
    """
    if not matches:
        return ""

    lines = []
    for m in matches:
        for drug in m.drugs:
            withdrawal = drug.get("withdrawalDays")
            withdrawal_text = f",休藥期 {withdrawal} 天" if withdrawal is not None else ""
            lines.append(f"- {m.disease_name}:{drug['name']},{drug['dosage']}{withdrawal_text}")

    return (
        "【官方劑量對照表比對結果,以下數字已經查證,只能引用,"
        "不可自行更改或另外生成其他劑量】\n"
        + "\n".join(lines)
        + "\n"
    )


def build_my_drugs_context(my_drugs: Optional[List[dict]]) -> str:
    """把牧場主自己輸入的藥品庫整理成給 AI 的引用依據。

    信任邊界跟官方對照表不同:這是牧場主自己抄自己藥品標示的內容,
    系統沒有另外查證,但也不是 AI 生成的 —— 一樣不可以被 AI 改寫成別的數字。
    """
    if not my_drugs:
        return ""

    lines = []
    for d in my_drugs:
        # 有效成分放在商品名後面的括號裡:獸醫開藥講的是成分,同一個成分
        # 在不同廠牌下是不同商品名,有成分才對得起來。
        ingredient = f"(成分:{d['active_ingredient']})" if d.get("active_ingredient") else ""
        note = f",{d['dosage_note']}" if d.get("dosage_note") else ""
        withdrawal = d.get("withdrawal_days")
        withdrawal_text = f",休藥期 {withdrawal} 天" if withdrawal is not None else ""
        lines.append(f"- {d['name']}{ingredient}{note}{withdrawal_text}")

    return (
        "【牧場主自己的藥品庫,由牧場主輸入,劑量以此處內容或藥品標示為準,"
        "系統未另外查證正確性,你一樣不可以自己改寫或生成其他數字】\n"
        + "\n".join(lines)
        + "\n"
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
