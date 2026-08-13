"""諮詢流程 —— 把確定性的部分與 AI 生成的部分組起來。

**關鍵設計:確定性的部分不經過 AI,也不依賴 AI 成功。**

通報須知與升級判斷在呼叫 AI 之前就算好並回傳。AI 掛掉時它們照樣送到
使用者眼前 —— 防疫提示不該因為額度用盡就消失(憲法第一、二條)。
"""

from typing import Iterator, List, NamedTuple, Optional

import config
from ai.prompts import (
    ADVICE_SYSTEM_PROMPT,
    build_advice_prompt,
    build_history_context,
    build_reference_factors,
)


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

