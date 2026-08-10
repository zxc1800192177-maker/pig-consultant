"""諮詢流程 —— 把確定性的部分與 AI 生成的部分組起來。

**關鍵設計:確定性的部分不經過 AI,也不依賴 AI 成功。**

通報須知與升級判斷在呼叫 AI 之前就算好並回傳。AI 掛掉時它們照樣送到
使用者眼前 —— 防疫提示不該因為額度用盡就消失(憲法第一、二條)。
"""

from typing import Iterator, List, NamedTuple, Optional

import config
from ai.label import parse_label
from ai.prompts import (
    ADVICE_SYSTEM_PROMPT,
    DISEASE_SYSTEM_PROMPT,
    LABEL_SYSTEM_PROMPT,
    build_advice_prompt,
    build_dosage_reference,
    build_farm_context,
    build_history_context,
    build_label_prompt,
    build_my_drugs_context,
    build_reference_factors,
)
from core.dosage import DosageEntry, match_dosage_entries
from core.reportable import ReportableMatch, baseline_notice, detect_reportable


class Consultation(NamedTuple):
    """一次諮詢的結果。

    baseline_notice、escalation、dosage_matches 是計算出來的,取得時即已確定;
    stream 是 AI 生成的,要迭代才會真的呼叫。
    """

    baseline_notice: str
    escalation: Optional[ReportableMatch]
    dosage_matches: List[DosageEntry]
    stream: Iterator[str]


