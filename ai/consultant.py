"""諮詢流程 —— 把確定性的部分與 AI 生成的部分組起來。

**關鍵設計:確定性的部分不經過 AI,也不依賴 AI 成功。**

通報須知與升級判斷在呼叫 AI 之前就算好並回傳。AI 掛掉時它們照樣送到
使用者眼前 —— 防疫提示不該因為額度用盡就消失(憲法第一、二條)。
"""

from typing import Iterator, List, NamedTuple, Optional

import config
from ai.prompts import (
    ADVICE_SYSTEM_PROMPT,
    DISEASE_SYSTEM_PROMPT,
    build_advice_prompt,
    build_farm_context,
)
from core.reportable import ReportableMatch, baseline_notice, detect_reportable


class Consultation(NamedTuple):
    """一次諮詢的結果。

    baseline_notice 與 escalation 是計算出來的,取得時即已確定;
    stream 是 AI 生成的,要迭代才會真的呼叫。
    """

    baseline_notice: str
    escalation: Optional[ReportableMatch]
    stream: Iterator[str]


class Consultant:
    def __init__(self, transport):
        self.transport = transport

    def consult(
        self,
        question: str,
        weaknesses: Optional[List[dict]] = None,
    ) -> Consultation:
        """疾病諮詢。

        通報判斷先做完再呼叫 AI —— 使用者可能在 AI 回完前就關掉頁面,
        防疫提示不能等到最後才出現。
        """
        question = (question or "").strip()
        if not question:
            raise ValueError("問題不可為空")
        if len(question) > config.MAX_QUESTION_CHARS:
            raise ValueError(f"問題請控制在 {config.MAX_QUESTION_CHARS} 字以內")

        context = build_farm_context(weaknesses)
        prompt = f"{context}\n{question}" if context else question

        return Consultation(
            baseline_notice=baseline_notice(),
            escalation=detect_reportable(question),
            stream=self.transport.stream(prompt, DISEASE_SYSTEM_PROMPT),
        )

    def advise(self, weaknesses: List[dict]) -> Iterator[str]:
        """生產健檢的改善建議。

        送進去的是**已經算好的**級距與落後程度,AI 只負責解讀(憲法第二條)。
        沒有弱項就不呼叫 AI —— 沒必要為了「恭喜你都很好」花掉額度。
        """
        if not weaknesses:
            return iter(())
        return self.transport.stream(
            build_advice_prompt(weaknesses),
            ADVICE_SYSTEM_PROMPT,
        )
