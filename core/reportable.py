"""法定動物傳染病提醒。

憲法第一、二條:通報提示由關鍵字比對觸發,提示文字是固定樣板,不經過 AI。
理由是漏報會延誤防疫,而 AI 的判斷不穩定 —— 同樣的描述可能這次提醒、下次沒提醒。

**設計上的關鍵決定:兩層提示,而非單一偵測器。**

查證時無法自公開來源取得完整且現行的官方病種清單(完整分類表在法規 PDF 附件中,
最後修正為民國101年)。既然清單不可能完整,一個「沒命中就沉默」的偵測器很危險:
使用者會把沒跳提示解讀成「系統說沒事」。

因此:
  baseline_notice()   疾病諮詢一律附上,說明本系統無法判斷法定傳染病
  detect_reportable() 命中時升級為顯著警示

沒命中不代表安全,只代表本系統的關鍵字沒認出來 —— 這件事必須寫在畫面上。

資料:data/reportable_diseases.json
說明:specs/reference/reportable-diseases.md
"""

import json
import pathlib
import unicodedata
from typing import List, NamedTuple, Optional

DATA_PATH = pathlib.Path(__file__).parent.parent / "data" / "reportable_diseases.json"


class ReportableMatch(NamedTuple):
    disease: str
    matched_terms: List[str]
    notice: str          # 固定樣板,非 AI 生成
    reason: str          # "disease_name" 或 "symptom_combination",供追溯


def _load():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


_DATA = _load()


def baseline_notice() -> str:
    """疾病諮詢一律附上的通報須知,與有沒有命中關鍵字無關。

    這是本模組最重要的輸出。關鍵字清單永遠不可能完整,
    所以不能讓「沒有警示」承擔「安全」的意思。
    """
    return _DATA["baseline_notice"]


def is_list_complete() -> bool:
    """關鍵字清單是否為完整的官方法定傳染病清單。

    永遠回傳 False。保留這個函式是為了讓呼叫端無法忽略這件事 ——
    畫面上必須據此標示「非完整清單,不可作為合規依據」。
    """
    return bool(_DATA["_completeness"]["is_complete"])


def _normalize(text: str) -> str:
    """全形轉半形、移除空白、統一小寫。

    現場輸入格式不一 ——「ＡＳＦ」「非 洲 豬 瘟」「AsF」都必須能命中。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(text.split())
    return text.lower()


def detect_reportable(text: str) -> Optional[ReportableMatch]:
    """偵測輸入是否指向法定動物傳染病。

    觸發條件(任一):
      1. 直接出現病名或代號(ASF/FMD/CSF)
      2. 同一疾病的症狀關鍵字命中達 min_symptom_matches 個

    第 2 條的門檻是為了避免「發燒」這類通用症狀單獨觸發,
    否則使用者很快就會對提示麻痺,反而在真的有事時忽略它。
    """
    normalized = _normalize(text)
    if not normalized:
        return None

    notice = _DATA["escalated_notice"]
    threshold = _DATA["min_symptom_matches"]

    # 病名優先:講出病名代表使用者已有懷疑,不需要再湊症狀
    for disease in _DATA["diseases"]:
        for alias in disease["aliases"]:
            if _normalize(alias) in normalized:
                return ReportableMatch(
                    disease=disease["name"],
                    matched_terms=[alias],
                    notice=notice,
                    reason="disease_name",
                )

    # 症狀組合
    for disease in _DATA["diseases"]:
        hits = [s for s in disease["symptoms"] if _normalize(s) in normalized]
        if len(hits) >= threshold:
            return ReportableMatch(
                disease=disease["name"],
                matched_terms=hits,
                notice=notice,
                reason="symptom_combination",
            )

    return None