class Consultant:
    def __init__(self, transport):
        self.transport = transport

    @staticmethod
    def _trim_history(history: Optional[List[dict]]) -> List[dict]:
        """裁切前端送來的對話歷史。

        歷史來自瀏覽器,內容不可信:20 則各塞 10 萬字一樣能灌爆 token 成本,
        所以「則數」與「每則長度」都要在伺服器端強制設限。
        格式壞掉的資料直接忽略,不讓它導致例外。
        """
        if not isinstance(history, list):
            return []

        cleaned = []
        for turn in history[-config.MAX_HISTORY_TURNS:]:
            if not isinstance(turn, dict):
                continue
            content = turn.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            role = "user" if turn.get("role") == "user" else "assistant"
            cleaned.append({
                "role": role,
                "content": content.strip()[:config.MAX_HISTORY_CHARS],
            })
        return cleaned

    @staticmethod
    def _clean_my_drugs(my_drugs: Optional[List[dict]]) -> List[dict]:
        """裁切使用者自己輸入的藥品庫。

        跟對話歷史一樣來自瀏覽器 localStorage,一樣不可信 ——
        則數與每個欄位的長度都要在伺服器端強制設限,格式壞掉的項目直接忽略。
        數字本身不查證(信任邊界是牧場主自己抄自己的藥品標示),
        但型別與長度仍要收斂,否則一則超長字串一樣能拿來灌爆 prompt。
        """
        if not isinstance(my_drugs, list):
            return []

        cleaned = []
        for drug in my_drugs[:config.MAX_MY_DRUGS]:
            if not isinstance(drug, dict):
                continue
            name = drug.get("name")
            if not isinstance(name, str) or not name.strip():
                continue

            note = drug.get("dosageNote")
            note = note.strip()[:config.MAX_DRUG_NOTE_CHARS] if isinstance(note, str) else ""

            ingredient = drug.get("activeIngredient")
            ingredient = (
                ingredient.strip()[:config.MAX_DRUG_INGREDIENT_CHARS]
                if isinstance(ingredient, str) else ""
            )

            withdrawal = drug.get("withdrawalDays")
            if not isinstance(withdrawal, (int, float)) or isinstance(withdrawal, bool) or withdrawal < 0:
                withdrawal = None

            cleaned.append({
                "name": name.strip()[:config.MAX_DRUG_NAME_CHARS],
                "active_ingredient": ingredient,
                "dosage_note": note,
                "withdrawal_days": withdrawal,
            })
        return cleaned

    @staticmethod
    def _clean_factors(factors: Optional[List[dict]]) -> List[dict]:
        """裁切「其他參考因素」。跟藥品庫同樣的道理:來自瀏覽器,不可信,
        則數與長度都要在伺服器端強制設限,格式壞掉的項目直接忽略。
        """
        if not isinstance(factors, list):
            return []

        cleaned = []
        for f in factors[:config.MAX_REFERENCE_FACTORS]:
            if not isinstance(f, dict):
                continue
            name = f.get("name")
            if not isinstance(name, str) or not name.strip():
                continue

            value = f.get("value")
            value = value.strip()[:config.MAX_FACTOR_CHARS] if isinstance(value, str) else ""

            cleaned.append({
                "name": name.strip()[:config.MAX_FACTOR_CHARS],
                "value": value,
            })
        return cleaned

    def consult(
        self,
        question: str,
        weaknesses: Optional[List[dict]] = None,
        history: Optional[List[dict]] = None,
        my_drugs: Optional[List[dict]] = None,
    ) -> Consultation:
        """疾病諮詢。

        通報判斷先做完再呼叫 AI —— 使用者可能在 AI 回完前就關掉頁面,
        防疫提示不能等到最後才出現。劑量對照表比對同理:結果必須在
        呼叫 AI 之前就算好,因為它不依賴 AI 是否成功回答。
        """
        # 型別必須先檢查:非字串直接呼叫 .strip() 會拋 AttributeError,
        # 一路往上炸掉整個請求處理,使用者只會看到畫面永遠卡在載入中。
        # 網頁介面不會送出這種資料,但任何人直接呼叫 API 就會觸發。
        if question is not None and not isinstance(question, str):
            raise ValueError("問題必須是文字")

        question = (question or "").strip()
        if not question:
            raise ValueError("問題不可為空")
        if len(question) > config.MAX_QUESTION_CHARS:
            raise ValueError(f"問題請控制在 {config.MAX_QUESTION_CHARS} 字以內")

        dosage_matches = match_dosage_entries(question)
        cleaned_drugs = self._clean_my_drugs(my_drugs)

        parts = [
            build_history_context(self._trim_history(history)),
            build_farm_context(weaknesses),
            build_dosage_reference(dosage_matches),
            build_my_drugs_context(cleaned_drugs),
            question,
        ]
        prompt = "\n".join(part for part in parts if part)

        return Consultation(
            baseline_notice=baseline_notice(),
            # 通報偵測只看這次的提問,不看歷史 —— 否則使用者一旦提過
            # 非洲豬瘟,之後每一題都會跳出警示,很快就會被忽略。
            escalation=detect_reportable(question),
            dosage_matches=dosage_matches,
            stream=self.transport.stream(prompt, DISEASE_SYSTEM_PROMPT),
        )

    def advise(
        self,
        weaknesses: List[dict],
        reference_factors: Optional[List[dict]] = None,
        question: Optional[str] = None,
        history: Optional[List[dict]] = None,
    ) -> Iterator[str]:
        """生產健檢的改善建議。

        送進去的級距與落後程度**已經算好**,AI 只負責解讀(憲法第二條)。
        沒有弱項就不呼叫 AI —— 沒必要為了「恭喜你都很好」花掉額度。

        question 有值時是追問模式:延續同一份改善建議繼續討論「那我該
        先做哪個」這類問題,用的仍是 ADVICE_SYSTEM_PROMPT 這個 persona,
        不會切換成疾病諮詢的語氣 —— 使用者問的是經營建議,不是在問診。
        """
        if not weaknesses:
            return iter(())

        # 背景先講,指令後講 —— 跟 consult() 的組法一致。
        # 曾經反過來(弱項與「請給改善建議」在前、參考因素在後),模型會
        # 把參考因素當成講完才補的附註,建議照樣是通用答案,牧場填的
        # 豬舍型式、飼養規模完全沒有反映在內容裡。
        cleaned_factors = self._clean_factors(reference_factors)
        parts = [build_reference_factors(cleaned_factors), build_advice_prompt(weaknesses)]

        if question is not None or history is not None:
            # 型別檢查跟 consult() 同一個理由:非字串呼叫 .strip() 會拋
            # AttributeError,一路炸掉整個請求處理。
            if question is not None and not isinstance(question, str):
                raise ValueError("問題必須是文字")
            question = (question or "").strip()
            if not question:
                raise ValueError("問題不可為空")
            if len(question) > config.MAX_QUESTION_CHARS:
                raise ValueError(f"問題請控制在 {config.MAX_QUESTION_CHARS} 字以內")

            parts.append(build_history_context(self._trim_history(history)))
            parts.append(question)

        prompt = "\n".join(part for part in parts if part)
        return self.transport.stream(prompt, ADVICE_SYSTEM_PROMPT)

    def read_label(self, image: dict) -> dict:
        """從藥品標示照片讀出藥品資料,回傳一份**草稿**。

        用 LABEL_SYSTEM_PROMPT 而不是顧問的兩個 persona:這是純抄寫任務,
        套上顧問角色模型會開始給用藥建議、加免責聲明,而我們要的只是
        標示上印了什麼。

        回傳的四個欄位都可能是 None(照片糊掉、標示沒印、根本不是藥品)。
        **呼叫端不得直接寫進藥品庫** —— 必須讓牧場主核對過(憲法第三條),
        否則藥品庫會從「人抄的」悄悄變成「AI 猜的」,而疾病諮詢還會把它
        當成可引用的劑量依據。
        """
        text = "".join(
            self.transport.stream_image(
                build_label_prompt(), LABEL_SYSTEM_PROMPT, image
            )
        )
        return parse_label(text)
